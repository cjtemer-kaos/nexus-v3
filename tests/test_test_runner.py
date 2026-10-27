"""Runner de tests: deteccion, parseo de resumenes y corrida real de pytest."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.tools import test_runner as tr


PYTEST_OK = """\
tests/test_x.py ..                                                       [100%]

============================== 2 passed in 0.12s ==============================
"""

PYTEST_FAIL = """\
tests/test_x.py F.                                                       [ 50%]
=================================== FAILURES ===================================
__________________________________ test_roto ___________________________________

    def test_roto():
>       assert 1 == 2
E       assert 1 == 2

tests/test_x.py:3: AssertionError
=========================== short test summary info ============================
FAILED tests/test_x.py::test_roto - assert 1 == 2
========================= 1 failed, 1 passed in 0.20s ==========================
"""

JEST_FAIL = """\
 FAIL  src/a.test.ts
  ● suma › suma mal

    expect(received).toBe(expected)

Tests:       1 failed, 2 passed, 3 total
"""

CARGO_FAIL = """\
running 2 tests
test ok ... ok
test roto ... FAILED
test result: FAILED. 1 passed; 1 failed; 0 ignored
---- roto stdout ----
assertion failed
"""

GO_FAIL = """\
--- FAIL: TestSuma (0.00s)
    suma_test.go:12: queria 3, vino 4
FAIL
"""


def test_parse_pytest_ok():
    r = tr.parse_pytest(PYTEST_OK)
    assert r["counts"]["passed"] == 2
    assert r["failures"] == []
    assert "2 passed" in r["summary_line"]


def test_parse_pytest_fallos():
    r = tr.parse_pytest(PYTEST_FAIL)
    assert r["counts"]["failed"] == 1
    assert r["counts"]["passed"] == 1
    assert r["failures"][0]["name"] == "tests/test_x.py::test_roto"
    assert "assert 1 == 2" in r["failures"][0]["detail"]


def test_parse_pytest_sin_banner_infiere_por_progreso():
    r = tr.parse_pytest("..F\n")
    assert r["counts"] == {"passed": 2, "failed": 1}


def test_parse_pytest_resumen_pelado_sin_decoracion():
    """pytest real (sin `====`): `1 failed, 1 passed in 0.08s`."""
    r = tr.parse_pytest("F.                                    [100%]\n1 failed, 1 passed in 0.08s\n")
    assert r["counts"] == {"failed": 1, "passed": 1}
    assert r["summary_line"] == "1 failed, 1 passed in 0.08s"


def test_parse_pytest_sin_tests():
    r = tr.parse_pytest("no tests ran in 0.02s\n")
    assert r["counts"] == {}
    assert "no tests ran" in r["summary_line"]


def test_parse_npm():
    r = tr.parse_npm(JEST_FAIL)
    assert r["counts"]["failed"] == 1
    assert r["counts"]["passed"] == 2
    assert any("suma" in f["name"] for f in r["failures"])


def test_parse_cargo():
    r = tr.parse_cargo(CARGO_FAIL)
    assert r["counts"]["passed"] == 1
    assert r["counts"]["failed"] == 1
    assert r["failures"][0]["name"] == "roto"


def test_parse_go():
    r = tr.parse_go(GO_FAIL)
    assert r["counts"]["failed"] == 1
    assert "queria 3" in r["failures"][0]["detail"]


def test_detect_pytest_y_npm(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text('{"scripts":{"test":"vitest"}}', encoding="utf-8")
    found = tr.detect(tmp_path)
    assert "pytest" in found
    assert "npm" in found


def test_detect_vacio(tmp_path):
    assert tr.detect(tmp_path) == []


def test_runner_auto_sin_runner_falla(tmp_path):
    with pytest.raises(RuntimeError):
        tr.run(tmp_path, runner="auto")


def test_runner_desconocido():
    with pytest.raises(RuntimeError):
        tr.run(Path("."), runner="magia")


def test_corrida_real_de_pytest(tmp_path):
    """Un proyecto de verdad: 1 pasa / 1 falla, y los conteos salen del output."""
    (tmp_path / "test_demo.py").write_text(
        "def test_ok():\n    assert True\n\n"
        "def test_roto():\n    assert 1 == 2\n", encoding="utf-8")
    res = tr.run(tmp_path, runner="pytest", timeout=180)
    assert res["exit_code"] != 0
    assert res["ok"] is False
    assert res["counts"].get("failed") == 1
    assert res["counts"].get("passed") == 1
    assert any("test_roto" in f["name"] for f in res["failures"])
    assert res["command"][:2] == [sys.executable, "-m"]


def test_corrida_que_no_termina_devuelve_timed_out(tmp_path):
    (tmp_path / "test_lento.py").write_text(
        "import time\n\ndef test_lento():\n    time.sleep(30)\n", encoding="utf-8")
    res = tr.run(tmp_path, runner="pytest", timeout=2)
    assert res["timed_out"] is True
    assert res["ok"] is False


def test_split_command_no_interpreta():
    assert tr.split_command("tests/test_a.py -x") == ["tests/test_a.py", "-x"]
