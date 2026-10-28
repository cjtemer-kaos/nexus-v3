"""Bootstrap para la suite pytest de nexus-v3 (paridad con run_tests.py)."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(autouse=True)
def _artefactos_en_tmp(tmp_path, monkeypatch):
    """Toda la suite escribe su `data/` en `tmp`, nunca en el del usuario.

    Ejercitar `write_file`/`edit_file` también ejercita el snapshot previo y el
    volcado de resultados grandes, y la compactación persiste el contenido que
    descarta: cada corrida dejaba `data/checkpoints/<id>` y `data/spill/` de
    sesiones que sólo existen durante el test, que después aparecían como
    huérfanos en la auditoría. Se corta acá, en un solo lugar.

    `NEXUS_V3_DATA` mueve `data_home()`, que es lo que usan spill, los repos git
    de checkpoints y los volcados de compactación. `checkpoints._ROOT` se fija
    al importar, así que se parchea aparte.
    """
    from src.core import checkpoints

    datos = tmp_path / "_datos"
    monkeypatch.setenv("NEXUS_V3_DATA", str(datos))
    monkeypatch.setattr(checkpoints, "_ROOT", datos / "checkpoints", raising=False)
    return datos
