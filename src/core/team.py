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

    def _resolve(self, key: str) -> str | None:
        if key in self._tasks:
            return key
        return self._aliases.get(key)

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

        def _done(t: asyncio.Task, tid: str = team_id) -> None:
            entry = self._meta.get(tid)
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

        task.add_done_callback(_done)
        return dict(meta)

    async def await_member(self, key: str, timeout: float | None = None) -> dict:
        team_id = self._resolve(key)
        if team_id is None:
            return {"ok": False, "error": f"team_id/child_id desconocido: {key}"}
        task = self._tasks[team_id]
        meta = self._meta[team_id]
        try:
            result = await asyncio.wait_for(asyncio.shield(task), timeout=timeout)
        except asyncio.TimeoutError:
            return {"ok": True, "timed_out": True, "status": "running",
                    "team_id": team_id, "child_id": meta.get("child_id", "")}
        except asyncio.CancelledError:
            return {"ok": False, "status": "cancelled", "team_id": team_id,
                    "error": "el hijo fue cancelado"}
        except Exception as e:
            return {"ok": False, "status": "failed", "team_id": team_id,
                    "error": str(e)}
        return {"ok": True, "timed_out": False, "team_id": team_id,
                "status": (result.status.value if hasattr(result.status, "value")
                           else str(result.status)),
                "result": _result_dict(result)}

    async def send(self, key: str, message: str) -> dict:
        team_id = self._resolve(key)
        if team_id is None:
            return {"ok": False, "error": f"team_id/child_id desconocido: {key}"}
        child_id = self._meta[team_id].get("child_id", "")
        sent = False
        for _ in range(3):
            sent = bool(self.spawner.send_message(child_id, message))
            if sent:
                break
            await asyncio.sleep(0.05)
        return {"ok": sent, "team_id": team_id, "child_id": child_id,
                "error": "" if sent else "el hijo todavia no arranco o ya termino"}

    def inbox(self, key: str) -> dict:
        team_id = self._resolve(key)
        if team_id is None:
            return {"ok": False, "error": f"team_id/child_id desconocido: {key}"}
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
