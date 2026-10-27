"""Kernel de Python persistente: estado entre celdas, errores y timeouts."""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from src.core import python_repl as pr


@pytest.fixture(autouse=True)
def _limpio():
    pr.shutdown_all()
    yield
    pr.shutdown_all()


@pytest.fixture()
def kernel(tmp_path):
    k = pr.get_kernel("test", tmp_path, timeout_s=10)
    yield k
    k.kill()


def test_estado_sobrevive_entre_celdas(kernel):
    r1 = kernel.run("x = 40\nimport math")
    assert r1["ok"] is True
    r2 = kernel.run("print(x + 2)")
    assert r2["ok"] is True
    assert r2["stdout"].strip() == "42"
    r3 = kernel.run("math.floor(2.9)")
    assert r3["value"] == "2"
    assert kernel.cells == 3


def test_ultima_expresion_queda_en_underscore(kernel):
    kernel.run("7 * 6")
    r = kernel.run("_ + 1")
    assert r["value"] == "43"


def test_error_devuelve_traceback_y_no_mata_el_kernel(kernel):
    r = kernel.run("1/0")
    assert r["ok"] is False
    assert "ZeroDivisionError" in r["error"]
    r2 = kernel.run("x = 'sigue vivo'\nprint(x)")
    assert r2["ok"] is True
    assert "sigue vivo" in r2["stdout"]


def test_stdout_y_stderr_separados(kernel):
    r = kernel.run("import sys\nprint('out')\nprint('err', file=sys.stderr)")
    assert r["stdout"].strip() == "out"
    assert r["stderr"].strip() == "err"


def test_timeout_mata_el_kernel_y_avisa(kernel):
    t0 = time.time()
    r = kernel.run("import time\ntime.sleep(30)", timeout_s=1)
    assert r["timed_out"] is True
    assert r["ok"] is False
    assert "se perdió" in r["error"] or "perdio" in r["error"]
    assert time.time() - t0 < 10
    # El kernel quedo muerto: la proxima celda arranca limpia.
    assert kernel.alive is False
    r2 = kernel.run("print('nuevo')")
    assert r2["ok"] is True
    assert r2["restarted"] is True
    assert "nuevo" in r2["stdout"]


def test_codigo_vacio_se_rechaza(kernel):
    with pytest.raises(pr.ReplError):
        kernel.run("   ")


def test_reset_y_drop_borran_el_estado(tmp_path):
    k = pr.get_kernel("otra", tmp_path, timeout_s=10)
    k.run("v = 1")
    assert pr.drop_kernel("otra") is True
    k2 = pr.get_kernel("otra", tmp_path, timeout_s=10)
    r = k2.run("print('v' in globals())")
    assert "False" in r["stdout"]
    k2.kill()


def test_kernels_separados_por_sesion(tmp_path):
    a = pr.get_kernel("a", tmp_path, timeout_s=10)
    b = pr.get_kernel("b", tmp_path, timeout_s=10)
    a.run("marca = 'A'")
    assert "False" in b.run("print('marca' in globals())")["stdout"]
    a.kill()
    b.kill()


def test_kernel_ocioso_se_recicla(tmp_path):
    k = pr.get_kernel("ocio", tmp_path, timeout_s=10, idle_s=0)
    k.run("z = 1")
    k.last_used = time.time() - 10
    k2 = pr.get_kernel("ocio", tmp_path, timeout_s=10, idle_s=1)
    assert k2 is not k
    assert "False" in k2.run("print('z' in globals())")["stdout"]
    k2.kill()


def test_info_del_kernel(kernel):
    kernel.run("1")
    info = kernel.info()
    assert info["alive"] is True
    assert info["cells"] == 1
    assert Path(info["cwd"]).is_dir()
