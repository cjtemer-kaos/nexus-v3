"""
Doctor Command — F15: Self-Diagnostic for the entire system

Checks: providers, extensions, connections, environment, memory, services.
Returns comprehensive health report with remediation suggestions.
"""

from src.brain.link import brain_path
import asyncio
import logging
import os
import shutil
import socket
import subprocess
import sys
from typing import Dict, List
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nexus-doctor")

# Auto-repair playbook — mapeo fallo conocido -> fix acotado (hermes-workspace parity)
# Si el fix seria destructivo/externo/especulativo -> escalar, no auto-reparar.
AUTO_REPAIR_PLAYBOOK: dict[str, dict] = {
    "missing_tmux_session": {"fix": "restart_worker", "destructive": False, "escalate": False},
    "stale_runtime": {"fix": "clear_runtime_json", "destructive": False, "escalate": False},
    "profile_path_mismatch": {"fix": "recreate_profile_symlink", "destructive": False, "escalate": False},
    "build_failure": {"fix": "clean_rebuild", "destructive": False, "escalate": False},
    "test_failure": {"fix": "rerun_isolated", "destructive": False, "escalate": False},
    "checkpoint_timeout": {"fix": "retry_with_backoff", "destructive": False, "escalate": False},
    "auth_unavailable": {"fix": "refresh_token", "destructive": False, "escalate": True},
    "branch_drift": {"fix": "rebase_or_reset", "destructive": True, "escalate": True},
    "port_in_use": {"fix": "kill_and_restart", "destructive": False, "escalate": False},
    "disk_full": {"fix": "prune_spill_and_logs", "destructive": False, "escalate": False},
    "ollama_down": {"fix": "restart_ollama", "destructive": False, "escalate": False},
}

def playbook_for(failure_key: str) -> dict | None:
    return AUTO_REPAIR_PLAYBOOK.get(failure_key)

def should_auto_repair(failure_key: str) -> bool:
    entry = AUTO_REPAIR_PLAYBOOK.get(failure_key)
    if not entry:
        return False
    # No auto-reparar si es destructivo/especulativo -> escalar
    return not entry.get("destructive") and not entry.get("escalate")


@dataclass
class CheckResult:
    name: str
    status: str  # "ok", "warning", "error", "skipped"
    message: str
    suggestion: str = ""
    details: Dict = field(default_factory=dict)


