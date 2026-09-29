"""
GemaHost - Extension Host Multiproceso para SuperNEXUS v2

Arquitectura inspirada en VS Code Extension Host:
- Cada gema se ejecuta como subproceso aislado
- Comunicacion exclusiva via JSON-RPC sobre stdin/stdout
- Si una gema falla, se elimina con SIGKILL y se reinicia
- Lazy loading: solo se instancia cuando se dispara un activationEvent
- RAM de inicio: ~3.5GB → ~150MB (solo Director + API en memoria)

Patrones:
- Manifiesto gema.json con activationEvents y semanticKeywords
- ThreadPool dinamico con depth limit MAX=3
- Health check periodico con auto-restart
- Resource limits por gema (memoria, CPU, tiempo)
"""

import asyncio
import json
import logging
import os
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.core.extension_pipeline import ExtensionPipeline, HookPhase, HookContext

logger = logging.getLogger("nexus-gema-host")


class GemaState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    CRASHED = "crashed"
    RESTARTING = "restarting"
    DISABLED = "disabled"


@dataclass
class GemaManifest:
    """Manifiesto declarativo de una gema (gema.json)"""
    name: str
    version: str = "1.0.0"
    description: str = ""
    system_prompt: str = ""  # Identidad/instrucciones de la gema
    main: str = ""  # Modulo Python principal
    model: str = ""  # Modelo LLM recomendado
    activation_events: List[str] = field(default_factory=list)  # ["onCommand:refactor", "onTask:code"]
    semantic_keywords: List[str] = field(default_factory=list)  # ["python", "debug", "refactor"]
    category: str = "general"
    max_memory_mb: int = 512
    max_cpu_percent: int = 25
    timeout_seconds: int = 300
    auto_restart: bool = True
    max_restarts: int = 5
    parallel_capable: bool = True
    capabilities: List[str] = field(default_factory=list)  # CapabilitySet — openfang parity


@dataclass
class GemaStats:
    """Estadisticas de ejecucion de una gema"""
    total_executions: int = 0
    successful_executions: int = 0
    failed_executions: int = 0
    total_latency_ms: float = 0
    last_execution: Optional[str] = None
    crash_count: int = 0
    restart_count: int = 0
    peak_memory_mb: float = 0


