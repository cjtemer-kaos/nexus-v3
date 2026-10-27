"""Team tools: spawn asincrono, await y mailbox sobre SubAgentSpawner.

`sub_agent` es bloqueante: el padre espera el resultado antes de seguir. Para
trabajo en equipo hace falta lanzar varios hijos, seguir haciendo cosas y
recoger resultados/mensajes despues (patron de Cline team tools). Este modulo
solo orquesta; la ejecucion y el aislamiento siguen siendo del spawner.
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, is_dataclass
from typing import Any

from .sub_agent_spawner import (
    SubAgentResult,
    SubAgentSpec,
    stable_child_id,
)


def _result_dict(result: Any) -> dict:
    if is_dataclass(result):
        data = asdict(result)
    elif isinstance(result, dict):
        data = dict(result)
    else:
        data = {"result": result}
    status = data.get("status")
    if hasattr(status, "value"):
        data["status"] = status.value
    return data


class TeamRuntime:
    """Capa de orquestacion por sesion (una instancia por spawner)."""

    def __init__(self, spawner):
        self.spawner = spawner
        self._tasks: dict[str, asyncio.Task] = {}
        self._meta: dict[str, dict] = {}
        self._aliases: dict[str, str] = {}
        # Mensajes que llegaron mientras el hijo trabajaba: se responden al
        # terminar el turno en curso (un turno mas por mensaje, no un bucle).
        self._pendiente_resume: set[str] = set()

    def _resolve(self, key: str) -> str | None:
        if key in self._tasks:
            return key
        return self._aliases.get(key)

    def _desconocido(self, key: str) -> dict:
        """Error con los ids vivos: sin la lista el modelo no puede reintentar bien."""
        activos = sorted(self._meta.keys())
        cola = f" Activos: {', '.join(activos)}." if activos else " No hay equipos activos."
        return {"ok": False, "error": f"team_id/child_id desconocido: {key}.{cola}"}

    def _marcar_done(self, team_id: str, t: asyncio.Task) -> None:
        entry = self._meta.get(team_id)
        if entry is None:
            return
        try:
            res = t.result()
            entry["status"] = res.status.value if hasattr(res.status, "value") else str(res.status)
            entry["result"] = _result_dict(res)
            if getattr(res, "child_id", ""):
                entry["child_id"] = res.child_id
        except asyncio.CancelledError:
            entry["status"] = "cancelled"
        except Exception as e:
            entry["status"] = "failed"
            entry["error"] = str(e)
        # Mensaje que llego mientras el hijo trabajaba: se responde en un turno
        # mas, sin que el padre tenga que volver a mandarlo.
        if team_id in self._pendiente_resume:
            self._pendiente_resume.discard(team_id)
            try:
                self._lanzar_resume(team_id, "")
            except Exception as e:
                entry["error"] = str(e)

    def _lanzar_resume(self, team_id: str, message: str) -> dict:
        meta = self._meta[team_id]
        child_id = meta.get("child_id", "")
        resume = getattr(self.spawner, "resume_child", None)
        if resume is None:
            return {"ok": False, "team_id": team_id, "child_id": child_id,
                    "resumed": False, "error": "el spawner no soporta continuacion"}
        task = asyncio.create_task(resume(child_id, message))
        self._tasks[team_id] = task
        meta["status"] = "running"
        meta["resumed"] = int(meta.get("resumed", 0) or 0) + 1
        task.add_done_callback(lambda t, tid=team_id: self._marcar_done(tid, t))
        return {"ok": True, "team_id": team_id, "child_id": child_id,
                "resumed": True, "resumed_count": meta["resumed"], "status": "running"}

    def spawn(self, spec: SubAgentSpec, name: str = "") -> dict:
        team_id = (name or "").strip() or f"team-{uuid.uuid4().hex[:8]}"
        if team_id in self._tasks:
            raise ValueError(f"team_id ya existe: {team_id}")
        child_id = stable_child_id(spec.task, "", 1, spec.provider, spec.mode)
        task = asyncio.create_task(self.spawner.spawn_child(spec))
        meta = {
            "team_id": team_id,
            "child_id": child_id,
            "task": spec.task,
            "mode": spec.mode,
            "status": "running",
            "created_at": time.time(),
        }
        self._tasks[team_id] = task
        self._meta[team_id] = meta
        self._aliases[child_id] = team_id
        task.add_done_callback(lambda t, tid=team_id: self._marcar_done(tid, t))
        return dict(meta)

    async def await_member(self, key: str, timeout: float | None = None) -> dict:
        team_id = self._resolve(key)
        if team_id is None:
            return self._desconocido(key)
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        for _ in range(4):
            task = self._tasks[team_id]
            meta = self._meta[team_id]
            restante = None if deadline is None else max(0.0, deadline - loop.time())
            try:
                result = await asyncio.wait_for(asyncio.shield(task), timeout=restante)
            except asyncio.TimeoutError:
                return {"ok": True, "timed_out": True, "status": "running",
                        "team_id": team_id, "child_id": meta.get("child_id", "")}
            except asyncio.CancelledError:
                return {"ok": False, "status": "cancelled", "team_id": team_id,
                        "error": "el hijo fue cancelado"}
            except Exception as e:
                return {"ok": False, "status": "failed", "team_id": team_id,
                        "error": str(e)}
            # Deja correr el callback de done: si habia un mensaje encolado, el
            # resume ya es el task nuevo y hay que esperarlo tambien.
            await asyncio.sleep(0)
            if self._tasks.get(team_id) is task:
                break
        return {"ok": True, "timed_out": False, "team_id": team_id,
                "status": (result.status.value if hasattr(result.status, "value")
                           else str(result.status)),
                "result": _result_dict(result)}

    async def _enqueue(self, child_id: str, message: str) -> bool:
        """Encola con reintentos: el hijo puede estar arrancando todavia."""
        for _ in range(3):
            if self.spawner.send_message(child_id, message):
                return True
            await asyncio.sleep(0.05)
        return False

    async def send(self, key: str, message: str) -> dict:
        """Manda un mensaje al hijo. Si ya termino y es continuable, lo reanuda."""
        team_id = self._resolve(key)
        if team_id is None:
            return self._desconocido(key)
        meta = self._meta[team_id]
        child_id = meta.get("child_id", "")
        message = (message or "").strip()
        task = self._tasks.get(team_id)
        vivo = task is not None and not task.done()

        if getattr(self.spawner, "resume_child", None) is None:
            # Spawner sin continuacion (fakes/viejos): encolar como siempre.
            sent = await self._enqueue(child_id, message)
            return {"ok": sent, "team_id": team_id, "child_id": child_id, "resumed": False,
                    "error": "" if sent else "el hijo todavia no arranco o ya termino"}

        if vivo:
            sent = await self._enqueue(child_id, message)
            if not sent:
                return {"ok": False, "team_id": team_id, "child_id": child_id,
                        "resumed": False, "error": "el hijo ya no acepta mensajes"}
            self._pendiente_resume.add(team_id)
            return {"ok": True, "team_id": team_id, "child_id": child_id, "resumed": False,
                    "queued": True, "status": "running",
                    "note": "el hijo responde al terminar el turno en curso"}
        # Terminado: el buzon no lo lee nadie, hay que correr otro turno.
        check = getattr(self.spawner, "_check_resumible", None)
        if check is not None:
            check(child_id)
        return self._lanzar_resume(team_id, message)

    def inbox(self, key: str) -> dict:
        team_id = self._resolve(key)
        if team_id is None:
            return self._desconocido(key)
        child_id = self._meta[team_id].get("child_id", "")
        msgs = []
        try:
            if hasattr(self.spawner, "drain_inbox"):
                msgs = self.spawner.drain_inbox(child_id) or []
            else:
                msgs = list(getattr(self.spawner, "_inbox", {}).get(child_id, []) or [])
        except Exception:
            msgs = []
        return {"ok": True, "team_id": team_id, "child_id": child_id,
                "messages": msgs, "count": len(msgs)}

    def list_members(self) -> list[dict]:
        out: list[dict] = []
        for team_id, meta in self._meta.items():
            item = dict(meta)
            task = self._tasks.get(team_id)
            if task is not None and task.done() and item.get("status") == "running":
                item["status"] = "completed"
            item.pop("result", None)
            out.append(item)
        return out

