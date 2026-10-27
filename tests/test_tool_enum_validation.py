"""`enum` del JSON Schema: antes se ignoraba y el valor llegaba crudo al código.

Medido:

    image_generate(prompt="x", mode="txt2img")   # nombre de Stable Diffusion
      -> lanzaba scripts/comfy_gen.py --mode txt2img  (proceso externo)
    sub_agent(task="...", mode="continuable ")
      -> caía al fallback silencioso y el hijo quedaba one-shot

Ahora el borde falla con el motivo y los valores válidos, y normaliza
mayúsculas/espacios al valor canónico.
"""
from __future__ import annotations

import uuid

import pytest

from src.core.tool_registry import validate_args


def test_un_enum_invalido_no_llega_al_codigo():
    esq = {"type": "object", "properties": {
        "prompt": {"type": "string"},
        "mode": {"type": "string", "enum": ["t2i", "edit"]}},
        "required": ["prompt"], "additionalProperties": False}

    with pytest.raises(ValueError) as e:
        validate_args("image_generate", {"prompt": "un gato", "mode": "txt2img"}, esq)

    msg = str(e.value)
    assert "image_generate.mode" in msg
    assert "txt2img" in msg
    assert "'t2i', 'edit'" in msg


def test_mayusculas_y_espacios_se_normalizan_al_canonico():
    esq = {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["one-shot", "continuable"]}}}

    assert validate_args("sub_agent", {"mode": " Continuable "}, esq)["mode"] == "continuable"
    assert validate_args("sub_agent", {"mode": "ONE-SHOT"}, esq)["mode"] == "one-shot"


def test_el_enum_anidado_en_una_lista_tambien_se_valida():
    esq = {"type": "object", "properties": {"todos": {
        "type": "array", "items": {"type": "object", "properties": {
            "content": {"type": "string"},
            "status": {"type": "string",
                       "enum": ["pending", "in_progress", "completed"]}}}}}}

    with pytest.raises(ValueError) as e:
        validate_args("todo_write", {"todos": [{"content": "a", "status": "done"}]}, esq)
    msg = str(e.value)
    assert "todo_write.todos[].status" in msg and "'pending'" in msg

    ok = validate_args("todo_write", {"todos": [{"content": "a", "status": "in_progress"}]}, esq)
    assert ok["todos"][0]["status"] == "in_progress"


def test_un_enum_valido_pasa_igual():
    esq = {"type": "object", "properties": {
        "mode": {"type": "string", "enum": ["t2i", "edit"]}}}

    assert validate_args("image_generate", {"mode": "edit"}, esq)["mode"] == "edit"


def test_el_schema_real_de_image_generate_declara_el_enum():
    from src.api import server as S

    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, f"test-enum-{uuid.uuid4().hex[:8]}", cfg)
    esq = reg.get("image_generate").parameters

    assert esq["properties"]["mode"]["enum"] == ["t2i", "edit"]
    with pytest.raises(ValueError):
        validate_args("image_generate", {"prompt": "x", "mode": "img2img"}, esq)
