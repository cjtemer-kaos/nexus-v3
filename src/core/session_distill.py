"""Session distillation — extrae el conocimiento de una sesión ANTES de compactar.

Antes de que la compactación descarte el historial crudo, se resume lo realizado
con el usuario (peticiones, herramientas usadas, resultados, errores) y se guarda
en el grafo de conocimiento. Así se libera contexto sin perder conocimiento, y el
saber de TODAS las sesiones queda persistido y buscable (recall).
"""
from __future__ import annotations

import hashlib
from typing import Any, Callable


def _turn_digest(messages: list[dict]) -> list[dict]:
    """Agrupa los mensajes en turnos de trabajo (petición → acciones → resultado)."""
    turns: list[dict] = []
    cur: dict | None = None
    for m in messages:
        role = (m.get("role") or "").strip()
        content = (m.get("content") or "").strip()
        if role == "user":
            if cur:
                turns.append(cur)
            cur = {"request": content, "tools": [], "result": "", "errors": []}
        elif cur is not None:
            if role == "tool":
                cur["tools"].append(m.get("tool_name") or "tool")
                low = content.lower()
                if low.startswith("error") or "error:" in low[:48] or "denied" in low[:48]:
                    cur["errors"].append(content[:200])
            elif role == "assistant" and content:
                cur["result"] = content[:800]
    if cur:
        turns.append(cur)
    return turns


def build_session_digest(session_id: str, messages: list[dict]) -> dict:
    turns = _turn_digest(messages)
    all_tools: set[str] = set()
    lines = [f"SESIÓN {session_id} — {len(turns)} turnos de trabajo"]
    for i, t in enumerate(turns, 1):
        tools = ", ".join(dict.fromkeys(t["tools"])) or "—"
        all_tools.update(t["tools"])
        lines.append(f"\n{i}. Petición del usuario: {t['request'][:300]}")
        lines.append(f"   Herramientas: {tools}")
        if t["result"]:
            lines.append(f"   Resultado: {t['result'][:500]}")
        if t["errors"]:
            lines.append(f"   Errores: {'; '.join(t['errors'][:3])}")
    return {
        "session_id": session_id,
        "turns": len(turns),
        "tools": sorted(all_tools),
        "content": "\n".join(lines),
    }


def _item_topic_key(session_id: str, text: str) -> str:
    h = hashlib.sha1(text.strip().lower().encode("utf-8")).hexdigest()[:12]
    return f"session/{session_id}/item/{h}"


def distill_session(
    session_id: str,
    messages: list[dict],
    learn_fn: Callable[..., Any],
    *,
    project: str = "nexus-v3",
    llm_extract: Callable[[str], list[dict]] | None = None,
) -> dict:
    """Extrae y persiste el conocimiento de la sesión. Idempotente por topic_key (UPSERT).

    learn_fn(content, *, topic_key, category, tags, title) escribe en el grafo.
    """
    digest = build_session_digest(session_id, messages)
    if not digest["turns"]:
        return {"ok": True, "distilled": 0, "reason": "sin turnos"}

    # 1) Nodo consolidado de la sesión (UPSERT: re-destilar actualiza en sitio).
    learn_fn(
        digest["content"],
        topic_key=f"session/{session_id}",
        category="episodio",
        tags=["session", "distill", *digest["tools"][:6]],
        title=f"Sesión {session_id}: {digest['turns']} turnos ({', '.join(digest['tools'][:4]) or 'sin tools'})",
    )
    written = 1

    # 2) Extracción enriquecida por LLM (opcional, best-effort): hechos/lecciones sueltas.
    if llm_extract is not None:
        try:
            for it in (llm_extract(digest["content"]) or [])[:12]:
                content = (it.get("content") or "").strip()
                if len(content) < 8:
                    continue
                learn_fn(
                    content,
                    topic_key=it.get("topic_key") or _item_topic_key(session_id, content),
                    category=(it.get("category") or "lesson").strip(),
                    tags=it.get("tags") or [],
                    title=(it.get("title") or content[:60]).strip(),
                )
                written += 1
        except Exception:
            pass

    return {"ok": True, "distilled": written, "turns": digest["turns"], "tools": digest["tools"]}
