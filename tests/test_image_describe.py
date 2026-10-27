"""image_describe: leer una imagen con el modelo de vision local.

opencode devuelve la imagen al modelo; nexus no puede (el dispatch de una tool
devuelve texto). La via equivalente es mirarla con el modelo de vision que ya
esta instalado (qwen3-vl/qwen2.5vl via ollama) y devolver la lectura en texto.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api import server as S  # noqa: E402


def _png(path: Path) -> Path:
    # 1x1 transparente valido (el mismo que usan los uploads de prueba)
    path.write_bytes(bytes.fromhex(
        "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
        "890000000a49444154789c630001000000050001"
        "0d0a2db40000000049454e44ae426082"))
    return path


def test_data_uri_de_png_es_base64_redondo(tmp_path):
    import base64

    p = _png(tmp_path / "captura.png")
    mime, uri = S._image_data_uri(p)

    assert mime == "image/png"
    assert uri.startswith("data:image/png;base64,")
    assert base64.b64decode(uri.split(",", 1)[1]) == p.read_bytes()


def test_extension_no_soportada_dice_cuales_sirven(tmp_path):
    p = tmp_path / "notas.md"
    p.write_text("hola", encoding="utf-8")

    with pytest.raises(ValueError) as e:
        S._image_data_uri(p)

    msg = str(e.value)
    assert "no es una imagen" in msg
    assert "png" in msg and "webp" in msg


def test_imagen_gigante_se_rechaza(tmp_path):
    p = tmp_path / "grande.png"
    p.write_bytes(b"\x89PNG" + b"0" * (S.MAX_IMAGE_BYTES + 10))

    with pytest.raises(ValueError) as e:
        S._image_data_uri(p)

    assert "demasiado grande" in str(e.value)


class _RouterFalso:
    def __init__(self, respuesta="una caja roja con el texto HOLA", vacio=False):
        self.respuesta = respuesta
        self.vacio = vacio
        self.visto: dict = {}

    async def chat(self, messages, tools=None, hint=None, force_tools=False,
                   model=None, require_vision=False):
        self.visto = {"messages": messages, "hint": hint,
                      "require_vision": require_vision}
        if self.vacio:
            return SimpleNamespace(content="", model="fake-vl")
        return SimpleNamespace(content=self.respuesta, model="fake-vl")


@pytest.fixture()
def con_router():
    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)

    def build(router):
        reg = S._build_registry(scope, f"test-img-{uuid.uuid4().hex[:8]}", cfg, router=router)

        def usar(args):
            return asyncio.run(reg.dispatch("image_describe", args, scope=scope, hooks=None))

        return usar

    return build


def test_tool_lee_la_imagen_con_el_modelo_de_vision(tmp_path, con_router):
    p = _png(tmp_path / "cap.png")
    router = _RouterFalso()
    usar = con_router(router)

    out = usar({"path": str(p), "prompt": "que dice?"})

    assert "una caja roja con el texto HOLA" in out
    assert "cap.png" in out and "fake-vl" in out
    assert router.visto["require_vision"] is True
    assert router.visto["hint"] == "vision"
    assert router.visto["messages"][-1].content == "que dice?"
    assert router.visto["messages"][-1].images[0].startswith("data:image/png;base64,")


def test_sin_prompt_igual_funciona(tmp_path, con_router):
    p = _png(tmp_path / "cap.png")
    router = _RouterFalso()
    usar = con_router(router)

    out = usar({"path": str(p)})

    assert "una caja roja" in out
    assert "Describe la imagen" in router.visto["messages"][-1].content


def test_texto_vacio_del_modelo_es_error(tmp_path, con_router):
    p = _png(tmp_path / "cap.png")
    usar = con_router(_RouterFalso(vacio=True))

    with pytest.raises(RuntimeError) as e:
        usar({"path": str(p)})

    assert "no devolvio texto" in str(e.value)


def test_archivo_inexistente(tmp_path, con_router):
    usar = con_router(_RouterFalso())

    with pytest.raises(FileNotFoundError):
        usar({"path": str(tmp_path / "no-existe.png")})


def test_esta_registrada_y_es_de_lectura():
    assert "image_describe" in S._SUB_TOOLSETS["debugger"]
    from src.core.scope import IDEMPOTENT_TOOLS

    assert "image_describe" in IDEMPOTENT_TOOLS