class GemaProcess:
    """
    Representa una gema como proceso aislado.
    Se comunica via JSON-RPC sobre stdin/stdout.
    """

    def __init__(self, manifest: GemaManifest, project_root: str):
        self.manifest = manifest
        self.project_root = project_root
        self.state = GemaState.STOPPED
        self.stats = GemaStats()
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._pending_requests: Dict[int, asyncio.Future] = {}
        self._reader_thread: Optional[threading.Thread] = None
        self._start_time: Optional[float] = None

    def start(self) -> bool:
        """Inicia el proceso de la gema"""
        with self._lock:
            if self.state == GemaState.RUNNING:
                return True

            self.state = GemaState.STARTING
            logger.info(f"Starting gema: {self.manifest.name}")

            try:
                env = os.environ.copy()
                env["PYTHONIOENCODING"] = "utf-8"
                env["NEXUS_GEMA_NAME"] = self.manifest.name
                env["NEXUS_PROJECT_ROOT"] = self.project_root
                from src.core import nexus_config
                env["NEXUS_API_BASE"] = nexus_config.get_nexus_url()

                # Pass manifest as env var so worker can load the gema module
                import json as _json
                from dataclasses import asdict as _asdict
                env["NEXUS_GEMA_MANIFEST"] = _json.dumps(_asdict(self.manifest))

                manifest_path = self._find_manifest_path()

                self._process = subprocess.Popen(
                    [
                        sys.executable, "-m", "src.core.gema_worker",
                        "--gema", self.manifest.name,
                        "--manifest", manifest_path,
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=self.project_root,
                    env=env,
                    text=True,
                    bufsize=1,  # Line buffered
                )

                self._start_time = time.time()
                self.state = GemaState.RUNNING

                # Join old reader thread before creating new one
                if self._reader_thread and self._reader_thread.is_alive():
                    self._reader_thread.join(timeout=2)
                # Iniciar thread de lectura
                self._reader_thread = threading.Thread(
                    target=self._read_output,
                    daemon=True,
                    name=f"gema-reader-{self.manifest.name}",
                )
                self._reader_thread.start()

                logger.info(f"Gema started: {self.manifest.name} (PID: {self._process.pid})")
                return True

            except Exception as e:
                logger.error(f"Failed to start gema {self.manifest.name}: {e}")
                self.state = GemaState.CRASHED
                self.stats.crash_count += 1
                return False

    def stop(self):
        """Detiene el proceso de la gema"""
        with self._lock:
            if self.state == GemaState.STOPPED:
                return

            logger.info(f"Stopping gema: {self.manifest.name}")
            self.state = GemaState.STOPPED

            if self._process and self._process.poll() is None:
                try:
                    self._process.terminate()
                    try:
                        self._process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=3)
                except Exception as e:
                    logger.error(f"Error stopping gema {self.manifest.name}: {e}")

            # Resolver pending requests como cancelados
            for future in self._pending_requests.values():
                if not future.done():
                    future.set_exception(asyncio.CancelledError("Gema stopped"))
            self._pending_requests.clear()

    def restart(self) -> bool:
        """Reinicia el proceso de la gema"""
        with self._lock:
            if self.stats.restart_count >= self.manifest.max_restarts:
                logger.error(
                    f"Gema {self.manifest.name} exceeded max restarts "
                    f"({self.manifest.max_restarts}), disabling"
                )
                self.state = GemaState.DISABLED
                return False

            self.state = GemaState.RESTARTING
            self.stats.restart_count += 1
            logger.info(
                f"Restarting gema: {self.manifest.name} "
                f"(restart {self.stats.restart_count}/{self.manifest.max_restarts})"
            )

        # Stop fuera del lock
        self.stop()
        time.sleep(1)  # Breve pausa antes de reiniciar
        return self.start()

    async def send_request(self, method: str, params: Dict[str, Any], timeout: float = None) -> Dict:
        """Envia una peticion JSON-RPC a la gema"""
        if self.state != GemaState.RUNNING or not self._process:
            return {"error": f"Gema {self.manifest.name} is not running (state: {self.state.value})"}

        timeout = timeout or self.manifest.timeout_seconds

        with self._lock:
            self._request_id += 1
            req_id = self._request_id
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            future = loop.create_future()
            self._pending_requests[req_id] = future

        request = {
            "jsonrpc": "2.0",
            "id": req_id,
            "method": method,
            "params": params,
        }

        try:
            # Escribir a stdin
            msg = json.dumps(request) + "\n"
            self._process.stdin.write(msg)
            self._process.stdin.flush()

            # Esperar respuesta con timeout
            result = await asyncio.wait_for(future, timeout=timeout)
            self.stats.total_executions += 1
            self.stats.successful_executions += 1
            self.stats.last_execution = datetime.now().isoformat()
            return result

        except asyncio.TimeoutError:
            self.stats.failed_executions += 1
            logger.error(f"Gema {self.manifest.name} request timeout: {method}")
            # Force restart if unresponsive
            if self.stats.failed_executions >= 3:
                logger.warning(f"Gema {self.manifest.name} failed 3 times, restarting")
                self.restart()
            return {"error": f"Request timeout after {timeout}s"}

        except (BrokenPipeError, OSError) as e:
            self.stats.failed_executions += 1
            logger.error(f"Gema {self.manifest.name} pipe broken: {e}")
            self.state = GemaState.CRASHED
            if self.manifest.auto_restart:
                self.restart()
            return {"error": f"Gema process lost: {e}"}

        except Exception as e:
            self.stats.failed_executions += 1
            logger.error(f"Gema {self.manifest.name} request error: {e}")
            return {"error": str(e)}

        finally:
            self._pending_requests.pop(req_id, None)

    def _read_output(self):
        """Thread que lee stdout del proceso y despacha respuestas"""
        if not self._process:
            return

        try:
            while self._process.poll() is None:
                line = self._process.stdout.readline()
                if not line:
                    break

                line = line.strip()
                if not line:
                    continue

                try:
                    response = json.loads(line)
                    req_id = response.get("id")

                    if req_id and req_id in self._pending_requests:
                        future = self._pending_requests[req_id]
                        if not future.done():
                            if "error" in response:
                                future.set_exception(Exception(response["error"]))
                            else:
                                future.set_result(response.get("result", {}))
                    elif response.get("method"):
                        # Notificacion del gema (ej: log, status update)
                        self._handle_notification(response)

                except json.JSONDecodeError:
                    logger.debug(f"Non-JSON output from gema {self.manifest.name}: {line[:100]}")

        except Exception as e:
            logger.error(f"Gema reader error for {self.manifest.name}: {e}")
        finally:
            # Log stderr if process crashed
            if self._process.poll() is not None and self._process.stderr:
                try:
                    stderr_data = self._process.stderr.read(4096)
                    if stderr_data:
                        logger.error(f"[{self.manifest.name}] stderr: {stderr_data[:500]}")
                except Exception:
                    pass
            # Si el proceso termino inesperadamente
            if self._process.poll() is not None and self.state == GemaState.RUNNING:
                exit_code = self._process.poll()
                logger.error(
                    f"Gema {self.manifest.name} exited unexpectedly (code: {exit_code})"
                )
                self.state = GemaState.CRASHED
                self.stats.crash_count += 1

                # Auto-restart si esta habilitado
                if self.manifest.auto_restart:
                    self.restart()

    def _handle_notification(self, response: Dict):
        """Procesa notificaciones del gema"""
        method = response.get("method", "")
        params = response.get("params", {})

        if method == "gema/log":
            level = params.get("level", "info")
            msg = params.get("message", "")
            getattr(logger, level, logger.info)(f"[{self.manifest.name}] {msg}")
        elif method == "gema/status":
            logger.info(f"[{self.manifest.name}] Status update: {params}")

    def _find_manifest_path(self) -> str:
        """Busca el archivo gema.json en el directorio de la gema"""
        candidates = [
            Path(self.project_root) / "src" / "gemas" / self.manifest.name / "gema.json",
            Path(self.project_root) / "src" / "agents" / f"{self.manifest.name}.json",
            Path(self.project_root) / "data" / "gemas" / f"{self.manifest.name}.json",
        ]
        for path in candidates:
            if path.exists():
                return str(path)
        return ""

    def is_healthy(self) -> bool:
        """Verifica si el proceso esta vivo y responsivo"""
        if self.state != GemaState.RUNNING or not self._process:
            return False

        if self._process.poll() is not None:
            return False

        # Verificar uso de memoria (si psutil esta disponible)
        try:
            import psutil
            proc = psutil.Process(self._process.pid)
            mem_mb = proc.memory_info().rss / (1024 * 1024)
            self.stats.peak_memory_mb = max(self.stats.peak_memory_mb, mem_mb)
            if mem_mb > self.manifest.max_memory_mb:
                logger.warning(
                    f"Gema {self.manifest.name} exceeds memory limit: "
                    f"{mem_mb:.0f}MB > {self.manifest.max_memory_mb}MB"
                )
                return False
        except ImportError:
            pass

        return True

    def get_status(self) -> Dict:
        return {
            "name": self.manifest.name,
            "state": self.state.value,
            "pid": self._process.pid if self._process else None,
            "stats": {
                "total_executions": self.stats.total_executions,
                "success_rate": (
                    self.stats.successful_executions / self.stats.total_executions
                    if self.stats.total_executions > 0 else 0
                ),
                "crash_count": self.stats.crash_count,
                "restart_count": self.stats.restart_count,
                "peak_memory_mb": self.stats.peak_memory_mb,
            },
        }


