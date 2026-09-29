"""
AutopilotService — triggers cron/webhook/conditional para automatización recurrente

Sobre la base de BackgroundWorkerManager (intervalos fijos), añade:
  - CronTrigger: expresiones cron como "*/5 * * * *" o "0 9 * * 1-5"
  - WebhookTrigger: endpoints HTTP locales que activan autopilots
  - ConditionalTrigger: disparo cuando una condición evalúa True
  - Persistencia SQLite de definiciones de autopilots

Uso:
    autopilot = AutopilotService(worker_manager, realtime_hub)
    await autopilot.start()

    # Cron: cada 5 minutos
    await autopilot.register(AutopilotDef(
        name="backup_db",
        trigger=CronTrigger("*/5 * * * *"),
        task_type="backup.database",
    ))

    # Webhook: POST /hooks/deploy → ejecuta deploy
    await autopilot.register(AutopilotDef(
        name="webhook_deploy",
        trigger=WebhookTrigger(path="/hooks/deploy", secret="abc123"),
        task_type="deploy.stack",
    ))

    # Conditional: si hay errores recurrentes, ejecuta análisis
    await autopilot.register(AutopilotDef(
        name="error_analyzer",
        trigger=ConditionalTrigger(condition_fn=lambda ctx: ctx.get("error_rate", 0) > 0.1),
        task_type="analysis.errors",
    ))
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("nexus-autopilot")

_ULID_COUNTER = 0


def _ulid() -> str:
    global _ULID_COUNTER
    _ULID_COUNTER += 1
    ts = int(time.time() * 1000)
    return f"{ts:012x}{_ULID_COUNTER:08x}"


class TriggerType(Enum):
    CRON = "cron"
    WEBHOOK = "webhook"
    CONDITIONAL = "conditional"
    EVENT = "event"
    MANUAL = "manual"


@dataclass
class CronTrigger:
    """Trigger por expresión cron. Soporta 5 campos: min hour day month weekday"""
    expression: str  # "*/5 * * * *", "0 9 * * 1-5"
    timezone: str = "UTC"

    def __post_init__(self):
        self._parsed = self._parse(self.expression)

    @staticmethod
    def _parse(expr: str) -> List[List[int]]:
        fields = expr.strip().split()
        if len(fields) != 5:
            raise ValueError(f"Cron must have 5 fields, got {len(fields)}: {expr}")
        names = ["minute", "hour", "day", "month", "weekday"]
        parsed = []
        for i, (fld, name) in enumerate(zip(fields, names)):
            parsed.append(CronTrigger._parse_field(fld, i))
        return parsed

    @staticmethod
    def _parse_field(field: str, pos: int) -> List[int]:
        ranges = [(0, 59), (0, 23), (1, 31), (1, 12), (0, 6)]
        lo, hi = ranges[pos]

        if field == "*":
            return list(range(lo, hi + 1))

        values = set()
        for part in field.split(","):
            if "/" in part:
                base, step = part.split("/")
                step = int(step)
                if base == "*":
                    base_range = range(lo, hi + 1)
                elif "-" in base:
                    a, b = base.split("-")
                    base_range = range(int(a), int(b) + 1)
                else:
                    base_range = range(int(base), hi + 1)
                values.update(base_range[::step])
            elif "-" in part:
                a, b = part.split("-")
                values.update(range(int(a), int(b) + 1))
            else:
                values.add(int(part))

        return sorted(v for v in values if lo <= v <= hi)

    def matches(self, dt: Optional[datetime] = None) -> bool:
        dt = dt or datetime.now()
        parts = [dt.minute, dt.hour, dt.day, dt.month, dt.weekday()]
        return all(p in vals for p, vals in zip(parts, self._parsed))

    def next_run(self, after: Optional[datetime] = None) -> Optional[datetime]:
        after = after or datetime.now()
        for _ in range(525600):
            after = after + timedelta(minutes=1)
            if self.matches(after):
                return after
        return None

    def to_dict(self) -> Dict:
        return {"type": "cron", "expression": self.expression}

    @classmethod
    def from_dict(cls, data: Dict) -> "CronTrigger":
        return cls(expression=data["expression"])


@dataclass
class WebhookTrigger:
    """Trigger por webhook HTTP. Se registra un endpoint en /hooks/{path}"""
    path: str
    secret: str = ""
    method: str = "POST"

    def verify_signature(self, payload: bytes, signature: str) -> bool:
        if not self.secret:
            return True
        expected = hmac.new(
            self.secret.encode(),
            payload,
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(expected, signature)

    def to_dict(self) -> Dict:
        return {"type": "webhook", "path": self.path, "method": self.method}

    @classmethod
    def from_dict(cls, data: Dict) -> "WebhookTrigger":
        return cls(path=data["path"], secret=data.get("secret", ""), method=data.get("method", "POST"))


@dataclass
class ConditionalTrigger:
    """Trigger condicional. condition_fn recibe context y retorna bool"""
    condition_fn: Callable[[Dict], bool]
    description: str = ""

    def evaluate(self, context: Dict) -> bool:
        try:
            return self.condition_fn(context)
        except Exception as e:
            logger.warning(f"Conditional trigger error: {e}")
            return False

    def to_dict(self) -> Dict:
        return {"type": "conditional", "description": self.description}

    @classmethod
    def from_dict(cls, data: Dict) -> "ConditionalTrigger":
        return cls(condition_fn=lambda ctx: False, description=data.get("description", ""))


@dataclass
class EventTrigger:
    """Trigger por evento del RealtimeHub"""
    event_type: str
    scope_pattern: str = "*"

    def to_dict(self) -> Dict:
        return {"type": "event", "event_type": self.event_type, "scope_pattern": self.scope_pattern}

    @classmethod
    def from_dict(cls, data: Dict) -> "EventTrigger":
        return cls(event_type=data["event_type"], scope_pattern=data.get("scope_pattern", "*"))


@dataclass
class AutopilotDef:
    """Definición de un autopilot"""
    id: str = field(default_factory=_ulid)
    name: str = ""
    trigger: Any = None  # CronTrigger | WebhookTrigger | ConditionalTrigger | EventTrigger
    task_type: str = ""  # "backup.database", "analysis.errors", etc.
    task_params: Dict[str, Any] = field(default_factory=dict)
    enabled: bool = True
    cooldown_seconds: int = 60
    created_at: float = field(default_factory=time.time)
    last_fired: Optional[float] = None
    fire_count: int = 0
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def trigger_type(self) -> TriggerType:
        if isinstance(self.trigger, CronTrigger):
            return TriggerType.CRON
        if isinstance(self.trigger, WebhookTrigger):
            return TriggerType.WEBHOOK
        if isinstance(self.trigger, ConditionalTrigger):
            return TriggerType.CONDITIONAL
        if isinstance(self.trigger, EventTrigger):
            return TriggerType.EVENT
        return TriggerType.MANUAL

    def can_fire(self) -> bool:
        if not self.enabled:
            return False
        if self.last_fired and (time.time() - self.last_fired) < self.cooldown_seconds:
            return False
        return True

    def to_dict(self) -> Dict:
        d = {
            "id": self.id,
            "name": self.name,
            "trigger": self.trigger.to_dict() if hasattr(self.trigger, 'to_dict') else {"type": "manual"},
            "task_type": self.task_type,
            "task_params": self.task_params,
            "enabled": self.enabled,
            "cooldown_seconds": self.cooldown_seconds,
            "created_at": self.created_at,
            "last_fired": self.last_fired,
            "fire_count": self.fire_count,
        }
        return d


class AutopilotStore:
    """Persistencia SQLite de definiciones de autopilots"""

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            db_path = str(Path.home() / ".nexus" / "autopilots.db")
        self.db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""CREATE TABLE IF NOT EXISTS autopilots (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                trigger_type TEXT NOT NULL,
                trigger_data TEXT NOT NULL,
                task_type TEXT NOT NULL,
                task_params TEXT DEFAULT '{}',
                enabled INTEGER DEFAULT 1,
                cooldown_seconds INTEGER DEFAULT 60,
                created_at REAL DEFAULT (strftime('%s', 'now')),
                last_fired REAL,
                fire_count INTEGER DEFAULT 0,
                metadata TEXT DEFAULT '{}'
            )""")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_autopilot_type ON autopilots(trigger_type)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_autopilot_enabled ON autopilots(enabled)")

    def save(self, defn: AutopilotDef):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                """INSERT OR REPLACE INTO autopilots
                (id, name, trigger_type, trigger_data, task_type, task_params,
                 enabled, cooldown_seconds, last_fired, fire_count, metadata)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    defn.id,
                    defn.name,
                    defn.trigger_type.value,
                    json.dumps(defn.trigger.to_dict() if hasattr(defn.trigger, 'to_dict') else {}),
                    defn.task_type,
                    json.dumps(defn.task_params),
                    1 if defn.enabled else 0,
                    defn.cooldown_seconds,
                    defn.last_fired,
                    defn.fire_count,
                    json.dumps(defn.metadata),
                ),
            )

    def load_all(self) -> List[Dict]:
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM autopilots ORDER BY created_at DESC").fetchall()
            return [dict(r) for r in rows]

    def delete(self, autopilot_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("DELETE FROM autopilots WHERE id = ?", (autopilot_id,))

    def update_fire(self, autopilot_id: str):
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "UPDATE autopilots SET last_fired = ?, fire_count = fire_count + 1 WHERE id = ?",
                (time.time(), autopilot_id),
            )


class WebhookServer:
    """Mini servidor HTTP para webhooks (sin dependencias externas)"""

    def __init__(self, host: str = "127.0.0.1", port: int = 9789):
        self.host = host
        self.port = port
        self._routes: Dict[str, Callable] = {}
        self._server: Optional[asyncio.AbstractServer] = None

    def register(self, path: str, handler: Callable):
        self._routes[path] = handler

    def unregister(self, path: str):
        self._routes.pop(path, None)

    async def start(self):
        self._server = await asyncio.start_server(
            self._handle_connection,
            host=self.host,
            port=self.port,
        )
        logger.info(f"Webhook server listening on {self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        try:
            data = await reader.read(65536)
            request = data.decode("utf-8", errors="replace")
            lines = request.split("\r\n")
            if not lines:
                return

            method, path, _ = lines[0].split(" ", 2)
            body_start = request.find("\r\n\r\n") + 4
            body = request[body_start:] if body_start > 4 else ""

            handler = self._routes.get(path)
            if not handler:
                await self._respond(writer, 404, '{"error":"not found"}')
                return

            result = await handler({"method": method, "path": path, "body": body})
            await self._respond(writer, 200, json.dumps(result))

        except Exception as e:
            logger.warning(f"Webhook request error: {e}")
            await self._respond(writer, 500, f'{{"error":"{e}"}}')
        finally:
            writer.close()

    async def _respond(self, writer: asyncio.StreamWriter, status: int, body: str):
        response = (
            f"HTTP/1.1 {status} {'OK' if status == 200 else 'Error'}\r\n"
            f"Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Connection: close\r\n"
            f"\r\n"
            f"{body}"
        )
        writer.write(response.encode())
        await writer.drain()


class AutopilotService:
    """
    Servicio de autopilots. Añade cron/webhook/conditional triggers
    sobre el sistema existente de BackgroundWorkerManager.

    Flujo:
      1. Cron loop: cada 60s evalúa todos los autopilots CRON
      2. Webhook server: escucha en puerto 9789 endpoints registrados
      3. Event listener: escucha eventos del RealtimeHub para triggers EVENT
      4. Conditional eval: periódicamente evalúa conditions
    """

    def __init__(
        self,
        worker_manager=None,
        realtime_hub=None,
        db_path: Optional[str] = None,
        webhook_port: int = 9789,
    ):
        self.worker_manager = worker_manager
        self.realtime_hub = realtime_hub
        self.store = AutopilotStore(db_path=db_path)
        self.webhook = WebhookServer(port=webhook_port)
        self._autopilots: Dict[str, AutopilotDef] = {}
        self._running = False
        self._cron_task: Optional[asyncio.Task] = None
        self._conditional_task: Optional[asyncio.Task] = None
        self._stats = {
            "total_autopilots": 0,
            "cron": 0,
            "webhook": 0,
            "conditional": 0,
            "event": 0,
            "total_fires": 0,
        }

    async def start(self):
        self._running = True

        # Cargar autopilots persistidos
        await self._load_persisted()

        # Iniciar cron loop
        self._cron_task = asyncio.create_task(self._cron_eval_loop())

        # Iniciar conditional eval loop
        self._conditional_task = asyncio.create_task(self._conditional_eval_loop())

        # Webhook server: opcional (interfiere con HTTP principal si arranca en el mismo loop)
        # Habilitar con NEXUS_AUTOPILOT_WEBHOOK=1
        if os.environ.get("NEXUS_AUTOPILOT_WEBHOOK") == "1":
            try:
                await asyncio.wait_for(self.webhook.start(), timeout=5)
            except Exception as e:
                logger.warning(f"Autopilot webhook disabled: {e}")
        else:
            logger.info("Autopilot webhook disabled (set NEXUS_AUTOPILOT_WEBHOOK=1 to enable)")

        # Suscribirse a eventos del Hub si está disponible
        if self.realtime_hub:
            await self.realtime_hub.subscribe("autopilot:*", self._on_hub_event)
            await self.realtime_hub.subscribe("trigger:*", self._on_hub_event)

        logger.info(f"AutopilotService started: {self._stats['total_autopilots']} autopilots")

    async def stop(self):
        self._running = False
        if self._cron_task:
            self._cron_task.cancel()
        if self._conditional_task:
            self._conditional_task.cancel()
        await self.webhook.stop()
        logger.info("AutopilotService stopped")

    async def register(self, defn: AutopilotDef) -> str:
        """Registra un nuevo autopilot"""
        self._autopilots[defn.id] = defn
        self.store.save(defn)
        self._update_stats()

        # Registrar webhook si aplica
        if isinstance(defn.trigger, WebhookTrigger):
            path = defn.trigger.path
            if not path.startswith("/"):
                path = f"/{path}"
            self.webhook.register(path, lambda req, d=defn: self._handle_webhook(d, req))

        # Suscribirse a evento si aplica
        if isinstance(defn.trigger, EventTrigger) and self.realtime_hub:
            await self.realtime_hub.subscribe(
                defn.trigger.scope_pattern,
                lambda ev, d=defn: self._handle_event(d, ev),
            )

        logger.info(f"Autopilot registered: {defn.name} ({defn.trigger_type.value})")
        return defn.id

    async def unregister(self, autopilot_id: str):
        """Elimina un autopilot"""
        defn = self._autopilots.pop(autopilot_id, None)
        if defn:
            self.store.delete(autopilot_id)
            if isinstance(defn.trigger, WebhookTrigger):
                self.webhook.unregister(defn.trigger.path)
            self._update_stats()
            logger.info(f"Autopilot unregistered: {defn.name}")

    async def fire(self, autopilot_id: str, context: Optional[Dict] = None):
        """Dispara un autopilot manualmente"""
        defn = self._autopilots.get(autopilot_id)
        if not defn:
            logger.warning(f"Autopilot not found: {autopilot_id}")
            return

        await self._execute(defn, context or {})

    async def list_autopilots(self) -> List[Dict]:
        """Lista todos los autopilots"""
        return [d.to_dict() for d in self._autopilots.values()]

    async def _execute(self, defn: AutopilotDef, context: Dict):
        """Ejecuta un autopilot: dispara task_type + opcionalmente llama a worker_manager"""
        defn.last_fired = time.time()
        defn.fire_count += 1
        self.store.update_fire(defn.id)
        self._stats["total_fires"] += 1

        # 1. Publicar evento en RealtimeHub
        if self.realtime_hub:
            await self.realtime_hub.publish(
                event_type=defn.task_type,
                content={"autopilot_id": defn.id, "params": defn.task_params, **context},
                scope=f"autopilot:{defn.name}",
                source="autopilot_service",
            )

        # 2. Si hay worker_manager y task_type matchea un worker, ejecutarlo
        if self.worker_manager:
            worker = self.worker_manager.get_worker(defn.task_type)
            if worker:
                asyncio.create_task(self._run_worker(worker, context))

        logger.info(f"Autopilot fired: {defn.name} ({defn.task_type})")

    async def _run_worker(self, worker, context: Dict):
        """Ejecuta un worker en segundo plano"""
        try:
            result = await worker.run(context)
            logger.debug(f"Autopilot worker result: {worker.config.name} success={result.success}")
        except Exception as e:
            logger.warning(f"Autopilot worker error: {worker.config.name}: {e}")

    async def _cron_eval_loop(self):
        """Cada 60s evalúa autopilots CRON"""
        while self._running:
            try:
                now = datetime.now()
                for defn in self._autopilots.values():
                    if isinstance(defn.trigger, CronTrigger) and defn.can_fire():
                        if defn.trigger.matches(now):
                            await self._execute(defn, {"trigger": "cron", "time": now.isoformat()})
            except Exception as e:
                logger.warning(f"Cron eval error: {e}")
            await asyncio.sleep(60)

    async def _conditional_eval_loop(self):
        """Cada 120s evalúa autopilots CONDITIONAL"""
        while self._running:
            try:
                context = {"timestamp": time.time()}
                for defn in self._autopilots.values():
                    if isinstance(defn.trigger, ConditionalTrigger) and defn.can_fire():
                        if defn.trigger.evaluate(context):
                            await self._execute(defn, {"trigger": "conditional"})
            except Exception as e:
                logger.warning(f"Conditional eval error: {e}")
            await asyncio.sleep(120)

    async def _handle_webhook(self, defn: AutopilotDef, request: Dict) -> Dict:
        """Maneja un webhook entrante"""
        if not defn.can_fire():
            return {"status": "cooldown", "autopilot": defn.name}

        sig = request.get("headers", {}).get("x-signature", "")
        if isinstance(defn.trigger, WebhookTrigger):
            if not defn.trigger.verify_signature(request.get("body", "").encode(), sig):
                return {"status": "error", "error": "invalid signature"}

        asyncio.create_task(self._execute(defn, {"trigger": "webhook", "request": request}))
        return {"status": "fired", "autopilot": defn.name}

    async def _on_hub_event(self, event):
        """Receives events from RealtimeHub and dispatches to matching autopilots."""
        event_type = event.get("type", "") if isinstance(event, dict) else str(event)
        for defn in self._autopilots.values():
            if isinstance(defn.trigger, EventTrigger) and defn.trigger.event_type == event_type:
                await self._execute(defn, {"trigger": "event", "event": event})

    async def _handle_event(self, defn: AutopilotDef, event):
        """Maneja un evento del RealtimeHub"""
        if not defn.can_fire():
            return

        if isinstance(defn.trigger, EventTrigger):
            if event.type == defn.trigger.event_type:
                await self._execute(defn, {"trigger": "event", "event": event.to_dict()})

    async def _load_persisted(self):
        """Carga autopilots desde SQLite"""
        rows = self.store.load_all()
        for r in rows:
            trigger_data = json.loads(r["trigger_data"])
            trigger = self._deserialize_trigger(trigger_data)
            defn = AutopilotDef(
                id=r["id"],
                name=r["name"],
                trigger=trigger,
                task_type=r["task_type"],
                task_params=json.loads(r["task_params"]),
                enabled=bool(r["enabled"]),
                cooldown_seconds=r["cooldown_seconds"],
                created_at=r["created_at"],
                last_fired=r["last_fired"],
                fire_count=r["fire_count"],
                metadata=json.loads(r["metadata"]),
            )
            self._autopilots[defn.id] = defn

    def _deserialize_trigger(self, data: Dict) -> Any:
        ttype = data.get("type", "")
        if ttype == "cron":
            return CronTrigger.from_dict(data)
        if ttype == "webhook":
            return WebhookTrigger.from_dict(data)
        if ttype == "conditional":
            return ConditionalTrigger.from_dict(data)
        if ttype == "event":
            return EventTrigger.from_dict(data)
        return None

    def _update_stats(self):
        cron = sum(1 for d in self._autopilots.values() if isinstance(d.trigger, CronTrigger))
        web = sum(1 for d in self._autopilots.values() if isinstance(d.trigger, WebhookTrigger))
        cond = sum(1 for d in self._autopilots.values() if isinstance(d.trigger, ConditionalTrigger))
        evt = sum(1 for d in self._autopilots.values() if isinstance(d.trigger, EventTrigger))
        self._stats.update({
            "total_autopilots": len(self._autopilots),
            "cron": cron,
            "webhook": web,
            "conditional": cond,
            "event": evt,
        })

    def get_status(self) -> Dict:
        return {**self._stats, "running": self._running, "webhook_port": self.webhook.port}
