"""Tools de busqueda: `grep` (no existia), acentos y limite total.

Medido contra el registry real:

1. **No habia tool `grep`.** `scope.py` y `sequence_guard.py` ya la nombraban
   entre las tools conocidas, pero el registry nunca la registraba: un modelo
   entrenado con Claude Code/opencode pedia `grep` y recibia
   `KeyError: herramienta no encontrada`.

2. **`code_search` devolvia mojibake en los acentos**: `caÃ±Ã³n` por `cañón`.
   La causa era `subprocess.run(..., text=True)`: rg escribe UTF-8 y se decodificaba
   con el locale (cp1252). Y el fallback en Python leia con utf-8+replace, asi que
   un archivo cp1252 nunca matcheaba una busqueda con acentos.

3. **`limit` no era el total**: rg usa `--max-count` POR ARCHIVO, asi que con
   `limit=3` y 10 coincidencias en un archivo volvian 10.

4. **`glob '*.py'` devolvia 0** en un arbol con los .py en subdirectorios:
   pathlib matchea solo el nivel superior, ripgrep/opencode matchean a cualquier
   profundidad. Y un `path` inexistente devolvia `count: 0` en vez de un error.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture()
def tools(tmp_path):
    from src.api import server as S

    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, "test-grep", cfg)

    def usar(name, **args):
        raw = asyncio.run(reg.dispatch(name, args, scope=scope, hooks=None))
        return raw if isinstance(raw, str) else getattr(raw, "output", str(raw))

    def intentar(name, **args):
        try:
            return "ok", usar(name, **args)
        except Exception as e:  # noqa: BLE001 - el mensaje es el dato del test
            return "err", f"{type(e).__name__}: {e}"

    return usar, intentar


@pytest.fixture()
def arbol(tmp_path):
    (tmp_path / "uno.py").write_text("def saludar():\n    return 'cañón del sur ñandú'\n",
                                     encoding="utf-8")
    (tmp_path / "dos.py").write_text("".join(f"match numero {i} ñ\n" for i in range(10)),
                                     encoding="utf-8")
    (tmp_path / "tres.txt").write_text("otro match aca\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "cuatro.py").write_text("match en subdirectorio\n", encoding="utf-8")
    # cp1252: solo lo encuentra el fallback si lee con el encoding real
    (tmp_path / "legacy.txt").write_bytes("resumen: cañón del sur\n".encode("cp1252"))
    return tmp_path


# ------------------------------------------------------------------- grep


def test_existe_la_tool_grep(tools):
    from src.api import server as S

    cfg = S._load_config_safe()
    registry = S._build_registry(S._build_scope(cfg), "test-grep-catalogo", cfg)
    nombres = [e.name for e in registry.list()]
    assert "grep" in nombres, "grep es el nombre que usan los modelos"
    assert "glob" in nombres and "read_file" in nombres


def test_grep_encuentra_y_no_devuelve_mojibake(arbol, tools):
    usar, _ = tools

    out = usar("grep", pattern="ñandú", path=str(arbol))

    assert "uno.py:2:" in out
    assert "cañón del sur ñandú" in out
    assert "Ã" not in out, "rg escribe UTF-8: no se decodifica con el locale"


def test_grep_respeta_el_limite_total(arbol, tools):
    usar, _ = tools

    out = usar("grep", pattern="match", path=str(arbol), limit=3)

    assert len(out.splitlines()) == 3, out


def test_grep_filtra_por_include(arbol, tools):
    usar, _ = tools

    out = usar("grep", pattern="match", path=str(arbol), include="*.py")

    assert "dos.py" in out and "cuatro.py" in out
    assert "tres.txt" not in out


def test_grep_sin_coincidencias_lo_dice(arbol, tools):
    usar, _ = tools

    out = usar("grep", pattern="zzzz_no_existe", path=str(arbol))

    assert "sin coincidencias" in out


def test_grep_con_regex_invalida_falla_con_mensaje(arbol, tools):
    _, intentar = tools

    est, out = intentar("grep", pattern="(", path=str(arbol))

    assert est == "err"
    assert "regex" in out.lower() or "pattern" in out.lower()


def test_grep_con_path_inexistente_falla(arbol, tools):
    _, intentar = tools

    est, out = intentar("grep", pattern="x", path=str(arbol / "no-existe"))

    assert est == "err"
    assert "not found" in out.lower() or "no existe" in out.lower()


def test_grep_lee_archivos_cp1252(arbol, tools):
    """El fallback en Python leia utf-8+replace: 'cañón' en cp1252 no matcheaba."""
    usar, _ = tools

    out = usar("grep", pattern="cañón", path=str(arbol / "legacy.txt"))

    assert "legacy.txt:1:" in out, out


# ------------------------------------------------------------ code_search


def test_code_search_no_devuelve_mojibake(arbol, tools):
    usar, _ = tools

    out = usar("code_search", query="ñandú", path=str(arbol))
    j = json.loads(out)

    assert j["count"] == 1
    assert "cañón del sur ñandú" in j["results"][0]["text"]
    assert "Ã" not in out


def test_code_search_limite_es_total(arbol, tools):
    usar, _ = tools

    j = json.loads(usar("code_search", query="match", path=str(arbol), limit=3))

    assert j["count"] == 3
    assert len(j["results"]) == 3


# ------------------------------------------------------------------- glob


def test_glob_sin_separadores_busca_en_todo_el_arbol(arbol, tools):
    usar, _ = tools

    j = json.loads(usar("glob", pattern="*.py", path=str(arbol)))

    nombres = {Path(p).name for p in j["matches"]}
    assert nombres == {"uno.py", "dos.py", "cuatro.py"}, j["matches"]
    assert j.get("pattern_effective") == "**/*.py"


def test_glob_con_separadores_sigue_siendo_explicito(arbol, tools):
    usar, _ = tools

    j = json.loads(usar("glob", pattern="sub/*.py", path=str(arbol)))

    assert [Path(p).name for p in j["matches"]] == ["cuatro.py"]
    assert "pattern_effective" not in j


def test_glob_de_directorio_inexistente_falla(arbol, tools):
    _, intentar = tools

    est, out = intentar("glob", pattern="*.py", path=str(arbol / "no-existe"))

    assert est == "err"
    assert "no existe" in out


def test_glob_sigue_devolviendo_tamanos(arbol, tools):
    usar, _ = tools

    j = json.loads(usar("glob", pattern="*.txt", path=str(arbol)))

    assert j["count"] >= 2
    assert j["largest"]["size_bytes"] > 0
    assert all(v > 0 for v in j["sizes"].values())
