"""Chequeo de la version de Python en los puntos de entrada.

`pyproject.toml` declara `requires-python = ">=3.11"`, pero nada lo verificaba:
en un 3.9 el server moria mas adelante con un SyntaxError o con un TypeError de
`X | Y`, sin decir que el problema era la version. Una linea al arrancar ahorra
esa caza.
"""
from __future__ import annotations

import sys
from typing import Any

MIN_PYTHON: tuple[int, int] = (3, 11)


def python_ok(version_info: Any = None, minimum: tuple[int, int] = MIN_PYTHON) -> bool:
    actual = tuple(int(x) for x in tuple(version_info or sys.version_info)[:2])
    return actual >= tuple(minimum)


def require_python(version_info: Any = None,
                   minimum: tuple[int, int] = MIN_PYTHON) -> None:
    """Corta con un mensaje accionable si el interprete es viejo.

    `version_info` existe para poder testear sin cambiar de interprete.
    """
    if python_ok(version_info, minimum):
        return
    actual = tuple(int(x) for x in tuple(version_info or sys.version_info)[:2])
    raise RuntimeError(
        f"nexus-v3 necesita Python {minimum[0]}.{minimum[1]}+ y este es "
        f"{actual[0]}.{actual[1]}. Instala uno compatible o apunta "
        f"NEXUS_PYTHON al interprete correcto."
    )