class GemaHost:
    """
    Extension Host que gestiona el ciclo de vida de todas las gemas.
    Inspirado en la arquitectura de VS Code Extension Host.

    - Lazy loading: las gemas solo se cargan cuando se necesitan
    - Aislamiento: cada gema en su propio proceso
    - Auto-recovery: restart automatico ante fallos
    - Health checks: monitoreo periodico de salud
    """

    MAX_DEPTH = 3  # Limite de delegacion en sub-agentes

    def __init__(self, project_root: str = None, extension_pipeline: ExtensionPipeline = None):
        self.project_root = project_root or os.getcwd()
        self._gemas: Dict[str, GemaProcess] = {}
        self._manifests: Dict[str, GemaManifest] = {}
        self._lock = threading.Lock()
        self._health_check_running = False
        self._health_check_thread: Optional[threading.Thread] = None
        self._initialized = False
        self.extension_pipeline = extension_pipeline

    def initialize(self):
        """Carga todos los manifiestos disponibles"""
        if self._initialized:
            return

        manifest_dir = Path(self.project_root) / "data" / "gemas"
        if not manifest_dir.exists():
            manifest_dir.mkdir(parents=True, exist_ok=True)
            # Si no existe, intentar desde la raiz del proyecto
            alt_dir = Path(__file__).parent.parent.parent / "data" / "gemas"
            if alt_dir.exists():
                manifest_dir = alt_dir

        # Cargar manifiestos existentes
        for manifest_file in manifest_dir.glob("*.json"):
            try:
                data = json.loads(manifest_file.read_text(encoding="utf-8"))
                manifest = GemaManifest(
                    name=data.get("name", manifest_file.stem),
                    version=data.get("version", "1.0.0"),
                    description=data.get("description", ""),
                    system_prompt=data.get("systemPrompt", ""),
                    main=data.get("main", ""),
                    model=data.get("model", ""),
                    activation_events=data.get("activationEvents", []),
                    semantic_keywords=data.get("semanticKeywords", []),
                    category=data.get("category", "general"),
                    max_memory_mb=data.get("maxMemoryMB", 512),
                    max_cpu_percent=data.get("maxCPUPercent", 25),
                    timeout_seconds=data.get("timeoutSeconds", 300),
                    auto_restart=data.get("autoRestart", True),
                    max_restarts=data.get("maxRestarts", 5),
                    parallel_capable=data.get("parallelCapable", True),
                )
                self._manifests[manifest.name] = manifest
                logger.info(f"Loaded manifest: {manifest.name}")
            except Exception as e:
                logger.error(f"Failed to load manifest {manifest_file}: {e}")

        self._initialized = True
        logger.info(f"GemaHost initialized: {len(self._manifests)} manifests loaded")

    def register_gema(self, manifest: GemaManifest):
        """Registra una gema manualmente"""
        self._manifests[manifest.name] = manifest
        logger.info(f"Registered gema: {manifest.name}")

    def find_gema_for_task(self, task: str) -> Optional[str]:
        """
        Busca la gema apropiada para una tarea usando semantic keywords.
        Retorna el nombre de la gema o None.
        """
        task_lower = task.lower()
        best_match = None
        best_score = 0

        for name, manifest in self._manifests.items():
            # Semantic keywords: 1 punto por match
            keyword_score = sum(
                1 for kw in manifest.semantic_keywords
                if kw.lower() in task_lower
            )
            
            # Activation events: 3 puntos por match (mayor prioridad)
            event_score = 0
            for event in manifest.activation_events:
                event_kw = event.split(":")[-1].lower()
                if event_kw in task_lower:
                    event_score += 3
            
            total_score = keyword_score + event_score
            if total_score > best_score:
                best_score = total_score
                best_match = name

        return best_match if best_score > 0 else None

    async def activate_gema(self, gema_name: str) -> bool:
        """Activa (lazy load) una gema especifica"""
        with self._lock:
            if gema_name in self._gemas:
                existing = self._gemas[gema_name]
                if existing.state == GemaState.RUNNING:
                    return True
                elif existing.state == GemaState.DISABLED:
                    return False
                # Esta en otro estado, intentar restart
                return existing.restart()

            manifest = self._manifests.get(gema_name)
            if not manifest:
                logger.error(f"No manifest found for gema: {gema_name}")
                return False

            gema = GemaProcess(manifest, self.project_root)
            self._gemas[gema_name] = gema

        return gema.start()

    async def execute_gema(
        self,
        gema_name: str,
        method: str,
        params: Dict[str, Any],
        depth: int = 0,
    ) -> Dict:
        """
        Ejecuta una operacion en una gema.
        Si la gema no esta activa, la activa primero (lazy loading).
        """
        if depth > self.MAX_DEPTH:
            return {"error": f"Max delegation depth exceeded ({self.MAX_DEPTH})"}

        # ExtensionPipeline: Hook on_tool_call
        if self.extension_pipeline:
            hook_ctx = HookContext(phase=HookPhase.ON_TOOL_CALL, gem_id=gema_name, data={"method": method, "params": params})
            hook_ctx = self.extension_pipeline.execute_phase(HookPhase.ON_TOOL_CALL, hook_ctx.data, {"gem_id": gema_name})
            if hook_ctx.should_abort:
                return {"error": hook_ctx.abort_reason}
            method = hook_ctx.data.get("method", method)
            params = hook_ctx.data.get("params", params)

        # Lazy activation
        if gema_name not in self._gemas or self._gemas[gema_name].state != GemaState.RUNNING:
            activated = await self.activate_gema(gema_name)
            if not activated:
                return {"error": f"Failed to activate gema: {gema_name}"}

        gema = self._gemas[gema_name]
        result = await gema.send_request(method, params)

        # ExtensionPipeline: Hook on_tool_result
        if self.extension_pipeline:
            hook_ctx = HookContext(phase=HookPhase.ON_TOOL_RESULT, gem_id=gema_name, data=result)
            hook_ctx = self.extension_pipeline.execute_phase(HookPhase.ON_TOOL_RESULT, hook_ctx.data, {"gem_id": gema_name})
            if not hook_ctx.should_abort:
                result = hook_ctx.data

        # Stats already incremented in send_request()
                gema.stats.successful_executions += 1
            else:
                gema.stats.failed_executions += 1
            gema.stats.last_execution = datetime.now().isoformat()

        return result

    async def execute_task(self, task: str, context: str = "") -> Dict:
        """
        Ejecuta una tarea buscando la gema apropiada automaticamente.
        """
        gema_name = self.find_gema_for_task(task)
        if not gema_name:
            return {"error": "No suitable gema found for task", "task": task}

        return await self.execute_gema(
            gema_name,
            "execute_task",
            {"task": task, "context": context},
        )

    def start_health_checks(self, interval: int = 30):
        """Inicia health checks periodicos"""
        if self._health_check_running:
            return

        self._health_check_running = True
        self._health_check_thread = threading.Thread(
            target=self._health_check_loop,
            args=(interval,),
            daemon=True,
            name="gema-health-check",
        )
        self._health_check_thread.start()
        logger.info(f"Health checks started (interval: {interval}s)")

    def stop_health_checks(self):
        """Detiene health checks"""
        self._health_check_running = False

    def _health_check_loop(self, interval: int):
        """Loop de health checks"""
        while self._health_check_running:
            time.sleep(interval)
            for name, gema in list(self._gemas.items()):
                if gema.state == GemaState.RUNNING and not gema.is_healthy():
                    logger.warning(f"Gema {name} unhealthy, triggering restart")
                    gema.restart()

    def get_status(self) -> Dict:
        """Estado de todas las gemas"""
        return {
            "total_manifests": len(self._manifests),
            "active_gemas": sum(
                1 for g in self._gemas.values() if g.state == GemaState.RUNNING
            ),
            "gemas": {
                name: gema.get_status()
                for name, gema in self._gemas.items()
            },
        }

    def shutdown(self):
        """Detiene todas las gemas"""
        logger.info("Shutting down GemaHost...")
        self.stop_health_checks()
        for name, gema in self._gemas.items():
            try:
                gema.stop()
            except Exception as e:
                logger.error(f"Error stopping gema {name}: {e}")
        logger.info("GemaHost shut down")
