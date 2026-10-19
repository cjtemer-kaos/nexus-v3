"""Tests del SchemaSanitizer: dialectos por proveedor y tool definitions."""
from src.core.schema_sanitizer import SchemaSanitizer


def test_gemini_union_to_nullable():
    out = SchemaSanitizer.sanitize({"type": ["string", "null"]}, "gemini")
    assert out["type"] == "string"
    assert out["nullable"] is True


def test_gemini_only_null_becomes_nullable_string():
    out = SchemaSanitizer.sanitize({"type": ["null"]}, "gemini")
    assert out["type"] == "string"
    assert out["nullable"] is True


def test_gemini_recurses_properties_items_and_combinators():
    schema = {
        "type": "object",
        "properties": {
            "tags": {"type": "array", "items": {"type": ["integer", "null"]}},
            "meta": {"anyOf": [{"type": ["string", "null"]}]},
        },
    }
    out = SchemaSanitizer.sanitize(schema, "gemini")
    assert out["properties"]["tags"]["items"] == {"type": "integer", "nullable": True}
    assert out["properties"]["meta"]["anyOf"][0] == {"type": "string", "nullable": True}


def test_gemini_rejects_ambiguous_union():
    import pytest

    with pytest.raises(ValueError):
        SchemaSanitizer.sanitize({"type": ["string", "integer"]}, "gemini")


def test_openai_collapses_union_without_nullable():
    out = SchemaSanitizer.sanitize({"type": ["string", "integer"]}, "openai")
    assert out["type"] == "string"
    assert "nullable" not in out


def test_anthropic_object_gets_properties():
    out = SchemaSanitizer.sanitize({"type": "object"}, "anthropic")
    assert out["properties"] == {}


def test_ollama_delegates_to_openai():
    out = SchemaSanitizer.sanitize({"type": ["number", "null"]}, "ollama")
    assert out["type"] == "number"
    assert out["nullable"] is True


def test_non_dict_schema_passthrough():
    assert SchemaSanitizer.sanitize("string", "gemini") == "string"
    assert SchemaSanitizer.sanitize(["string", "null"], "gemini") == ["string", "null"]


def test_sanitize_tool_definitions_covers_parameters_and_input_schema():
    tools = [
        {"name": "a", "parameters": {"type": ["string", "null"]}},
        {"name": "b", "input_schema": {"type": "object"}},
        {"name": "c"},
    ]
    out = SchemaSanitizer.sanitize_tool_definitions(tools, "gemini")
    assert out[0]["parameters"] == {"type": "string", "nullable": True}
    assert out[1]["input_schema"] == {"type": "object"}
    assert out[2] == {"name": "c"}
    assert tools[0]["parameters"]["type"] == ["string", "null"]
