"""Bordes de las tools de archivos, medidas con el registry real.

Defectos encontrados en los barridos (scope temporal, permiso `full`):

1. `edit(old="", new="X", replace_all=true)` DESTRUÍA el archivo:
   `str.count("")` devuelve len+1, así que reemplazaba "nada" entre cada par de
   caracteres — `uno\\ndos` quedaba `XuXnXoX\\nXdXoXsX` — y devolvía
   "editado (14 reemplazo/s)" como si fuera un éxito.
2. `glob(path=<archivo existente>)` decía "directorio no existe: ..." (falso) y
   el modelo reintentaba la misma ruta.
3. `write_file(path=<directorio>)` salía como "[Errno 13] Permission denied",
   indistinguible de un bloqueo del scope jail.
4. `glob(pattern="[")` devolvía `count: 0` sin avisar (Path.glob no lanza con un
   patrón roto): indistinguible de "no hay archivos".
5. `exec(cwd="no-existe")` creaba el directorio y corría el comando ahí.

Y el regex inválido de grep/code_search hablaba en inglés y no decía qué
escapar ("Invalid regex pattern: def (").
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from src.api import server as S
from src.core.scope import Scope, ScopeConfig


@pytest.fixture()
def tools(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "nota.txt").write_text("uno\ndos\ntres\n", encoding="utf-8")
    cfg = S._load_config_safe()
    scope = Scope(ScopeConfig(allowed_roots=(str(tmp_path),), permission="full"),
                  project_root=tmp_path)
    reg = S._build_registry(scope, f"test-files-{uuid.uuid4().hex[:8]}", cfg)

    def llamar(name, args):
        return asyncio.run(reg.dispatch(name, args, scope=scope, hooks=None))

    return llamar, tmp_path


def test_edit_con_old_vacio_no_rompe_el_archivo(tools):
    llamar, raiz = tools
    antes = (raiz / "sub" / "nota.txt").read_text(encoding="utf-8")

    with pytest.raises(ValueError) as e:
        llamar("edit", {"path": "sub/nota.txt", "old": "", "new": "X",
                        "replace_all": True})

    assert "vacío" in str(e.value)
    assert (raiz / "sub" / "nota.txt").read_text(encoding="utf-8") == antes


def test_edit_con_old_vacio_falla_tambien_sin_replace_all(tools):
    llamar, raiz = tools

    with pytest.raises(ValueError):
        llamar("edit", {"path": "sub/nota.txt", "old": "", "new": "X"})

    assert (raiz / "sub" / "nota.txt").read_text(encoding="utf-8").startswith("uno")


def test_glob_sobre_un_archivo_manda_a_grep(tools):
    llamar, raiz = tools
    archivo = str(raiz / "sub" / "nota.txt")

    with pytest.raises(ValueError) as e:
        llamar("glob", {"pattern": "*", "path": archivo})

    msg = str(e.value)
    assert "es un archivo" in msg and "grep" in msg
    assert "no existe" not in msg


def test_glob_sobre_un_directorio_inexistente_sigue_diciendo_no_existe(tools):
    llamar, _ = tools

    with pytest.raises(FileNotFoundError) as e:
        llamar("glob", {"pattern": "*", "path": "no-existe"})

    assert "no existe" in str(e.value)


def test_write_file_sobre_un_directorio_lo_explica(tools):
    llamar, raiz = tools

    with pytest.raises(ValueError) as e:
        llamar("write_file", {"path": "sub", "content": "x"})

    msg = str(e.value)
    assert "es un directorio" in msg and "sub/nota.md" in msg
    assert "Permission denied" not in msg


def test_write_file_sigue_creando_directorios(tools):
    llamar, raiz = tools

    llamar("write_file", {"path": "nuevo/a/b.txt", "content": "hola"})

    assert (raiz / "nuevo" / "a" / "b.txt").read_text(encoding="utf-8") == "hola"


def test_glob_con_patron_roto_no_dice_cero_en_silencio(tools):
    """`Path.glob("[")` no lanza: devolvía count 0 como si no hubiera archivos."""
    llamar, _ = tools

    for roto in ("[", "**/[", "[abc", "sub/[!]"):
        with pytest.raises(ValueError) as e:
            llamar("glob", {"pattern": roto})
        msg = str(e.value)
        assert "patrón glob inválido" in msg, roto
        assert "corchete sin cerrar" in msg, roto
        assert "'*.py'" in msg, roto


def test_glob_con_clase_de_caracteres_valida_sigue_andando(tools):
    llamar, raiz = tools

    out = llamar("glob", {"pattern": "[no]*.txt"})
    assert "nota.txt" in out

    out2 = llamar("glob", {"pattern": "[!x]*.txt"})
    assert "nota.txt" in out2


def test_glob_con_corchete_literal_en_el_nombre_no_es_un_patron_roto(tools):
    llamar, raiz = tools
    (raiz / "bra[ck]et.md").write_text("# ojo\n", encoding="utf-8")

    out = llamar("glob", {"pattern": "*"})
    assert "bra[ck]et.md" in out


def test_grep_con_regex_invalida_dice_que_escapar(tools):
    llamar, _ = tools

    with pytest.raises(ValueError) as e:
        llamar("grep", {"pattern": "def ("})

    msg = str(e.value)
    assert "regex inválida" in msg
    assert "Escapá" in msg
    assert "Invalid regex" not in msg


def test_exec_con_cwd_inexistente_no_crea_el_directorio(tools):
    """Antes creaba el directorio y corría ahí: un typo en cwd daba salida normal."""
    llamar, raiz = tools
    destino = raiz / "no-existe-dir"

    with pytest.raises(FileNotFoundError) as e:
        llamar("exec", {"command": "echo hola", "cwd": "no-existe-dir"})

    msg = str(e.value)
    assert "cwd no existe" in msg
    assert "no crea directorios" in msg
    assert not destino.exists(), "exec no debe crear el cwd por su cuenta"


def test_exec_con_cwd_que_es_archivo_lo_explica(tools):
    llamar, _ = tools

    with pytest.raises(NotADirectoryError) as e:
        llamar("exec", {"command": "echo hola", "cwd": "sub/nota.txt"})

    msg = str(e.value)
    assert "un directorio" in msg and "sub/nota.txt" in msg


