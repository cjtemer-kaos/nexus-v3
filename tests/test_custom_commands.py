"""Comandos personalizados en markdown (.nexus/commands/*.md).

Se fija lo que importa para que un comando sea usable y seguro: parseo de
frontmatter, sustitución de argumentos, inclusión de archivos acotada al
proyecto, y que un archivo roto no tumbe el listado ni pise un builtin.
"""
from __future__ import annotations

import asyncio

import pytest

from src.core import custom_commands as cc
from src.core.slash_commands import SlashRegistry, registry as global_registry


@pytest.fixture(autouse=True)
def _clear_cache():
    cc._cache.clear()
    yield
    cc._cache.clear()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_descubre_comando_con_frontmatter(tmp_path):
    d = tmp_path / "commands"
    _write(d / "revisar.md", "---\ndescription: Revisa el diff\nargument-hint: \"[ruta]\"\nalias: [rv]\n---\nRevisá $ARGUMENTS\n")
    specs, errors = cc.load_dirs([d])
    assert errors == []
    assert len(specs) == 1
    spec = specs[0]
    assert spec.name == "revisar"
    assert spec.description == "Revisa el diff"
    assert spec.args_hint == "[ruta]"
    assert spec.aliases == ["rv"]
    assert spec.body == "Revisá $ARGUMENTS"


def test_sin_frontmatter_usa_primera_linea(tmp_path):
    d = tmp_path / "commands"
    _write(d / "simple.md", "\n# Resumen del repo\n\nExplicá la estructura.\n")
    specs, _ = cc.load_dirs([d])
    assert specs[0].name == "simple"
    assert specs[0].description == "Resumen del repo"


def test_nombre_invalido_se_ignora(tmp_path):
    d = tmp_path / "commands"
    _write(d / "Con Espacios.md", "hola\n")
    _write(d / "bien.md", "hola\n")
    specs, errors = cc.load_dirs([d])
    assert [s.name for s in specs] == ["bien"]
    assert any("Con Espacios" in e for e in errors)


def test_sustituye_arguments_y_posicionales(tmp_path):
    d = tmp_path / "commands"
    _write(d / "t.md", "---\ndescription: x\n---\nTodo: $ARGUMENTS | primero: $1 | tercero: $3\n")
    spec = cc.load_dirs([d])[0][0]
    prompt, warns = cc.render(spec, ["uno", "dos"], root=tmp_path)
    assert prompt.rstrip() == "Todo: uno dos | primero: uno | tercero:"
    assert warns == []


def test_incluye_archivo_del_proyecto(tmp_path):
    _write(tmp_path / "notas.md", "contenido real")
    d = tmp_path / "commands"
    _write(d / "t.md", "Leé esto:\n@notas.md\n")
    spec = cc.load_dirs([d])[0][0]
    prompt, warns = cc.render(spec, [], root=tmp_path)
    assert "contenido real" in prompt
    assert "```notas.md" in prompt
    assert warns == []


def test_rechaza_include_fuera_del_proyecto(tmp_path):
    outside = tmp_path.parent / "fuera-del-proyecto.md"
    outside.write_text("SECRETO", encoding="utf-8")
    d = tmp_path / "commands"
    _write(d / "t.md", f"Incluí @{outside.as_posix()}\n")
    spec = cc.load_dirs([d])[0][0]
    prompt, warns = cc.render(spec, [], root=tmp_path)
    assert "SECRETO" not in prompt
    assert any("fuera del proyecto" in w for w in warns)


def test_include_recortado(tmp_path):
    _write(tmp_path / "grande.txt", "a" * (cc.MAX_INCLUDE_CHARS + 500))
    d = tmp_path / "commands"
    _write(d / "t.md", "@grande.txt\n")
    spec = cc.load_dirs([d])[0][0]
    prompt, warns = cc.render(spec, [], root=tmp_path)
    assert len(prompt) < cc.MAX_INCLUDE_CHARS + 200
    assert any("recortado" in w for w in warns)


def test_include_inexistente_no_rompe(tmp_path):
    d = tmp_path / "commands"
    _write(d / "t.md", "@no-existe.md\n")
    spec = cc.load_dirs([d])[0][0]
    prompt, warns = cc.render(spec, [], root=tmp_path)
    assert "@no-existe.md" in prompt
    assert any("no existe" in w for w in warns)


def test_email_no_se_toma_como_include(tmp_path):
    d = tmp_path / "commands"
    _write(d / "t.md", "Escribile a cjtemer@gmail.com\n")
    spec = cc.load_dirs([d])[0][0]
    prompt, warns = cc.render(spec, [], root=tmp_path)
    assert prompt == "Escribile a cjtemer@gmail.com"
    assert warns == []


def test_crlf(tmp_path):
    d = tmp_path / "commands"
    raw = "---\r\ndescription: win\r\n---\r\nhacé algo\r\n"
    (d).mkdir(parents=True, exist_ok=True)
    (d / "win.md").write_bytes(raw.encode("utf-8"))
    specs, errors = cc.load_dirs([d])
    assert errors == []
    assert specs[0].description == "win"
    assert specs[0].body == "hacé algo"


def test_directorio_inexistente_no_rompe(tmp_path):
    specs, errors = cc.load_dirs([tmp_path / "no-va"])
    assert specs == []
    assert errors == []


def test_no_pisa_builtin(tmp_path):
    d = tmp_path / "commands"
    _write(d / "help.md", "---\ndescription: pretende pisar ayuda\n---\nX\n")
    reg = SlashRegistry()
    from src.core.slash_commands import registry as builtin_reg

    help_cmd = builtin_reg.get("help")
    reg.register(help_cmd)
    out = cc.refresh(reg, [d])
    assert out["registered"] == []
    assert any("choca" in s for s in out["skipped"])
    assert reg.get("help") is help_cmd


def test_ejecuta_y_devuelve_send_prompt(tmp_path):
    d = tmp_path / "commands"
    _write(d / "saludar.md", "---\ndescription: saluda\nagent: code\n---\nSaludá a $1 y contá 3 cosas de $ARGUMENTS\n")
    reg = SlashRegistry()
    cc.refresh(reg, [d])
    res = asyncio.run(reg.execute("/saludar Ana tema", {"session_id": "s1"}))
    assert res.ok is True
    assert res.data["action"] == "send_prompt"
    assert res.data["agent"] == "code"
    assert res.data["prompt"].startswith("Saludá a Ana")
    assert "tema" in res.data["prompt"]


def test_comando_vacio_responde_error(tmp_path):
    d = tmp_path / "commands"
    _write(d / "vacio.md", "---\ndescription: nada\n---\n   \n")
    reg = SlashRegistry()
    cc.refresh(reg, [d])
    res = asyncio.run(reg.execute("/vacio", {}))
    assert res.ok is False
    assert "no produce texto" in res.message


def test_api_slash_lista_los_personalizados():
    from fastapi.testclient import TestClient

    from src.api.server import app

    client = TestClient(app)
    r = client.get("/api/slash")
    assert r.status_code == 200
    names = {c["name"] for c in r.json()["commands"]}
    assert "help" in names
    assert "herramientas" in names
    # el refresh no debe romper el listado aunque no haya carpeta de comandos
    assert global_registry.get("help") is not None
