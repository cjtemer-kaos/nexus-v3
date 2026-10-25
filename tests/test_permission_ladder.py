"""Escalera de permisos #0..#4: peldaño por sesión y por mensaje."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.scope import (
    LEVEL_ALIASES,
    PERMISSION_LADDER,
    Scope,
    ScopeConfig,
    ScopeDenied,
    ladder_table,
    level_from_permission,
    level_name,
    level_preset,
    normalize_level,
)

ROOT = Path(__file__).resolve().parents[1]
CFG = ScopeConfig(allowed_roots=("./workspace", "./data"), permission="workspace_write")


def _scope(level=None, permission="workspace_write"):
    cfg = ScopeConfig(allowed_roots=("./workspace", "./data"), permission=permission)
    return Scope(cfg, project_root=ROOT, turn_level=level)


def test_escalera_tiene_cinco_peldanos_ordenados():
    tabla = ladder_table()
    assert [l["level"] for l in tabla] == [0, 1, 2, 3, 4]
    assert [l["name"] for l in tabla] == ["conversar", "leer", "workspace", "maquina", "total"]
    assert [l["preset"] for l in tabla] == [
        "read_only", "read_only", "workspace_write", "full", "full"]
    assert PERMISSION_LADDER[0]["tools"] is False
    assert PERMISSION_LADDER[3]["confirm_mutating"] is True
    assert PERMISSION_LADDER[4]["confirm_mutating"] is False


def test_normalize_level_acepta_numero_almohadilla_y_nombre():
    assert normalize_level("#0") == 0
    assert normalize_level("#3") == 3
    assert normalize_level(2) == 2
    assert normalize_level("maquina") == 3
    assert normalize_level("read_only") == 1
    assert normalize_level("full") == 4
    assert normalize_level("workspace-write") == 2
    # Un valor que no se reconoce cae al default, nunca al techo.
    assert normalize_level("lo-que-sea", default=2) == 2
    assert normalize_level(None, default=2) == 2
    # Clamp: nunca sale de 0..4.
    assert normalize_level(9) == 4
    assert normalize_level(-3) == 0
    assert set(LEVEL_ALIASES) and level_name(0) == "conversar"


def test_preset_a_peldano_es_estable():
    assert level_from_permission("read_only") == 1
    assert level_from_permission("workspace_write") == 2
    assert level_from_permission("full") == 4
    assert level_preset(0) == "read_only"
    assert level_preset(3) == "full"


def test_nivel_cero_no_da_ninguna_herramienta():
    s = _scope(level=0)
    assert s.level == 0
    assert s.permission == "read_only"
    for tool in ("read_file", "write", "exec", "recall"):
        with pytest.raises(ScopeDenied):
            s.check_tool(tool, {"path": "workspace/x.txt", "command": "dir"})


def test_nivel_uno_lee_pero_no_escribe():
    s = _scope(level=1)
    s.check_tool("read_file", {"path": "workspace/x.txt"})
    with pytest.raises(ScopeDenied):
        s.check_tool("write", {"path": "workspace/x.txt", "content": "x"})


def test_nivel_dos_escribe_en_el_workspace_pero_no_fuera():
    s = _scope(level=2)
    s.check_tool("write", {"path": "workspace/x.txt", "content": "x"})
    with pytest.raises(ScopeDenied):
        s.check_tool("write", {"path": "C:/Windows/System32/x.txt", "content": "x"})


def test_nivel_tres_pide_confirmacion_en_cada_mutacion():
    s = _scope(level=3)
    assert s.permission == "full"
    with pytest.raises(ScopeDenied) as err:
        s.check_tool("write", {"path": "workspace/x.txt", "content": "x"})
    assert "confirm=true" in str(err.value)
    s.check_tool("write", {"path": "workspace/x.txt", "content": "x", "confirm": True})
    # Leer nunca pide confirmacion.
    s.check_tool("read_file", {"path": "workspace/x.txt"})


def test_nivel_cuatro_no_pide_nada():
    s = _scope(level=4)
    s.check_tool("write", {"path": "workspace/x.txt", "content": "x"})
    assert s.to_dict()["level"] == 4
    assert s.to_dict()["level_name"] == "total"


def test_sin_peldano_manda_el_preset_de_config():
    s = _scope(level=None, permission="read_only")
    assert s.level == 1
    s = _scope(level=None, permission="full")
    assert s.level == 4


def test_escalar_durante_un_turno_con_peldano_esta_prohibido():
    s = _scope(level=2)
    with pytest.raises(ScopeDenied) as err:
        s.escalate("full", approved=True)
    assert "peldaño" in str(err.value)


# ---------------------------------------------------------------------------
# Resolución del peldaño por turno (server)
# ---------------------------------------------------------------------------
def test_resolve_turn_level_baja_siempre_y_sube_con_aprobacion():
    from src.api.server import _SESSION_LEVELS, _resolve_turn_level

    cfg = {"scope": {"permission": "workspace_write"}}
    _SESSION_LEVELS.pop("s-ladder", None)

    # Sin pedido: manda el preset (None = preset).
    assert _resolve_turn_level(None, "s-ladder", cfg) == (None, "")
    # Bajar el alcance nunca necesita permiso.
    assert _resolve_turn_level("#0", "s-ladder", cfg) == (0, "")
    assert _resolve_turn_level("leer", "s-ladder", cfg) == (1, "")
    # Subir por encima del preset se recorta y se avisa.
    lvl, note = _resolve_turn_level("#4", "s-ladder", cfg)
    assert lvl == 2 and "supera el alcance" in note
    # Con aprobación explícita, sube solo para ese turno.
    assert _resolve_turn_level("#4", "s-ladder", cfg, approved=True) == (4, "")

    # El peldaño de la sesión sube el techo de los turnos siguientes.
    _SESSION_LEVELS["s-ladder"] = 4
    try:
        assert _resolve_turn_level(None, "s-ladder", cfg) == (4, "")
        assert _resolve_turn_level("#3", "s-ladder", cfg) == (3, "")
    finally:
        _SESSION_LEVELS.pop("s-ladder", None)
