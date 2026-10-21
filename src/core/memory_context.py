"""Inyeccion automatica del grafo de conocimiento en el prompt.

No es un wrapper de `recall`: arma terminos significativos del turno, busca
cada uno por separado (el FTS de Nexus une con AND y una frase larga casi
nunca matchea), fusiona por rango reciproco, recorta a presupuesto y registra
uso real (`access_count` + evento `memory_used`).

El aprendizaje nunca puede romper un turno: todo fallo de memoria devuelve "".
"""
from __future__ import annotations

import logging
import math
import re
import time
import unicodedata
from typing import Any, Optional

logger = logging.getLogger(__name__)

_FENCE = re.compile(r"```.*?```", re.S)
_INLINE = re.compile(r"`[^`]*`")
_URL = re.compile(r"https?://\S+")
_WORD = re.compile(r"[0-9a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00fc\u00f1_]{3,}", re.I)


def _fold(text: str) -> str:
    """Minusculas sin diacriticos: 'cómo' y 'como' son la misma stopword."""
    norm = unicodedata.normalize("NFD", text or "")
    return "".join(c for c in norm if unicodedata.category(c) != "Mn").lower()


_STOP = frozenset(_fold(w) for w in {
    # es
    "que", "con", "por", "para", "como", "cuando", "donde", "porque", "sobre",
    "este", "esta", "estos", "estas", "esto", "ese", "esa", "esos", "esas",
    "eso", "aquel", "aquella", "los", "las", "del", "una", "uno", "unos",
    "unas", "pero", "sino", "mas", "muy", "sin", "son", "fue", "era", "ser",
    "esta", "estan", "hay", "tiene", "tienen", "hace", "hacer", "puede",
    "pueden", "debe", "deben", "todo", "toda", "todos", "todas", "otro",
    "otra", "otros", "otras", "mismo", "misma", "cada", "entre", "desde",
    "hasta", "según", "segun", "tambien", "también", "solo", "sólo", "aun",
    "aún", "ya", "muy", "nos", "les", "porque", "quien", "quién", "cual",
    "cuál", "cuales", "cuáles", "vos", "tenes", "tenés", "queres", "querés",
    "funciona", "funcionan", "funcionando", "prueba", "pruebas", "correctamente",
    "ejemplo", "hola", "gracias", "favor", "quiero", "necesito", "podes",
    "arregla", "arreglar", "implementar", "implementa", "decidimos",
    # en
    "the", "and", "for", "with", "that", "this", "from", "are", "was", "were",
    "have", "has", "had", "not", "but", "its", "into", "than", "then", "them",
    "they", "will", "would", "could", "should", "can", "may", "use", "using",
    "new", "one", "two", "all", "any", "each", "every", "via", "you", "your",
    "our", "out", "how", "what", "when", "where", "why", "who", "does",
})


def query_terms(text: str, limit: int = 10) -> list[str]:
    """Terminos utiles del turno: sin codigo, URLs, stopwords ni repetidos."""
    clean = _FENCE.sub(" ", text or "")
    clean = _INLINE.sub(" ", clean)
    clean = _URL.sub(" ", clean)
    out: list[str] = []
    seen: set[str] = set()
    for raw in _WORD.findall(clean):
        word = _fold(raw)
        if word in _STOP or word.isdigit() or word in seen:
            continue
        seen.add(word)
        out.append(word)
        if len(out) >= max(1, int(limit)):
            break
    return out


def _load_cfg() -> dict:
    try:
        from src.core.config import load_config

        return load_config() or {}
    except Exception:
        return {}


def _first_line(text: str, limit: int = 90) -> str:
    line = re.sub(r"\s+", " ", (text or "")).strip()
    return line[:limit]


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b:
        return 0.0
    n = min(len(a), len(b))
    dot = sum(float(a[i]) * float(b[i]) for i in range(n))
    na = math.sqrt(sum(float(x) * float(x) for x in a[:n]))
    nb = math.sqrt(sum(float(x) * float(x) for x in b[:n]))
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / (na * nb)


def _semantic_rerank(query: str, hits: list[dict], mcfg: dict) -> tuple[list[dict], str, int]:
    """Reordena por coseno (embeddings locales) con fallback lexico silencioso.

    `auto` intenta embeddings y cae a lexico sin romper el turno; `force`
    registra el fallo pero tampoco rompe (el contrato de memoria es no-fatal).
    """
    mode = str(mcfg.get("rerank", "auto") or "auto").lower()
    if mode in ("off", "none", "0", "false") or not hits:
        return hits, "off", 0
    timeout_ms = max(100, int(mcfg.get("rerank_timeout_ms", 2000) or 2000))
    max_cand = max(1, min(16, int(mcfg.get("rerank_candidates", 8) or 8)))
    cand = hits[:max_cand]
    texts = [query]
    for h in cand:
        texts.append(((h.get("title") or "") + " " + (h.get("snippet") or ""))[:2000])
    t0 = time.monotonic()
    try:
        from src.core.embeddings import embed
        vecs = embed(texts, timeout=max(1, int(round(timeout_ms / 1000.0))))
        if len(vecs) != len(texts) or not vecs[0]:
            raise ValueError("embeddings incompletos")
    except Exception as e:
        logger.debug("memory_context: rerank sin embeddings (%s)", e)
        return hits, "fallback", int((time.monotonic() - t0) * 1000)
    qv = vecs[0]
    lexical = [max(0.0, float(h.get("score") or 0.0)) for h in cand]
    max_lex = max(lexical) if lexical else 0.0
    scored: list[dict] = []
    for i, h in enumerate(cand):
        sem = _cosine(qv, vecs[i + 1])
        lex = (lexical[i] / max_lex) if max_lex > 0 else 0.0
        item = dict(h)
        item["_semantic"] = round(sem, 4)
        item["_final"] = round(0.65 * sem + 0.35 * lex, 6)
        scored.append(item)
    scored.sort(key=lambda x: float(x.get("_final") or 0.0), reverse=True)
    return scored + hits[max_cand:], "semantic", int((time.monotonic() - t0) * 1000)


