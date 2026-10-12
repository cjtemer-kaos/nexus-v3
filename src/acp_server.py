"""Adaptador ACP (Agent Client Protocol) sobre stdio para nexus-v3.

Expone el harness como agente ACP (usado por Zed y otros editores). JSON-RPC 2.0
newline-delimited por stdin/stdout. Reusa el ProviderRouter (LLM por capacidad).

Uso:
    python -m src.acp_server
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid


def _log(*a) -> None:
    print("[nexus-acp]", *a, file=sys.stderr, flush=True)


class NexusACPAgent:
    def __init__(self) -> None:
        if os.getcwd() not in sys.path:
            sys.path.insert(0, os.getcwd())
        from src.api.server import _build_router_from_config, _load_config_safe
        self.router = _build_router_from_config(_load_config_safe())
        self.sessions: dict[str, list] = {}

    def _emit(self, method: str, params: dict) -> None:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "method": method, "params": params},
                                    ensure_ascii=False) + "\n")
        sys.stdout.flush()

    def _chunk(self, session_id: str, text: str) -> None:
        self._emit("session/update", {
            "sessionId": session_id,
            "update": {"sessionUpdate": "agent_message_chunk",
                       "content": {"type": "text", "text": text}},
        })

    async def prompt(self, session_id: str, prompt: list) -> str:
        from src.core.provider_base import LLMMessage  # type: ignore
        text = "\n".join(p.get("text", "") for p in (prompt or []) if isinstance(p, dict))
        hist = self.sessions.setdefault(session_id, [])
        hist.append(LLMMessage(role="user", content=text))
        try:
            resp = await asyncio.wait_for(self.router.chat(hist), timeout=180)
            out = resp.content or ""
            hist.append(LLMMessage(role="assistant", content=out))
        except Exception as e:
            out = f"error: {e}"
        self._chunk(session_id, out)
        return "end_turn"

    async def handle(self, msg: dict) -> dict | None:
        mid = msg.get("id")
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "initialize":
            return {"jsonrpc": "2.0", "id": mid, "result": {
                "protocolVersion": 1,
                "agentCapabilities": {"promptCapabilities": {"image": False, "audio": False}},
                "agentInfo": {"name": "nexus-v3", "version": "3.0.0"},
            }}
        if method == "session/new":
            sid = uuid.uuid4().hex
            self.sessions[sid] = []
            return {"jsonrpc": "2.0", "id": mid, "result": {"sessionId": sid}}
        if method == "session/prompt":
            sid = params.get("sessionId", "")
            stop = await self.prompt(sid, params.get("prompt") or [])
            return {"jsonrpc": "2.0", "id": mid, "result": {"stopReason": stop}}
        if method == "session/cancel":
            return {"jsonrpc": "2.0", "id": mid, "result": {}}
        if method in ("session/update", "notifications/initialized"):
            return None
        if mid is None:
            return None
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stdin.reconfigure(encoding="utf-8")
    except Exception:
        pass
    agent = NexusACPAgent()
    _log("listo")
    for raw in sys.stdin:
        line = raw.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except Exception:
            continue
        try:
            resp = asyncio.run(agent.handle(msg))
        except Exception as e:
            _log("handler error:", e)
            continue
        if resp is not None:
            sys.stdout.write(json.dumps(resp, ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
