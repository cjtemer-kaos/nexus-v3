"""Elige que schemas de tools viajan en el request.

Medido el 2026-10-28 en este repo: 47 tools = 26.503 chars = ~6.600 tokens, y
con las 53 fijadas de una sesion real el prompt era 17.029 tokens. O sea que
~40 % de CADA request era la lista de schemas, reenviada en cada iteracion del
turno: en una corrida de 100 llamadas son ~700k tokens solo en definiciones.

Anthropic mide lo mismo y lo resuelve con "tool search" (`defer_loading`): se
mandan 3-5 tools fijas y el resto se descubre bajo demanda. Su numero es que
las definiciones de un setup multi-MCP llegan a ~55k tokens y la busqueda las
recorta >85 %. Tambien documentan que la precision para elegir tool se cae
pasados los 30-50 tools, asi que recortar no solo ahorra: mejora la eleccion.

Aca se hace la version local (Ollama no tiene tool_reference):
- un nucleo fijo de tools que se usan en casi todo turno,
- el resto se agrega por coincidencia con el pedido (sin gastar un LLM),
- y `tool_search` deja que el modelo pida mas a mitad del turno.

La seleccion se calcula UNA vez por turno: si cambiara entre iteraciones se
invalidaria el prefijo cacheado en cada llamada.
"""
from __future__ import annotations

import re
import unicodedata

# Tools que aparecen en casi cualquier turno. Sin estas, el modelo no puede ni
# leer un archivo ni ejecutar un comando sin pedirlas primero.
CORE = (
    "read_file", "write_file", "edit", "exec", "list_dir", "grep", "glob",
    "apply_patch", "todo_write", "question", "tool_search",
)

# Palabras del pedido -> tools que resuelven ese pedido. En espanol porque el
# usuario escribe en espanol y los nombres de las tools estan en ingles: sin
# este puente, "commitea los cambios" no matchea `git_commit`.
PISTAS: dict[str, tuple[str, ...]] = {
    "git": ("git_status", "git_diff", "git_log", "git_commit"),
    "commit": ("git_commit", "git_status", "git_diff"),
    "rama": ("git_status", "git_log"),
    "diff": ("git_diff",),
    "test": ("run_tests",),
    "prueba": ("run_tests",),
    "pytest": ("run_tests",),
    "web": ("web_search", "web_fetch"),
    "internet": ("web_search", "web_fetch"),
    "busca": ("web_search", "grep", "code_search"),
    "buscar": ("web_search", "grep", "code_search"),
    "investiga": ("web_search", "web_fetch"),
    "url": ("web_fetch",),
    "descarga": ("web_fetch", "exec"),
    "mcp": ("mcp_list", "mcp_call"),
    "imagen": ("image_generate", "image_describe"),
    "foto": ("image_describe",),
    "memoria": ("learn", "recall"),
    "recuerda": ("recall",),
    "aprende": ("learn",),
    "subagente": ("sub_agent", "sub_agent_message", "sub_agent_list"),
    "delega": ("sub_agent", "sub_agent_message"),
    "equipo": ("team_spawn", "team_list", "team_mailbox", "team_await"),
    "checkpoint": ("checkpoint_create", "checkpoint_list", "checkpoint_diff", "checkpoint_restore"),
    "snapshot": ("checkpoint_create", "checkpoint_list"),
    "background": ("job_start", "job_read", "job_wait", "job_list"),
    "segundo plano": ("job_start", "job_read", "job_wait"),
    "skill": ("skill",),
    "habilidad": ("skill",),
    "harness": ("harness",),
    "gema": ("harness",),
    "repl": ("repl",),
    "python": ("repl", "exec"),
    "mapa": ("repo_map",),
    "estructura": ("repo_map",),
    "tarea": ("todo_write", "todo_read"),
    "pendiente": ("todo_write", "todo_read"),
}

_STOP = {
    "que", "con", "por", "para", "los", "las", "una", "uno", "del", "como",
    "este", "esta", "esto", "mas", "sin", "sus", "sea", "the", "and", "for",
    "you", "your", "with", "from", "this", "that", "all", "any", "use", "usa",
    "hace", "hacer", "quiero", "podes", "puedas", "solo", "todo", "todos",
}


def _norm(texto: str) -> str:
    """Minusculas sin acentos: 'Descarga' y 'descargá' tienen que matchear."""
    t = unicodedata.normalize("NFKD", str(texto or ""))
    t = "".join(c for c in t if not unicodedata.combining(c))
    return t.lower()


