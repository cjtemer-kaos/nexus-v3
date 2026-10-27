"""Codificacion de archivos: leer con la real y escribir con la misma.

Defecto medido: un `.txt` en cp1252 (`resumen: cañón del sur` /
`no tocar: ñandú`) leido con `utf-8 + errors="replace"` devolvia
`resumen: ca\ufffd\ufffdn del sur`. El modelo copiaba ese fragmento como `old`
del `edit`, el reemplazo lo encontraba... y la linea que NUNCA toco terminaba
como `no tocar: \ufffdand\ufffd`. Bytes originales perdidos, en silencio.

La regla que se prueba aca: se lee con el encoding que decodifica y se escribe
con ESE MISMO encoding; si el texto nuevo no entra, se falla sin escribir.
"""
from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.file_text import (  # noqa: E402
    detectar_bom,
    encode_text,
    read_text_file,
    write_preserving_encoding,
)


@pytest.fixture()
def tools(tmp_path, monkeypatch):
    from src.api import server as S

    # El snapshot real escribe en data/checkpoints/<session>: en un test no hace
    # falta y no debe tocar los datos del usuario.
    monkeypatch.setattr("src.core.checkpoints.snapshot", lambda *a, **k: None)
    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, "test-enc", cfg)

    def usar(name, **args):
        raw = asyncio.run(reg.dispatch(name, args, scope=scope, hooks=None))
        return raw if isinstance(raw, str) else getattr(raw, "output", str(raw))

    def intentar(name, **args):
        try:
            return "ok", usar(name, **args)
        except Exception as e:  # noqa: BLE001 - el mensaje es el dato del test
            return "err", f"{type(e).__name__}: {e}"

    return usar, intentar


def _cp1252(tmp_path, nombre="notas.txt"):
    f = tmp_path / nombre
    f.write_bytes("resumen: cañón del sur\r\nno tocar: ñandú\r\n".encode("cp1252"))
    return f


# ------------------------------------------------------------ read_text_file


def test_cp1252_se_decodifica_con_acentos_reales(tmp_path):
    f = _cp1252(tmp_path)

    texto, enc = read_text_file(f)

    assert enc == "cp1252"
    assert texto == "resumen: cañón del sur\r\nno tocar: ñandú\r\n"
    assert "\ufffd" not in texto


def test_utf8_sin_bom_no_se_confunde_con_utf8_sig(tmp_path):
    """Si `utf-8-sig` ganara, cada escritura le agregaria un BOM que no tenia."""
    f = tmp_path / "plano.txt"
    f.write_bytes("hola ñ\n".encode("utf-8"))

    texto, enc = read_text_file(f)

    assert enc == "utf-8"
    assert texto == "hola ñ\n"
    assert not f.read_bytes().startswith(b"\xef\xbb\xbf")


def test_bom_utf8_se_detecta_y_no_aparece_en_el_texto(tmp_path):
    f = tmp_path / "bom.txt"
    f.write_bytes(b"\xef\xbb\xbf" + "acentos: ñ\n".encode("utf-8"))

    texto, enc = read_text_file(f)

    assert enc == "utf-8-sig"
    assert texto == "acentos: ñ\n"
    assert "\ufeff" not in texto
    assert detectar_bom(f.read_bytes()[:8]) == "utf-8-sig"


def test_utf16_se_lee_completo(tmp_path):
    f = tmp_path / "windows.txt"
    f.write_text("texto en utf-16: ñandú", encoding="utf-16")

    texto, enc = read_text_file(f)

    assert enc == "utf-16"
    assert texto == "texto en utf-16: ñandú"


def test_encode_text_avisa_si_el_caracter_no_entra():
    with pytest.raises(ValueError) as e:
        encode_text("flecha →", "cp1252")
    assert "no puede representar" in str(e.value)
    assert "cp1252" in str(e.value)


def test_write_preserving_encoding_archivo_nuevo_es_utf8(tmp_path):
    f = tmp_path / "nuevo.txt"

    enc, convertido = write_preserving_encoding(f, "hola ñ\n")

    assert (enc, convertido) == ("utf-8", False)
    assert f.read_bytes() == "hola ñ\n".encode("utf-8")


def test_write_preserving_encoding_respeta_cp1252(tmp_path):
    f = _cp1252(tmp_path)

    enc, convertido = write_preserving_encoding(f, "otra cosa: ñ\n")

    assert (enc, convertido) == ("cp1252", False)
    assert f.read_bytes() == "otra cosa: ñ\n".encode("cp1252")


def test_write_preserving_encoding_convierte_si_no_entra(tmp_path):
    f = _cp1252(tmp_path)

    enc, convertido = write_preserving_encoding(f, "flecha →\n")

    assert (enc, convertido) == ("utf-8", True)
    assert f.read_text(encoding="utf-8") == "flecha →\n"


