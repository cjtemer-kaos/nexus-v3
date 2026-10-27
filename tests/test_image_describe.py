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


@pytest.fixture()
def leer_archivo(tmp_path):
    """Registry real con una raiz permitida temporal (sin tocar data/ ni workspace/)."""
    import copy

    cfg = copy.deepcopy(S._load_config_safe())
    cfg.setdefault("scope", {})
    cfg["scope"]["allowed_roots"] = [str(tmp_path)]
    cfg["scope"]["permission"] = "full"
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, f"test-read-{uuid.uuid4().hex[:8]}", cfg)

    def usar(path):
        return asyncio.run(reg.dispatch("read_file", {"path": str(path)}, scope=scope, hooks=None))

    return usar


def test_read_file_de_una_imagen_manda_a_image_describe(tmp_path, leer_archivo):
    """Antes decia \"no es texto (binario)\" y el modelo no sabia que tenia image_describe."""
    p = _png(tmp_path / "captura.png")

    with pytest.raises(ValueError) as e:
        leer_archivo(p)

    msg = str(e.value)
    assert "es una imagen" in msg and "png" in msg
    assert "image_describe" in msg
    assert "no es texto" not in msg


def test_read_file_de_un_binario_que_no_es_imagen_sigue_generico(tmp_path, leer_archivo):
    p = tmp_path / "programa.bin"
    p.write_bytes(b"\x00\x01\x02\x03" * 512)

    with pytest.raises(ValueError) as e:
        leer_archivo(p)

    msg = str(e.value)
    assert "no es texto (binario" in msg
    assert "image_describe" not in msg


def test_read_file_de_texto_sigue_leyendo(tmp_path, leer_archivo):
    p = tmp_path / "notas.txt"
    p.write_text("hola\nmundo\n", encoding="utf-8")

    out = leer_archivo(p)

    assert "hola" in out and "mundo" in out