def _tokens(texto: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", _norm(texto))
            if len(w) >= 3 and w not in _STOP}


def _parecido(a: str, b: str) -> bool:
    """Match por raiz: 'descargar'/'descarga', 'commitear'/'commit'."""
    if a == b:
        return True
    n = min(len(a), len(b), 5)
    return n >= 4 and a[:n] == b[:n]


def _score(tarea: set[str], tool: dict, pistas_extra: dict[str, float]) -> float:
    fn = tool.get("function") or {}
    nombre = str(fn.get("name") or "")
    partes = set(w for w in nombre.split("_") if w)
    desc = _tokens(fn.get("description") or "")
    params = _tokens(" ".join(((fn.get("parameters") or {}).get("properties") or {}).keys()))

    score = 0.0
    for t in tarea:
        if any(_parecido(t, p) for p in partes):
            score += 3.0
        elif any(_parecido(t, d) for d in desc):
            score += 1.0
        elif any(_parecido(t, p) for p in params):
            score += 0.5
    score += pistas_extra.get(nombre, 0.0)
    return score


def _pistas(tarea_norm: str) -> dict[str, float]:
    """Tools sugeridas por el texto del pedido (substring, no token exacto)."""
    pesos: dict[str, float] = {}
    for pista, nombres in PISTAS.items():
        if pista in tarea_norm:
            for n in nombres:
                pesos[n] = max(pesos.get(n, 0.0), 6.0)
    return pesos


def select(
    task: str,
    tools: list[dict] | None,
    always: tuple[str, ...] | list[str] = CORE,
    max_tools: int = 16,
    min_score: float = 1.0,
) -> list[dict]:
    """Devuelve el subconjunto de `tools` que se manda en el turno.

    Mantiene el orden original: el prefijo (tools + system) queda estable y el
    proveedor puede reusar la cache entre iteraciones.
    """
    if not tools:
        return tools or []
    max_tools = max(1, int(max_tools or 16))
    fijas = set(always or ())
    if len(tools) <= max_tools:
        return tools

    tarea_norm = _norm(task)
    tarea = _tokens(task)
    pesos = _pistas(tarea_norm)

    elegidas: set[str] = set()
    for t in tools:
        nombre = str((t.get("function") or {}).get("name") or "")
        if nombre in fijas:
            elegidas.add(nombre)

    # Cupo restante para las que matchean el pedido.
    cupo = max(0, max_tools - len(elegidas))
    candidatas: list[tuple[float, int, str]] = []
    for i, t in enumerate(tools):
        nombre = str((t.get("function") or {}).get("name") or "")
        if nombre in elegidas:
            continue
        s = _score(tarea, t, pesos)
        if s >= min_score:
            candidatas.append((s, i, nombre))
    candidatas.sort(key=lambda x: (-x[0], x[1]))
    for _s, _i, nombre in candidatas[:cupo]:
        elegidas.add(nombre)

    return [t for t in tools if str((t.get("function") or {}).get("name") or "") in elegidas]


def search(tools: list[dict] | None, query: str, limit: int = 5) -> list[dict]:
    """Busca tools por texto libre (para la tool `tool_search`)."""
    if not tools:
        return []
    limit = max(1, min(20, int(limit or 5)))
    q = _norm(query)
    if not q.strip():
        return []
    tokens = _tokens(query)
    pesos = _pistas(q)
    filas: list[tuple[float, int, dict]] = []
    for i, t in enumerate(tools):
        fn = t.get("function") or {}
        nombre = str(fn.get("name") or "")
        # El substring directo pesa mas que el match por token: "checkpoint"
        # tiene que encontrar `checkpoint_diff` aunque no haya mas palabras.
        directo = 10.0 if q.strip() in _norm(nombre) else 0.0
        s = directo + _score(tokens, t, pesos)
        if s > 0:
            filas.append((s, i, t))
    filas.sort(key=lambda x: (-x[0], x[1]))
    return [t for _s, _i, t in filas[:limit]]


def resumen(tool: dict) -> dict:
    """Version compacta del schema para devolver en el resultado de `tool_search`.

    Se manda el schema completo: el modelo necesita los tipos y los `required`
    para armar la llamada, y una descripcion sin firma solo gasta tokens.
    """
    fn = tool.get("function") or {}
    return {
        "name": fn.get("name"),
        "description": (fn.get("description") or "").strip(),
        "parameters": fn.get("parameters") or {"type": "object", "properties": {}},
    }