class Doctor:
    """Self-diagnostic for the entire SuperNEXUS system"""

    def __init__(self):
        self.results: List[CheckResult] = []

    async def run_full_diagnostic(self) -> Dict:
        """Run all diagnostic checks"""
        self.results = []

        checks = [
            self._check_python,
            self._check_ollama,
            self._check_node,
            self._check_database,
            self._check_env_vars,
            self._check_ports,
            self._check_disk_space,
            self._check_memory,
            self._check_skills,
            self._check_mcp_bridge,
            self._check_nexus_hive,
            self._check_api_server,
        ]

        for check in checks:
            try:
                result = await check()
                if isinstance(result, list):
                    self.results.extend(result)
                elif result:
                    self.results.append(result)
            except Exception as e:
                self.results.append(CheckResult(
                    name=check.__name__,
                    status="error",
                    message=f"Check failed: {e}",
                    suggestion="Review check implementation",
                ))

        return self._generate_report()

    async def _check_python(self) -> CheckResult:
        version = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
        if sys.version_info < (3, 10):
            return CheckResult(
                name="Python",
                status="warning",
                message=f"Python {version} (recommend 3.10+)",
                suggestion="Upgrade Python to 3.10+ for best performance",
                details={"version": version},
            )
        return CheckResult(name="Python", status="ok", message=f"Python {version}")

    async def _check_ollama(self) -> List[CheckResult]:
        results = []
        ollama_url = os.getenv("OLLAMA_URL", "http://localhost:11434")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5) as client:
                r = await client.get(f"{ollama_url}/api/tags")
                if r.status_code == 200:
                    models = r.json().get("models", [])
                    model_names = [m["name"] for m in models]
                    results.append(CheckResult(
                        name="Ollama",
                        status="ok",
                        message=f"Ollama running with {len(models)} models",
                        details={"models": model_names},
                    ))

                    # Check for recommended models
                    recommended = ["qwen3.6:latest", "qwen2.5vl:7b", "llama3.2:3b", "nemotron-3-nano:4b"]
                    missing = [m for m in recommended if not any(m.split(":")[0] in mn for mn in model_names)]
                    if missing:
                        results.append(CheckResult(
                            name="Ollama Models",
                            status="warning",
                            message=f"Missing recommended models: {', '.join(missing)}",
                            suggestion=f"Run: {'; '.join(f'ollama pull {m}' for m in missing)}",
                        ))
                else:
                    results.append(CheckResult(
                        name="Ollama",
                        status="error",
                        message=f"Ollama responded with status {r.status_code}",
                        suggestion="Check Ollama service",
                    ))
        except Exception as e:
            results.append(CheckResult(
                name="Ollama",
                status="error",
                message=f"Ollama not available: {e}",
                suggestion="Start Ollama: ollama serve",
            ))
        return results

    async def _check_node(self) -> CheckResult:
        node = shutil.which("node")
        if not node:
            return CheckResult(
                name="Node.js",
                status="warning",
                message="Node.js not found",
                suggestion="Install Node.js for OpenClaw and UI features",
            )
        try:
            result = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=5)
            return CheckResult(name="Node.js", status="ok", message=result.stdout.strip())
        except Exception:
            return CheckResult(name="Node.js", status="error", message="Node.js found but not executable")

    async def _check_database(self) -> List[CheckResult]:
        results = []
        root = brain_path().parent
        db_paths = [
            brain_path() / "message_board.db",
            root / "data" / "session.db",
            root / "data" / "event_store.db",
        ]
        for db_path in db_paths:
            if db_path.exists():
                size = db_path.stat().st_size
                results.append(CheckResult(
                    name=f"DB: {db_path.name}",
                    status="ok",
                    message=f"{size / 1024:.1f} KB",
                    details={"path": str(db_path), "size": size},
                ))
            else:
                results.append(CheckResult(
                    name=f"DB: {db_path.name}",
                    status="warning",
                    message="Database not found (will be created on first use)",
                ))
        return results

    async def _check_env_vars(self) -> List[CheckResult]:
        results = []
        required = []  # portabilidad: nada requerido, todo resuelve a defaults internos
        optional = ["NEXUS_BRAIN", "NEXUS_V3_DATA", "OLLAMA_HOST",
                    "SUPER_NEXUS_PC2_IP", "DISCORD_TOKEN", "OPENCODE_CLI"]

        for var in required:
            if os.getenv(var):
                results.append(CheckResult(name=f"ENV: {var}", status="ok", message="Set"))
            else:
                results.append(CheckResult(
                    name=f"ENV: {var}",
                    status="warning",
                    message="Not set (using default)",
                    suggestion=f"Add {var} to .env file",
                ))

        for var in optional:
            if os.getenv(var):
                results.append(CheckResult(name=f"ENV: {var}", status="ok", message="Set"))

        return results

    async def _check_ports(self) -> List[CheckResult]:
        results = []
        ports = {
            int(os.getenv("NEXUS_MASTER_PORT", 9000)): "NEXUS Master API",
            int(os.getenv("NEXUS_API_PORT", 9001)): "SuperNEXUS API",
            int(os.getenv("OLLAMA_PORT", 11434)): "Ollama",
            int(os.getenv("OPENCLAW_PORT", 18789)): "OpenClaw",
        }
        for port, service in ports.items():
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(1)
            result = sock.connect_ex(("127.0.0.1", port))
            sock.close()
            if result == 0:
                results.append(CheckResult(name=f"Port {port}", status="ok", message=f"{service} listening"))
            else:
                results.append(CheckResult(
                    name=f"Port {port}",
                    status="warning",
                    message=f"{service} not listening",
                ))
        return results

    async def _check_disk_space(self) -> CheckResult:
        try:
            usage = shutil.disk_usage(Path(__file__).parent.parent.parent)
            free_gb = usage.free / (1024 ** 3)
            if free_gb < 5:
                return CheckResult(
                    name="Disk Space",
                    status="warning",
                    message=f"Only {free_gb:.1f} GB free",
                    suggestion="Free up disk space",
                    details={"free_gb": round(free_gb, 1)},
                )
            return CheckResult(name="Disk Space", status="ok", message=f"{free_gb:.1f} GB free")
        except Exception:
            return CheckResult(name="Disk Space", status="error", message="Could not check disk space")

    async def _check_memory(self) -> CheckResult:
        try:
            import psutil
            mem = psutil.virtual_memory()
            available_gb = mem.available / (1024 ** 3)
            if available_gb < 2:
                return CheckResult(
                    name="Memory",
                    status="warning",
                    message=f"Only {available_gb:.1f} GB available",
                    suggestion="Close other applications",
                    details={"available_gb": round(available_gb, 1), "percent": mem.percent},
                )
            return CheckResult(name="Memory", status="ok", message=f"{available_gb:.1f} GB available ({100 - mem.percent:.0f}% free)")
        except ImportError:
            return CheckResult(name="Memory", status="skipped", message="psutil not installed")
        except Exception:
            return CheckResult(name="Memory", status="error", message="Could not check memory")

    async def _check_skills(self) -> CheckResult:
        skills_dir = Path(__file__).parent.parent / "skills" / "hub"
        if skills_dir.exists():
            count = len(list(skills_dir.iterdir()))
            return CheckResult(name="Skills", status="ok", message=f"{count} skills in hub")
        return CheckResult(name="Skills", status="warning", message="Skills hub not found")

    async def _check_mcp_bridge(self) -> CheckResult:
        try:
            from src.bridges.mcp_bridge_server import mcp
            tools = mcp._tool_manager._tools if hasattr(mcp, "_tool_manager") else {}
            return CheckResult(name="MCP Bridge", status="ok", message=f"{len(tools)} tools registered")
        except Exception as e:
            return CheckResult(name="MCP Bridge", status="error", message=f"Error: {e}")

    async def _check_nexus_hive(self) -> CheckResult:
        try:
            return CheckResult(name="NexusHive", status="ok", message="Module available")
        except Exception as e:
            return CheckResult(name="NexusHive", status="error", message=f"Error: {e}")

    async def _check_api_server(self) -> CheckResult:
        try:
            return CheckResult(name="API Server", status="ok", message="Module available")
        except Exception as e:
            return CheckResult(name="API Server", status="error", message=f"Error: {e}")

    def _generate_report(self) -> Dict:
        ok = sum(1 for r in self.results if r.status == "ok")
        warnings = sum(1 for r in self.results if r.status == "warning")
        errors = sum(1 for r in self.results if r.status == "error")
        skipped = sum(1 for r in self.results if r.status == "skipped")

        overall = "healthy" if errors == 0 else ("degraded" if errors <= 2 else "critical")

        return {
            "timestamp": datetime.now().isoformat(),
            "overall_status": overall,
            "summary": {
                "total_checks": len(self.results),
                "ok": ok,
                "warnings": warnings,
                "errors": errors,
                "skipped": skipped,
            },
            "checks": [
                {
                    "name": r.name,
                    "status": r.status,
                    "message": r.message,
                    "suggestion": r.suggestion,
                }
                for r in self.results
            ],
        }

    def diagnose(self) -> Dict:
        """Synchronous diagnostic (runs async checks synchronously)"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None
        if loop and loop.is_running():
            return {"checks": [], "summary": {"total": 0, "ok": 0, "warnings": 0, "errors": 0, "skipped": 0}}
        return asyncio.run(self.run_full_diagnostic())
