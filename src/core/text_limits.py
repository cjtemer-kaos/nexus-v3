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


def truncate_head_tail(text: str, max_chars: int = DEFAULT_MAX_CHARS, *, label: str = "salida") -> str:
    """Recorta el medio y conserva inicio+final, con aviso accionable."""
    text = text or ""
    max_chars = max(2, int(max_chars))
    if len(text) <= max_chars:
        return text
    head = max_chars // 2
    tail = max_chars - head
    return (
        f"{text[:head]}\n"
        f"[... {label} truncada: {len(text)} chars en total; se conservan {head} del inicio "
        f"y {tail} del final. Refina el comando (grep/head/tail) para ver el medio elidido ...]\n"
        f"{text[-tail:]}"
    )
