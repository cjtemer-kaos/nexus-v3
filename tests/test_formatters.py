"""Formateadores: resolución por extensión, cambio real y fallos que no rompen.

Los tests no dependen de ruff/prettier instalados: usan un "formateador" falso
(un script de Python) inyectado por config, así se prueba el contrato del
módulo (status, detección de cambio, timeout, error) y no el entorno.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.core import formatters as fm


def _script(tmp_path: Path, name: str, body: str) -> Path:
    p = tmp_path / "tools" / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body, encoding="utf-8")
    return p


@pytest.fixture()
def proyecto(tmp_path):
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    return root


def _cfg(ext: str, script: Path, **extra) -> dict:
    return {"formatters": {"commands": {ext: [sys.executable, str(script), "{file}"], **extra}}}


def test_sustituye_el_placeholder(proyecto):
    script = _script(proyecto, "upper.py", "import sys,pathlib;p=pathlib.Path(sys.argv[1]);p.write_text(p.read_text(encoding='utf-8').upper(),encoding='utf-8')\n")
    target = proyecto / "src" / "a.py"
    target.write_text("hola\n", encoding="utf-8")
    out = fm.format_file(target, cfg=_cfg(".py", script), root=proyecto)
    assert out["status"] == "formatted"
    assert out["formatter"] == "python"
    assert target.read_text(encoding="utf-8") == "HOLA\n"


def test_sin_cambios_reporta_clean(proyecto):
    script = _script(proyecto, "noop.py", "import sys\n")
    target = proyecto / "src" / "b.py"
    target.write_text("igual\n", encoding="utf-8")
    out = fm.format_file(target, cfg=_cfg(".py", script), root=proyecto)
    assert out["status"] == "clean"
    assert target.read_text(encoding="utf-8") == "igual\n"


def test_error_del_formateador_no_lanza(proyecto):
    script = _script(proyecto, "fail.py", "import sys;sys.stderr.write('boom de formato');sys.exit(3)\n")
    target = proyecto / "src" / "c.py"
    target.write_text("x =1\n", encoding="utf-8")
    out = fm.format_file(target, cfg=_cfg(".py", script), root=proyecto)
    assert out["status"] == "error"
    assert "boom" in str(out["detail"])
    assert target.read_text(encoding="utf-8") == "x =1\n"


def test_timeout_no_lanza_y_avisa(proyecto):
    script = _script(proyecto, "lento.py", "import time;time.sleep(5)\n")
    target = proyecto / "src" / "d.py"
    target.write_text("x = 1\n", encoding="utf-8")
    cfg = _cfg(".py", script)
    cfg["formatters"]["timeout_s"] = 0.3
    out = fm.format_file(target, cfg=cfg, root=proyecto)
    assert out["status"] == "error"
    assert "timeout" in str(out["detail"])


def test_deshabilitado_no_corre_nada(proyecto):
    script = _script(proyecto, "upper.py", "import sys,pathlib;p=pathlib.Path(sys.argv[1]);p.write_text('CAMBIADO',encoding='utf-8')\n")
    target = proyecto / "src" / "e.py"
    target.write_text("hola\n", encoding="utf-8")
    cfg = _cfg(".py", script)
    cfg["formatters"]["enabled"] = False
    out = fm.format_file(target, cfg=cfg, root=proyecto)
    assert out["status"] == "skipped"
    assert target.read_text(encoding="utf-8") == "hola\n"


def test_fuera_del_proyecto_no_se_toca(proyecto, tmp_path):
    script = _script(proyecto, "upper.py", "import sys,pathlib;p=pathlib.Path(sys.argv[1]);p.write_text('PWNED',encoding='utf-8')\n")
    outside = tmp_path / "afuera.py"
    outside.write_text("hola\n", encoding="utf-8")
    out = fm.format_file(outside, cfg=_cfg(".py", script), root=proyecto)
    assert out["status"] == "skipped"
    assert "fuera del proyecto" in str(out["detail"])
    assert outside.read_text(encoding="utf-8") == "hola\n"


def test_extension_sin_formateador_se_saltea(proyecto):
    target = proyecto / "src" / "raro.xyz"
    target.write_text("hola\n", encoding="utf-8")
    out = fm.format_file(target, cfg={"formatters": {"commands": {}}}, root=proyecto)
    assert out["status"] == "skipped"
    assert "sin formateador" in str(out["detail"])


def test_archivo_inexistente_se_saltea(proyecto):
    out = fm.format_file(proyecto / "src" / "no-va.py", cfg={"formatters": {}}, root=proyecto)
    assert out["status"] == "skipped"


def test_comando_explicito_inexistente_se_ignora(proyecto):
    cfg = {"formatters": {"commands": {".py": ["no-existe-este-binario-nunca", "{file}"]}}}
    assert fm.resolve(proyecto / "src" / "a.py", cfg=cfg) is None


def test_no_usa_npx(proyecto):
    fmt = fm.resolve(proyecto / "src" / "a.tsx", cfg={"formatters": {"commands": {}}})
    if fmt is not None:
        assert "npx" not in " ".join(fmt.argv).lower()


def test_extension_desconocida_no_resuelve(proyecto):
    assert fm.resolve(proyecto / "src" / "a.zzz", cfg={"formatters": {"commands": {}}}) is None


def test_resuelve_python_si_hay_herramienta(proyecto):
    fmt = fm.resolve(proyecto / "src" / "a.py", cfg={"formatters": {"commands": {}}})
    if fmt is not None:
        assert fmt.name in {"ruff", "black"}
        assert fmt.argv and Path(fmt.argv[0]).name.lower().startswith(("ruff", "black"))


def test_summarize(proyecto):
    res = [
        {"status": "formatted", "file": "a", "detail": ""},
        {"status": "clean", "file": "b", "detail": ""},
        {"status": "error", "file": "c", "detail": "rompió"},
    ]
    texto = fm.summarize(res)
    assert "1 formateado(s)" in texto
    assert "1 error(es)" in texto
    assert "rompió" in texto


def test_format_files_sigue_con_el_resto(proyecto):
    script = _script(proyecto, "upper.py", "import sys,pathlib;p=pathlib.Path(sys.argv[1]);p.write_text(p.read_text(encoding='utf-8').upper(),encoding='utf-8')\n")
    ok = proyecto / "src" / "ok.py"
    ok.write_text("hola\n", encoding="utf-8")
    missing = proyecto / "src" / "no-existe.py"
    out = fm.format_files([ok, missing], cfg=_cfg(".py", script), root=proyecto)
    assert out[0]["status"] == "formatted"
    assert out[1]["status"] == "skipped"
    assert ok.read_text(encoding="utf-8") == "HOLA\n"
