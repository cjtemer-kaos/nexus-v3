"""Bootstrap para la suite pytest de nexus-v3 (paridad con run_tests.py)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture(scope="session", autouse=True)
def _bases_aisladas(tmp_path_factory):
    """El `session.db` y el `event_store.db` de la suite viven en un tmp.

    `NEXUS_V3_DATA` mueve spill y checkpoints, pero no las bases: `_session_log()`
    del server las resuelve desde config (`./data/session.db`). Un test que
    usaba `_session_log()` creaba sesiones reales en la base del usuario
    (`project=pytest-context`) que después aparecían en la UI. Se siembra el
    estado del server una sola vez, antes de que el primer test lo lea.
    """
    from src.api import server
    from src.core import checkpoints
    from src.core.event_store import EventStore
    from src.core.session_log import SessionLog

    datos = tmp_path_factory.mktemp("datos")
    previo = os.environ.get("NEXUS_V3_DATA")
    os.environ["NEXUS_V3_DATA"] = str(datos)
    checkpoints._ROOT = datos / "checkpoints"

    server._state["session_log"] = SessionLog(str(datos / "session.db"), wal=True)
    server._state["event_store"] = EventStore(str(datos / "event_store.db"))
    yield datos

    server._state.pop("session_log", None)
    server._state.pop("event_store", None)
    if previo is None:
        os.environ.pop("NEXUS_V3_DATA", None)
    else:
        os.environ["NEXUS_V3_DATA"] = previo


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