# ----------------------------------------------------------- tool read_file


def test_read_file_muestra_los_acentos_y_avisa_el_encoding(tmp_path, tools):
    usar, _ = tools
    f = _cp1252(tmp_path)

    out = usar("read_file", path=str(f))

    assert "resumen: cañón del sur" in out
    assert "no tocar: ñandú" in out
    assert "\ufffd" not in out
    assert "[codificacion: cp1252" in out, "el modelo tiene que saber que no es utf-8"


def test_read_file_de_binario_falla_en_vez_de_devolver_mojibake(tmp_path, tools):
    _, intentar = tools
    f = tmp_path / "falso.exe"
    f.write_bytes(b"MZ\x00\x03\x00\x00" + b"\x00" * 4000)

    est, out = intentar("read_file", path=str(f))

    assert est == "err"
    assert "binario" in out
    assert "\ufffd" not in out


def test_read_file_utf16_con_bom_se_lee(tmp_path, tools):
    usar, _ = tools
    f = tmp_path / "utf16.txt"
    f.write_text("ñandú en utf-16", encoding="utf-16")

    out = usar("read_file", path=str(f))

    assert "ñandú en utf-16" in out
    assert "\ufffd" not in out


# --------------------------------------------------------------- tool edit


def test_edit_preserva_cp1252_y_deja_intacta_la_linea_no_tocada(tmp_path, tools):
    usar, _ = tools
    f = _cp1252(tmp_path)
    antes = f.read_bytes()
    linea = [ln for ln in usar("read_file", path=str(f)).splitlines()
             if ln.startswith("resumen")][0]

    out = usar("edit", path=str(f), old=linea, new="RESUMEN: canon del sur")

    assert "codificacion preservada: cp1252" in out
    despues = f.read_bytes()
    assert despues.decode("cp1252").splitlines() == [
        "RESUMEN: canon del sur",
        "no tocar: ñandú",
    ]
    # La linea que el modelo nunca miro tiene que quedar byte por byte igual.
    assert b"no tocar: \xf1and\xfa" in despues
    assert b"\xef\xbf\xbd" not in despues
    assert antes.endswith(b"no tocar: \xf1and\xfa\r\n")


def test_edit_falla_sin_escribir_si_el_texto_no_entra_en_el_encoding(tmp_path, tools):
    usar, intentar = tools
    f = _cp1252(tmp_path)
    antes = f.read_bytes()

    est, out = intentar("edit", path=str(f), old="no tocar: ñandú", new="flecha →")

    assert est == "err"
    assert "no puede representar" in out
    assert f.read_bytes() == antes, "no se puede escribir nada"


def test_edit_conserva_el_bom_utf8(tmp_path, tools):
    usar, _ = tools
    f = tmp_path / "bom.txt"
    f.write_bytes(b"\xef\xbb\xbf" + "uno\nacentos: ñ\n".encode("utf-8"))

    leido = usar("read_file", path=str(f))
    usar("edit", path=str(f), old="uno", new="UNO")

    datos = f.read_bytes()
    assert datos.startswith(b"\xef\xbb\xbf"), "el BOM no puede perderse"
    assert datos.decode("utf-8-sig") == "UNO\nacentos: ñ\n"


def test_edit_no_agrega_bom_a_un_utf8_plano(tmp_path, tools):
    usar, _ = tools
    f = tmp_path / "plano.txt"
    f.write_bytes("uno\nacentos: ñ\n".encode("utf-8"))

    usar("edit", path=str(f), old="uno", new="UNO")

    datos = f.read_bytes()
    assert not datos.startswith(b"\xef\xbb\xbf")
    assert datos == "UNO\nacentos: ñ\n".encode("utf-8")


def test_edit_de_utf16_sigue_en_utf16(tmp_path, tools):
    usar, _ = tools
    f = tmp_path / "utf16.txt"
    f.write_text("uno\nacentos: ñandú\n", encoding="utf-16")

    usar("edit", path=str(f), old="uno", new="UNO")

    assert f.read_text(encoding="utf-16") == "UNO\nacentos: ñandú\n"


# ----------------------------------------------------------- tool write_file


def test_write_file_no_traduce_lf_a_crlf(tmp_path, tools):
    """`Path.write_text` en Windows convierte cada \\n en \\r\\n: el archivo
    quedaba distinto de lo que el modelo escribio (diffs de archivo completo)."""
    usar, _ = tools
    f = tmp_path / "lf.txt"

    usar("write_file", path=str(f), content="a\nb\nñ\n")

    assert f.read_bytes() == "a\nb\nñ\n".encode("utf-8")


