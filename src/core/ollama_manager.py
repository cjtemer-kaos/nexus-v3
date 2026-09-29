"""
OllamaManager - Auto-detect, start, and manage Ollama lifecycle

Features:
- Auto-detect if Ollama is installed
- Auto-start Ollama process if not running
- Auto-pull required models
- Health check with auto-restart
- VRAM management: unload idle models
- Windows: runs as child process, not service
"""

import asyncio
import json
import logging
import os
import platform
import shutil
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional

import httpx

logger = logging.getLogger("nexus-ollama")

# Default models for a working NEXUS system (GPU 12GB, models up to ~6B)
REQUIRED_MODELS = [
    "qwen2.5-coder:7b",     # Default: code + general (4.7GB)
    "deepseek-r1:8b",       # Reasoning (5.2GB)
    "qwen2.5:0.5b",         # Fast chat / fallback (0.4GB)
]

OPTIONAL_MODELS = [
    "nemotron-3-nano:4b",   # Fast responses (2.8GB)
    "qwen2.5vl:7b",         # Vision (6.0GB)
    "qwen2.5-coder:7b",        # Creative (9.6GB)
]


class OllamaManager:
    """
    Manages Ollama lifecycle: detection, startup, model management, health.
    """

    def __init__(
        self,
        ollama_url: str = None,
        ollama_home: str = None,
        auto_start: bool = True,
        auto_pull: bool = True,
        health_interval: int = 30,
    ):
        self.ollama_url = ollama_url or os.environ.get("OLLAMA_URL", "http://localhost:11434")
        self.ollama_home = ollama_home or os.environ.get("OLLAMA_HOME", "")
        self.auto_start = auto_start
        self.auto_pull = auto_pull
        self.health_interval = health_interval
        self._process: Optional[subprocess.Popen] = None
        self._health_task: Optional[asyncio.Task] = None
        self._available: Optional[bool] = None
        self._models_cache: Optional[List[str]] = None
        self._cache_time: float = 0

    # ─── Detection ─────────────────────────────────────────────────────

    def is_installed(self) -> bool:
        """Check if Ollama binary is available in PATH."""
        return shutil.which("ollama") is not None

    def find_ollama_binary(self) -> Optional[str]:
        """Find Ollama binary, checking common locations."""
        # Check PATH first
        binary = shutil.which("ollama")
        if binary:
            return binary

        # Common installation paths
        search_paths = []
        if platform.system() == "Windows":
            search_paths = [
                Path(os.environ.get("LOCALAPPDATA", "")) / "Ollama" / "ollama.exe",
                Path(os.environ.get("PROGRAMFILES", "")) / "Ollama" / "ollama.exe",
                Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Ollama" / "ollama.exe",
                Path.home() / "AppData" / "Local" / "Ollama" / "ollama.exe",
            ]
        elif platform.system() == "Linux":
            search_paths = [
                Path("/usr/local/bin/ollama"),
                Path("/usr/bin/ollama"),
                Path.home() / ".local" / "bin" / "ollama",
            ]
        elif platform.system() == "Darwin":
            search_paths = [
                Path("/usr/local/bin/ollama"),
                Path("/opt/homebrew/bin/ollama"),
            ]

        for p in search_paths:
            if p.exists():
                return str(p)

        return None

    # ─── Startup ───────────────────────────────────────────────────────

    def start(self) -> bool:
        """
        Start Ollama process. Returns True if Ollama is running (started or already running).
        """
        # Check if already running
        if self.is_running():
            logger.info("Ollama is already running")
            self._available = True
            return True

        binary = self.find_ollama_binary()
        if not binary:
            logger.warning("Ollama binary not found. Cannot auto-start.")
            return False

        logger.info(f"Starting Ollama from: {binary}")

        env = os.environ.copy()
        if self.ollama_home:
            env["OLLAMA_MODELS"] = self.ollama_home

        try:
            # Start Ollama in background
            self._process = subprocess.Popen(
                [binary, "serve"],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if platform.system() == "Windows" else 0,
            )
            logger.info(f"Ollama started (PID: {self._process.pid})")

            # Wait for it to be ready
            for i in range(30):
                time.sleep(1)
                if self._check_health_sync():
                    logger.info("Ollama is ready")
                    self._available = True
                    return True

            logger.warning("Ollama did not become ready within 30 seconds")
            return False

        except Exception as e:
            logger.error(f"Failed to start Ollama: {e}")
            return False

    def stop(self):
        """Stop Ollama process if we started it."""
        if self._process:
            logger.info(f"Stopping Ollama (PID: {self._process.pid})")
            try:
                self._process.terminate()
                self._process.wait(timeout=10)
            except Exception:
                self._process.kill()
            self._process = None
            self._available = False

    def is_running(self) -> bool:
        """Check if Ollama is running by checking health endpoint."""
        return self._check_health_sync()

    def _check_health_sync(self) -> bool:
        """Synchronous health check."""
        try:
            resp = httpx.get(f"{self.ollama_url}/api/tags", timeout=3.0)
            return resp.status_code == 200
        except Exception:
            return False

    # ─── Health Check ──────────────────────────────────────────────────

    async def health_check(self) -> bool:
        """Async health check."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.ollama_url}/api/tags")
                healthy = resp.status_code == 200
                if not healthy and self._available:
                    logger.warning("Ollama health check failed")
                elif healthy and not self._available:
                    logger.info("Ollama recovered")
                self._available = healthy
                return healthy
        except Exception:
            if self._available:
                logger.warning("Ollama health check failed (connection error)")
            self._available = False
            return False

    async def start_health_monitor(self):
        """Start background health monitoring with auto-restart."""
        if self._health_task:
            return

        async def _monitor():
            while True:
                await asyncio.sleep(self.health_interval)
                healthy = await self.health_check()
                if not healthy and self.auto_start:
                    logger.info("Attempting to restart Ollama...")
                    self.stop()
                    self.start()

        self._health_task = asyncio.create_task(_monitor())
        logger.info(f"Ollama health monitor started (interval: {self.health_interval}s)")

    def stop_health_monitor(self):
        """Stop background health monitoring."""
        if self._health_task:
            self._health_task.cancel()
            self._health_task = None

    # ─── Model Management ──────────────────────────────────────────────

    async def list_models(self) -> List[str]:
        """List installed models."""
        now = time.time()
        if self._models_cache and (now - self._cache_time) < 60:
            return self._models_cache

        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.ollama_url}/api/tags")
                if resp.status_code == 200:
                    data = resp.json()
                    models = [m.get("name", "") for m in data.get("models", [])]
                    self._models_cache = models
                    self._cache_time = now
                    return models
        except Exception as e:
            logger.warning(f"Failed to list models: {e}")

        return []

    async def has_model(self, name: str) -> bool:
        """Check if a model is installed."""
        models = await self.list_models()
        return any(name in m for m in models)

    async def pull_model(self, name: str, stream: bool = False) -> bool:
        """
        Pull a model from Ollama registry.
        Returns True when download is complete.
        """
        if await self.has_model(name):
            logger.info(f"Model {name} already installed")
            return True

        logger.info(f"Pulling model: {name}")
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream(
                    "POST",
                    f"{self.ollama_url}/api/pull",
                    json={"name": name, "stream": True},
                    timeout=None,
                ) as resp:
                    if resp.status_code != 200:
                        logger.error(f"Failed to pull {name}: HTTP {resp.status_code}")
                        return False

                    last_status = ""
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            status = data.get("status", "")
                            if status != last_status:
                                logger.info(f"  {name}: {status}")
                                last_status = status
                            if data.get("completed"):
                                logger.info(f"Model {name} downloaded successfully")
                                self._models_cache = None  # Invalidate cache
                                return True
                        except json.JSONDecodeError:
                            continue

        except Exception as e:
            logger.error(f"Failed to pull {name}: {e}")
            return False

        return False

    async def ensure_models(self, models: List[str] = None) -> Dict[str, bool]:
        """
        Ensure all required models are installed. Pulls missing ones.
        Returns dict of {model_name: success}.
        """
        models = models or REQUIRED_MODELS
        results = {}
        for model in models:
            results[model] = await self.pull_model(model)
        return results

    async def ensure_required_models(self) -> Dict[str, bool]:
        """Ensure all REQUIRED_MODELS are installed."""
        return await self.ensure_models(REQUIRED_MODELS)

    # ─── VRAM Management ───────────────────────────────────────────────

    async def unload_model(self, name: str) -> bool:
        """Unload a model from VRAM."""
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(
                    f"{self.ollama_url}/api/generate",
                    json={"model": name, "keep_alive": 0},
                )
                return resp.status_code == 200
        except Exception as e:
            logger.warning(f"Failed to unload {name}: {e}")
            return False

    async def get_loaded_models(self) -> List[Dict]:
        """Get models currently loaded in VRAM."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.ollama_url}/api/ps")
                if resp.status_code == 200:
                    data = resp.json()
                    return data.get("models", [])
        except Exception:
            pass
        return []

    # ─── Status ────────────────────────────────────────────────────────

    def get_status(self) -> Dict:
        return {
            "installed": self.is_installed(),
            "running": self.is_running(),
            "url": self.ollama_url,
            "binary": self.find_ollama_binary(),
            "pid": self._process.pid if self._process else None,
            "auto_start": self.auto_start,
            "auto_pull": self.auto_pull,
            "health_interval": self.health_interval,
        }

    async def get_full_status(self) -> Dict:
        models = await self.list_models()
        loaded = await self.get_loaded_models()
        return {
            **self.get_status(),
            "available": self._available,
            "installed_models": models,
            "loaded_models": loaded,
            "required_models": REQUIRED_MODELS,
            "optional_models": OPTIONAL_MODELS,
            "missing_required": [
                m for m in REQUIRED_MODELS if not any(m in inst for inst in models)
            ],
        }

    # ─── Cleanup ───────────────────────────────────────────────────────

    def __del__(self):
        self.stop()

    def __enter__(self):
        if self.auto_start:
            self.start()
        return self

    def __exit__(self, *args):
        self.stop()
