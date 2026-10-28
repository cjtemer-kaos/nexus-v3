"""El guard de secuencia mira lo que la accion TOCA, no lo que el archivo DICE.

Caso real: un sub-agente escribio `requirements.txt` y el plan (ok) y despues
fallo `README.md` y `main.py` con "secuencia MEDIUM no aprobada para write_file:
sin canal interactivo: fail-closed (policy=never)". El contenido del backend
mencionaba `.env` y `SECRET_KEY`: el guard serializaba `args` completo (incluido
`content`) y marcaba "toca secretos". Sin canal interactivo eso es fail-closed,
asi que el agente no podia escribir NADA y concluia que el scope lo bloqueaba.
"""
from __future__ import annotations

import asyncio

import pytest

from src.core.sequence_guard import RISK_LOW, RISK_MEDIUM, SequenceGuard

BACKEND = """\
from dotenv import load_dotenv
load_dotenv(".env")
SECRET_KEY = os.getenv("SECRET_KEY")
# ver credentials.md para el id_rsa del deploy
"""

CFG = {"guard": {"sequence": {"enabled": True, "mode": "heuristic",
                              "policy": {"enabled": False}}}}


def _risk(tool: str, args: dict, cfg: dict | None = None) -> str:
    guard = SequenceGuard(cfg or CFG)
    return asyncio.run(guard.evaluate(tool, args)).risk


def test_contenido_que_menciona_secretos_no_es_medio():
    """Escribir codigo que NOMBRA .env/secretos no es tocar secretos."""
    assert _risk("write_file", {"path": "workspace/main.py", "content": BACKEND}) == RISK_LOW
    assert _risk("edit", {"path": "workspace/main.py", "old": "x", "new": BACKEND}) == RISK_LOW


def test_ruta_de_secreto_sigue_siendo_medio():
    """La ruta si importa: escribir en `.env` o en una key es MEDIUM."""
    assert _risk("write_file", {"path": "workspace/.env", "content": "A=1"}) == RISK_MEDIUM
    assert _risk("write_file", {"path": "certs/server.pem", "content": "x"}) == RISK_MEDIUM


def test_comando_que_lee_secretos_sigue_siendo_medio():
    assert _risk("exec", {"command": "type .env"}) == RISK_MEDIUM
    assert _risk("exec", {"command": "dir"}) == RISK_LOW


def test_apply_patch_solo_mira_las_rutas_del_parche():
    parche_ok = (
        "*** Begin Patch\n*** Add File: workspace/main.py\n"
        "+load_dotenv('.env')\n+SECRET_KEY = 1\n*** End Patch\n"
    )
    assert _risk("apply_patch", {"patch": parche_ok}) == RISK_LOW
    parche_secreto = (
        "*** Begin Patch\n*** Add File: workspace/.env\n+A=1\n*** End Patch\n"
    )
    assert _risk("apply_patch", {"patch": parche_secreto}) == RISK_MEDIUM


def test_regla_de_politica_medium_secrets_no_mira_el_cuerpo():
    """La regla declarativa `medium-secrets` (tools: ['*']) tenia el mismo bug."""
    cfg = {"guard": {"sequence": {"enabled": True, "mode": "heuristic", "policy": {
        "enabled": True,
        "rules": [{"id": "medium-secrets", "action": "medium", "tools": ["*"],
                   "pattern": r"(?i)(?:^|[\s\"']|[\\/])\.env(?:[\s\"']|[\\/]|$)"
                              r"|\b(?:credentials|secrets?)\b",
                   "reason": "toca secretos o credenciales"}],
    }}}}
    assert _risk("write_file", {"path": "workspace/main.py", "content": BACKEND}, cfg) == RISK_LOW
    assert _risk("write_file", {"path": "workspace/.env", "content": "A=1"}, cfg) == RISK_MEDIUM


def test_repeticion_exacta_sigue_escalando_a_medio():
    """El anti-thrash no se toca: la MISMA escritura 3 veces sigue siendo MEDIUM."""
    guard = SequenceGuard(CFG)
    args = {"path": "workspace/main.py", "content": BACKEND}
    for _ in range(2):
        assert asyncio.run(guard.evaluate("write_file", args)).risk == RISK_LOW
        guard.record("write_file", args, True)
    assert asyncio.run(guard.evaluate("write_file", args)).risk == RISK_MEDIUM
