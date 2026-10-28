"""El expansor de `${ENV}` no puede tocar las regex de la config.

La regla `deny-rm-root` del guard de secuencia es:

    (?i)\\brm\\s+-rf\\s+(?:/|~|\\$home|\\$env:userprofile)(?:\\s|$)

`load_config()` expandia tambien los `$VAR` SUELTOS, y en Windows `HOME` existe:
`\\$home` terminaba como `\\C:\\Users\\<user>`, `re.compile` fallaba con "bad
escape \\C" y la regla se descartaba con un warning. O sea: el deny de
`rm -rf ~` quedaba desactivado en silencio.
"""
from __future__ import annotations

import re

from src.core.config import _expand_env, load_config


def test_expande_llaves_y_default(monkeypatch):
    monkeypatch.delenv("NEXUS_TEST_VAR", raising=False)
    assert _expand_env("${NEXUS_TEST_VAR:-./brain}") == "./brain"
    monkeypatch.setenv("NEXUS_TEST_VAR", "C:/x")
    assert _expand_env("${NEXUS_TEST_VAR}") == "C:/x"


def test_no_expande_dentro_de_una_regex(monkeypatch):
    monkeypatch.setenv("HOME", r"C:\Users\probe")
    patron = r"(?i)\brm\s+-rf\s+(?:/|~|\$home|\$env:userprofile)(?:\s|$)"
    assert _expand_env(patron) == patron
    assert re.compile(_expand_env(patron))  # compila


def test_sigue_expandiendo_rutas_sueltas(monkeypatch):
    monkeypatch.setenv("HOME", "C:/Users/probe")
    assert _expand_env("$HOME/.nexus") == "C:/Users/probe/.nexus"


def test_var_desconocida_suelta_se_deja_como_esta():
    assert _expand_env("$NO_EXISTE_XYZ/loquesea") == "$NO_EXISTE_XYZ/loquesea"


def test_todas_las_reglas_del_config_compilan(monkeypatch):
    """Regresion con el config real: ninguna regla puede quedar sin compilar."""
    monkeypatch.setenv("HOME", r"C:\Users\probe")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\probe")
    cfg = load_config()
    rules = ((cfg.get("guard") or {}).get("sequence") or {}).get("policy") or {}
    rules = rules.get("rules") or []
    assert rules, "el config deberia traer reglas de politica"
    for rule in rules:
        patron = rule.get("pattern")
        if patron:
            re.compile(str(patron), re.I)  # no debe lanzar re.error
