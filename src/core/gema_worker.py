"""
GemaWorker - Proceso aislado para cada gema

Se ejecuta como subproceso independiente y se comunica con el GemaHost
via JSON-RPC sobre stdin/stdout.

Uso:
    python -m src.core.gema_worker --gema code --manifest path/to/gema.json

Entrada (stdin):  {"jsonrpc": "2.0", "id": 1, "method": "execute_task", "params": {...}}
Salida (stdout):  {"jsonrpc": "2.0", "id": 1, "result": {"content": "..."}}
"""

import asyncio
import json
import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

# Configurar logging para stderr (no stdout, que es para JSON-RPC)
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
)
logger = logging.getLogger("gema-worker")


import os, json

class GemaWorker:
    """
    Worker aislado para una gema especifica.
    Procesa peticiones JSON-RPC y devuelve resultados.
    """

    def __init__(self, gema_name: str, manifest_path: str = ""):
        self.gema_name = gema_name
        self.manifest_path = manifest_path
        self._running = True
        self._request_count = 0

        # Cargar manifiesto: primero JSON file, luego env var NEXUS_GEMA_MANIFEST
        self.manifest = {}
        # Try env var first (set by GemaHost)
        manifest_data = os.environ.get("NEXUS_GEMA_MANIFEST", "")
        if manifest_data:
            try:
                self.manifest = json.loads(manifest_data)
            except Exception as e:
                logger.error(f"Failed to load manifest from env: {e}")
        # Fallback to file
        if not self.manifest and manifest_path and Path(manifest_path).exists():
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    self.manifest = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load manifest: {e}")

        # Fallback: load from env var (set by GemaHost)
        if not self.manifest:
            import os
            env_manifest = os.environ.get("NEXUS_GEMA_MANIFEST", "")
            if env_manifest:
                try:
                    self.manifest = json.loads(env_manifest)
                except Exception as e:
                    logger.error(f"Failed to load manifest from env: {e}")

        # Inicializar modulo de la gema
        self._gema_instance = self._load_gema_module()

    def _load_gema_module(self) -> Optional[Any]:
        """Carga el modulo Python de la gema"""
        try:
            # Intentar cargar desde src/gemas/{name}/ o src/agents/{name}_gem.py
            gema_module_name = self.manifest.get("main", "")
            if gema_module_name:
                import importlib
                module = importlib.import_module(gema_module_name)
                # Buscar clase principal (excluir base LLMGema)
                from src.plugins.gemas.llm_base import LLMGema
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and "Gem" in attr_name and attr is not LLMGema:
                        return attr()
                # Fallback: buscar con "Gem" sin filtro
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and "Gem" in attr_name:
                        return attr()
                return module
            else:
                # Fallback: intentar cargar desde src/agents
                agent_module = f"src.agents.{self.gema_name}_gem"
                import importlib
                module = importlib.import_module(agent_module)
                for attr_name in dir(module):
                    attr = getattr(module, attr_name)
                    if isinstance(attr, type) and "Gem" in attr_name:
                        return attr()
                return module
        except Exception as e:
            logger.warning(f"Could not load gema module for {self.gema_name}: {e}")
            return None

    async def handle_request(self, request: Dict) -> Dict:
        """Procesa una peticion JSON-RPC"""
        method = request.get("method", "")
        params = request.get("params", {})
        req_id = request.get("id")

        self._request_count += 1
        start = time.time()

        try:
            if method == "execute_task":
                result = await self._execute_task(params)
            elif method == "get_status":
                result = self._get_status()
            elif method == "health_check":
                result = {"status": "healthy", "requests": self._request_count}
            else:
                result = {"error": f"Unknown method: {method}"}

            elapsed = (time.time() - start) * 1000
            result.setdefault("metadata", {})["execution_ms"] = round(elapsed, 2)

            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "result": result,
            }

        except Exception as e:
            logger.error(f"Error handling request {method}: {e}")
            return {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": str(e),
            }

    async def _execute_task(self, params: Dict) -> Dict:
        """Ejecuta una tarea en la gema"""
        task = params.get("task", "")
        context = params.get("context", "")

        if not task:
            return {"error": "Task is required"}

        # Si la gema tiene un metodo execute o process, usarlo
        if self._gema_instance:
            if hasattr(self._gema_instance, "execute"):
                result = self._gema_instance.execute(task)
                if asyncio.iscoroutine(result):
                    result = await result
                return result
            elif hasattr(self._gema_instance, "process"):
                result = self._gema_instance.process(task)
                if asyncio.iscoroutine(result):
                    result = await result
                return result
            elif hasattr(self._gema_instance, "organize"):
                # Biblioteca gem pattern
                result = self._gema_instance.organize(
                    title=task[:100],
                    content=task,
                    category=context or "general",
                )
                if asyncio.iscoroutine(result):
                    result = await result
                return result

        # Fallback: respuesta basica
        return {
            "gema": self.gema_name,
            "task": task,
            "status": "processed",
            "note": "Gema module not fully implemented",
        }

    def _get_status(self) -> Dict:
        """Estado del worker"""
        return {
            "gema": self.gema_name,
            "requests_handled": self._request_count,
            "module_loaded": self._gema_instance is not None,
            "manifest": self.manifest.get("name", ""),
        }

    def send_notification(self, method: str, params: Dict):
        """Envia una notificacion al host (no espera respuesta)"""
        notification = {
            "jsonrpc": "2.0",
            "method": method,
            "params": params,
        }
        sys.stdout.write(json.dumps(notification) + "\n")
        sys.stdout.flush()

    def run(self):
        """Loop principal del worker (lee stdin, procesa, escribe stdout)"""
        logger.info(f"GemaWorker started: {self.gema_name}")
        self.send_notification("gema/status", {"status": "started", "gema": self.gema_name})

        # Configurar signal handlers
        def handle_signal(signum, frame):
            logger.info(f"Received signal {signum}, shutting down")
            self._running = False

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        while self._running:
            try:
                line = sys.stdin.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    request = json.loads(line)
                    # Ejecutar en event loop (reusar si existe)
                    try:
                        loop = asyncio.get_event_loop()
                        if loop.is_closed():
                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                    response = loop.run_until_complete(self.handle_request(request))

                    sys.stdout.write(json.dumps(response) + "\n")
                    sys.stdout.flush()

                except json.JSONDecodeError as e:
                    logger.warning(f"Invalid JSON received: {e}")
                    # No escribir nada a stdout para no romper el protocolo

            except Exception as e:
                logger.error(f"Worker error: {e}")
                self.send_notification("gema/log", {"level": "error", "message": str(e)})

        self.send_notification("gema/status", {"status": "stopped", "gema": self.gema_name})
        logger.info(f"GemaWorker stopped: {self.gema_name}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="GemaWorker - Isolated gema process")
    parser.add_argument("--gema", required=True, help="Gema name")
    parser.add_argument("--manifest", default="", help="Path to gema.json manifest")
    args = parser.parse_args()

    worker = GemaWorker(args.gema, args.manifest)
    worker.run()


if __name__ == "__main__":
    main()
