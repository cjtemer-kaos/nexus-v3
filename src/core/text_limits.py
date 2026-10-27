"""Limites de texto para salidas de tools.

Port de `truncateCommandOutput` (Cline, output-limits.ts): cada caracter que
devuelve una tool se reenvia al modelo en CADA request posterior, asi que una
salida gigante cuesta cuadraticamente. Cuando hay que recortar, conservamos
cabeza Y cola: los errores y el exit code suelen estar al final, y un
`out[:N]` ciego los borra. El aviso vive en el borde para que sobreviva a un
recorte posterior.
"""
from __future__ import annotations

DEFAULT_MAX_CHARS = 20_000


def cap_json_list(payload: dict, key: str, max_chars: int = 48_000,
                  *, hint: str = "") -> str:
    """Serializa un dict recortando la lista `key` desde el final.

    A diferencia de `truncate_head_tail`, conserva JSON valido: el modelo puede
    parsearlo. Se usa en list_dir/glob/code_search, donde una lista enorme
    cuesta tokens en cada request posterior.
    """
    import json

    def _dump(obj: dict) -> str:
        return json.dumps(obj, ensure_ascii=False, indent=2)

    text = _dump(payload)
    if len(text) <= max_chars:
        return text
    items = payload.get(key)
    if not isinstance(items, list) or not items:
        return truncate_head_tail(text, max_chars, label=f"JSON ({key})")

    lo, hi, best = 0, len(items), 0
    while lo <= hi:
        mid = (lo + hi) // 2
        candidate = {**payload, key: items[:mid], "truncated": True}
        if hint:
            candidate["hint"] = hint
        if len(_dump(candidate)) <= max_chars:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    out = {**payload, key: items[:best], "truncated": True}
    if hint:
        out["hint"] = hint
    return _dump(out)


def truncate_head_tail(text: str, max_chars: int = DEFAULT_MAX_CHARS, *, label: str = "salida",
                       hint: str = "") -> str:
    """Recorta el medio y conserva inicio+final, con aviso accionable.

    `hint` reemplaza el consejo por defecto ("refina el comando"): cuando el
    texto viene de leer un ARCHIVO, el modelo no puede correr grep/head/tail
    sobre lo que ya tiene en la mano, tiene que paginar con offset/limit.
    """
    text = text or ""
    max_chars = max(2, int(max_chars))
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    consejo = hint or "Refina el comando (grep/head/tail) para ver el medio elidido"
    return (
        f"{text[:head]}\n"
        f"[... {label} truncada: {len(text)} chars en total; se conservan {head} del inicio "
        f"y {tail} del final. {consejo} ...]\n"
        f"{text[-tail:]}"
    )
