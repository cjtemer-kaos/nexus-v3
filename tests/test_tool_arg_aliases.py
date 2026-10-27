"""Alias de nombres de tool y de argumentos (paridad con opencode/Claude Code).

Dos clases de defecto, las dos silenciosas:

1. **Argumento con otro nombre**: `validate_args` descarta las claves que el
   schema no declara (tolerancia a modelos chicos que inventan campos). El
   problema es cuando la clave descartada era la unica que importaba: `repo_map`
   llama `root` a su unica ruta, asi que `repo_map(path="sub")` devolvia el mapa
   del proyecto ENTERO con 200, como si hubiera mapeado `sub`.

2. **Tool con otro nombre**: los modelos entrenados con opencode/Claude Code
   emiten `read`, `write`, `list`, `bash`. Antes recibian
   `KeyError: herramienta no encontrada`, aunque la tool existiera con otro
   nombre (`read_file`, `write_file`, `list_dir`, `exec`).
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.tool_registry import ARG_ALIASES, TOOL_ALIASES, validate_args  # noqa: E402


# ------------------------------------------------------- validate_args: alias


def _schema(**props):
    return {"type": "object", "properties": props, "additionalProperties": False}


def test_filepath_se_convierte_en_path():
    esq = _schema(path={"type": "string"}, content={"type": "string"})

    out = validate_args("write_file", {"filePath": "a.txt", "content": "hola"}, esq)

    assert out == {"path": "a.txt", "content": "hola"}


def test_oldstring_newstring_se_convierten_en_old_new():
    esq = _schema(path={"type": "string"}, old={"type": "string"},
                  new={"type": "string"}, replace_all={"type": "boolean"})

    out = validate_args("edit", {"filePath": "a.py", "oldString": "uno",
                                 "newString": "UNO", "replaceAll": "true"}, esq)

    assert out == {"path": "a.py", "old": "uno", "new": "UNO", "replace_all": True}


def test_el_alias_no_pisa_un_parametro_real_con_ese_nombre():
    """Si el schema declara `path` y `root`, manda el valor explicito de `path`."""
    esq = _schema(path={"type": "string"}, root={"type": "string"})

    out = validate_args("x", {"path": "real", "root": "raiz"}, esq)

    assert out == {"path": "real", "root": "raiz"}


def test_el_alias_no_aplica_si_el_destino_no_existe():
    esq = _schema(patch={"type": "string"})

    out = validate_args("apply_patch", {"filePath": "a.txt", "patch": "x"}, esq)

    assert out == {"patch": "x"}, "sin destino valido se descarta como siempre"


def test_las_claves_inventadas_se_siguen_descartando():
    esq = _schema(path={"type": "string"})

    out = validate_args("read_file", {"path": "a", "explicacion": "porque si"}, esq)

    assert out == {"path": "a"}


def test_query_y_pattern_se_mapean_segun_el_schema():
    assert validate_args("grep", {"query": "x"}, _schema(pattern={"type": "string"})) == {"pattern": "x"}
    assert validate_args("code_search", {"pattern": "x"},
                         _schema(query={"type": "string"})) == {"query": "x"}


def test_los_alias_declarados_tienen_destino_conocido():
    """El mapa es texto: si alguien agrega un alias a una tool que no existe, se
    entera aca y no en produccion."""
    destinos_args = {d for d in ARG_ALIASES.values()}
    assert destinos_args <= {"path", "root", "old", "new", "replace_all",
                             "pattern", "query", "command", "topic_key", "job_id"}
    assert set(TOOL_ALIASES.values()) <= {
        "read_file", "write_file", "list_dir", "exec", "apply_patch",
        "web_fetch", "todo_write", "todo_read"}


# --------------------------------------------------- dispatch: tool y args


@pytest.fixture()
def tools(monkeypatch):
    from src.api import server as S

    monkeypatch.setattr("src.core.checkpoints.snapshot", lambda *a, **k: None)
    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, "test-alias", cfg)

    def usar(name, **args):
        raw = asyncio.run(reg.dispatch(name, args, scope=scope, hooks=None))
        return raw if isinstance(raw, str) else getattr(raw, "output", str(raw))

    return usar


def test_read_con_filepath_funciona(tmp_path, tools):
    f = tmp_path / "nota.txt"
    f.write_bytes("hola ñ\n".encode("utf-8"))

    out = tools("read", filePath=str(f))

    assert "hola ñ" in out


def test_write_y_list_con_alias(tmp_path, tools):
    f = tmp_path / "nuevo.txt"

    tools("write", filePath=str(f), content="uno\n")
    assert f.read_bytes() == b"uno\n"

    out = tools("list", path=str(tmp_path))
    assert "nuevo.txt" in out


def test_edit_con_oldstring_newstring(tmp_path, tools):
    f = tmp_path / "a.py"
    f.write_bytes("uno\ndos\n".encode("utf-8"))

    tools("edit", filePath=str(f), oldString="dos", newString="DOS")

    assert f.read_bytes() == b"uno\nDOS\n"


def test_bash_corre_como_exec(tmp_path, tools):
    out = tools("bash", command="echo alias-ok")

    assert "alias-ok" in out


def test_el_alias_de_bash_sigue_pasando_por_el_guard_de_scope(tmp_path, tools, monkeypatch):
    """Si el alias se resolviera DESPUES del guard, un `bash` no pasaria por la
    blocklist de `exec`."""
    from src.api import server as S

    vistos: list[str] = []
    real = S._build_scope

    def espia(cfg, *a, **k):
        sc = real(cfg, *a, **k)
        orig = sc.check_tool

        def check_tool(name, args, *aa, **kk):
            vistos.append(name)
            return orig(name, args, *aa, **kk)

        sc.check_tool = check_tool
        return sc

    cfg = S._load_config_safe()
    scope = espia(cfg)
    reg = S._build_registry(scope, "test-alias-guard", cfg)

    asyncio.run(reg.dispatch("bash", {"command": "echo x"}, scope=scope, hooks=None))

    assert vistos and set(vistos) == {"exec"}, f"el guard tiene que ver el nombre canonico: {vistos}"


def test_repo_map_acepta_path_y_mapea_ese_directorio(tmp_path, tools):
    sub = tmp_path / "paquete"
    sub.mkdir()
    (sub / "unico.py").write_text("def solo_aqui():\n    return 1\n", encoding="utf-8")
    (tmp_path / "fuera.py").write_text("def fuera():\n    return 2\n", encoding="utf-8")

    j = json.loads(tools("repo_map", path=str(sub), max_files=50))

    rutas = [f["path"] for f in j["files"]]
    assert any("unico.py" in r for r in rutas), rutas
    assert not any("fuera.py" in r for r in rutas), rutas


def test_grep_acepta_query(tmp_path, tools):
    f = tmp_path / "a.py"
    f.write_text("def objetivo():\n    return 1\n", encoding="utf-8")

    out = tools("grep", query="objetivo", path=str(f))

    assert "a.py:1:" in out