def build_memory_context(
    query: str,
    *,
    session_id: str = "",
    limit: int | None = None,
    max_chars: int | None = None,
    memory: Any = None,
    event_store: Any = None,
    config: Optional[dict] = None,
    collect: Optional[dict] = None,
) -> str:
    """Devuelve el bloque de memoria para el prompt, o "" si no hay nada util."""
    cfg = config if config is not None else _load_cfg()
    mcfg = ((cfg.get("memory") or {}).get("inject") or {}) if isinstance(cfg, dict) else {}
    if not mcfg.get("enabled", True):
        return ""
    text = (query or "").strip()
    if len(text) < int(mcfg.get("min_query_chars", 8)):
        return ""
    terms = query_terms(text, limit=int(mcfg.get("max_terms", 10)))
    if len(terms) < int(mcfg.get("min_terms", 2)):
        return ""

    limit = max(1, min(8, int(limit or mcfg.get("limit", 3))))
    max_chars = max(200, int(max_chars or mcfg.get("max_chars", 2400)))
    snippet_chars = max(80, int(mcfg.get("snippet_chars", 320)))
    min_matches = max(1, int(mcfg.get("min_matches", 2)))

    try:
        if memory is None:
            from src.core.knowledge_memory import get_knowledge_memory

            memory = get_knowledge_memory()
        hits = memory.search_terms(terms, limit=max(limit * 3, 8), per_term=24)
    except Exception as e:
        logger.debug("memory_context: busqueda omitida: %s", e)
        return ""

    rerank_mode = str(mcfg.get("rerank", "auto") or "auto").lower()
    max_pick = limit
    if rerank_mode not in ("off", "none", "0", "false"):
        max_pick = max(limit, min(16, int(mcfg.get("rerank_candidates", 8) or 8)))
    picked: list[dict] = []
    for hit in hits or []:
        need = min_matches if len(terms) >= 3 else 1
        if int(hit.get("matches", 1)) < need:
            continue
        picked.append(hit)
        if len(picked) >= max_pick:
            break
    if not picked:
        if collect is not None:
            collect.update({"node_ids": [], "rerank": "empty", "terms": terms})
        return ""

    picked, rerank_status, rerank_ms = _semantic_rerank(text, picked, mcfg)
    picked = picked[:limit]

    lines = [
        "MEMORIA DEL GRAFO (recuperada automáticamente; usá estos datos exactos y "
        "citá el [id]. Si no aplican a la tarea, ignorá este bloque):"
    ]
    used = len(lines[0])
    node_ids: list[int] = []
    for hit in picked:
        snippet = re.sub(r"\s+", " ", (hit.get("snippet") or "")).strip()
        if not snippet:
            continue
        title = _first_line(hit.get("title") or "", 90)
        category = (hit.get("category") or "").strip()
        meta = " · ".join(p for p in (category, str(hit.get("updated_at") or "")[:10]) if p)
        head = f"- [{hit.get('id')}]"
        if meta:
            head += f" ({meta})"
        if title:
            head += f" {title}"
        remaining = max_chars - used - 1
        room = remaining - len(head) - 3
        if room < 40:
            break
        snippet = snippet[: min(snippet_chars, room)]
        line = f"{head} - {snippet}"
        if used + len(line) + 1 > max_chars:
            break
        lines.append(line)
        used += len(line) + 1
        try:
            node_ids.append(int(hit.get("id")))
        except (TypeError, ValueError):
            continue
    if not node_ids:
        if collect is not None:
            collect.update({"node_ids": [], "rerank": rerank_status, "terms": terms})
        return ""
    if collect is not None:
        collect.update({
            "node_ids": list(node_ids), "rerank": rerank_status,
            "rerank_ms": rerank_ms, "terms": terms,
        })

    try:
        memory.touch_many(node_ids)
    except Exception as e:
        logger.debug("memory_context: touch omitido: %s", e)
    if event_store is not None:
        try:
            event_store.append_event(
                "memory_used",
                {
                    "node_ids": node_ids,
                    "terms": terms,
                    "query_len": len(text),
                    "injected_chars": used,
                    "rerank": rerank_status,
                    "rerank_ms": rerank_ms,
                },
                session_id=session_id,
            )
        except Exception as e:
            logger.debug("memory_context: evento omitido: %s", e)
    return "\n".join(lines)


def memory_stats(memory: Any = None) -> dict:
    """Contadores para medir el foso: uso por nodo y cobertura de recuperacion."""
    try:
        mem = memory
        if mem is None:
            from src.core.knowledge_memory import get_knowledge_memory

            mem = get_knowledge_memory()
        con = mem._connect()
        try:
            row = con.execute(
                "SELECT count(*) total, sum(access_count) usos, "
                "sum(CASE WHEN access_count>0 THEN 1 ELSE 0 END) usados, "
                "max(last_accessed) ultimo FROM nodes"
            ).fetchone()
            return {
                "nodes": int(row["total"] or 0),
                "uses": int(row["usos"] or 0),
                "used_nodes": int(row["usados"] or 0),
                "last_used": row["ultimo"],
            }
        finally:
            con.close()
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}