def test_write_file_preserva_cp1252_al_reescribir(tmp_path, tools):
    usar, _ = tools
    f = _cp1252(tmp_path)

    out = usar("write_file", path=str(f), content="todo nuevo: ñ\n")

    assert "codificacion cp1252" in out
    assert f.read_bytes() == "todo nuevo: ñ\n".encode("cp1252")


def test_write_file_convierte_avisando_si_el_texto_no_entra(tmp_path, tools):
    usar, _ = tools
    f = _cp1252(tmp_path)

    out = usar("write_file", path=str(f), content="flecha →\n")

    assert "convertido a utf-8" in out
    assert f.read_text(encoding="utf-8") == "flecha →\n"


# ------------------------------------------------------------- tool apply_patch


def _patch_update(path, borrar, poner):
    return (f"*** Begin Patch\n*** Update File: {path}\n@@\n-{borrar}\n+{poner}\n"
            f"*** End Patch")


@pytest.fixture()
def patch_aislado(monkeypatch):
    """apply_patch crea un checkpoint git real del repo: en tests no."""
    monkeypatch.setattr("src.core.checkpoints.snapshot", lambda *a, **k: None)
    monkeypatch.setattr("src.core.checkpoints.restore", lambda *a, **k: None)
    try:
        monkeypatch.setattr("src.core.git_checkpoints.create_git_checkpoint",
                            lambda *a, **k: {"ok": False})
    except Exception:
        pass


def test_apply_patch_preserva_cp1252_y_la_linea_no_tocada(tmp_path, tools, patch_aislado):
    """Mismo defecto que tenia `edit`: leia utf-8+replace y escribia utf-8, asi
    que la linea que el parche NO tocaba quedaba con U+FFFD."""
    usar, _ = tools
    f = _cp1252(tmp_path)

    out = usar("apply_patch", patch=_patch_update(f, "resumen: cañón del sur",
                                                  "RESUMEN: canon del sur"))

    assert '"ok": true' in out
    despues = f.read_bytes()
    assert despues.decode("cp1252").splitlines() == ["RESUMEN: canon del sur",
                                                     "no tocar: ñandú"]
    assert b"no tocar: \xf1and\xfa" in despues, "la linea no tocada, byte por byte"
    assert b"\xef\xbf\xbd" not in despues


def test_apply_patch_falla_sin_escribir_si_el_texto_no_entra_en_el_encoding(tmp_path, tools, patch_aislado):
    usar, intentar = tools
    f = _cp1252(tmp_path)
    antes = f.read_bytes()

    est, out = intentar("apply_patch", patch=_patch_update(f, "no tocar: ñandú",
                                                           "flecha →"))

    assert est == "err"
    assert "no puede representar" in out
    assert f.read_bytes() == antes, "el parche es atomico: no se escribe nada"


def test_apply_patch_no_traduce_lf_a_crlf(tmp_path, tools, patch_aislado):
    usar, _ = tools
    f = tmp_path / "lf.txt"
    f.write_bytes(b"uno\ndos\ntres\n")

    usar("apply_patch", patch=_patch_update(f, "dos", "DOS"))

    assert f.read_bytes() == b"uno\nDOS\ntres\n"


def test_apply_patch_dry_run_informa_el_encoding_y_no_escribe(tmp_path, tools, patch_aislado):
    import json as _json

    usar, _ = tools
    f = _cp1252(tmp_path)
    antes = f.read_bytes()

    j = _json.loads(usar("apply_patch", patch=_patch_update(f, "no tocar: ñandú",
                                                            "otra cosa: ñ"),
                         dry_run=True))

    assert j["dry_run"] is True
    assert j["encodings"][str(f)] == "cp1252"
    assert f.read_bytes() == antes


# -------------------------------------------------------------- API fs/read


def test_fs_read_devuelve_acentos_de_cp1252(tmp_path):
    from src.api.server import app

    f = _cp1252(tmp_path)
    r = TestClient(app).get("/api/fs/read", params={"path": str(f)})

    assert r.status_code == 200, r.text[:200]
    assert r.json()["content"] == "resumen: cañón del sur\r\nno tocar: ñandú\r\n"


def test_fs_write_preserva_el_encoding_del_archivo(tmp_path):
    from src.api.server import app

    f = _cp1252(tmp_path)
    r = TestClient(app).put("/api/fs/write", json={"path": str(f), "content": "otra: ñ\n"})

    assert r.status_code == 200, r.text[:200]
    assert r.json()["encoding"] == "cp1252"
    assert r.json()["converted"] is False
    assert f.read_bytes() == "otra: ñ\n".encode("cp1252")
