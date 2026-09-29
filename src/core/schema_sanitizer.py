"""
Schema Sanitizer - Translates JSON Schema 2020-12 to provider-specific dialects

Providers (Gemini, OpenAI, Ollama, Anthropic) each accept slightly different
subsets of JSON Schema. This module normalizes tool/parameter schemas before
they are sent to inference, preventing rejection errors.
"""

from typing import Any, Dict, List


class SchemaSanitizer:
    """Normalizes JSON Schema for specific LLM providers."""

    @staticmethod
    def sanitize(schema: Any, provider: str = "ollama") -> Any:
        """
        Sanitize a JSON Schema for the target provider.

        Args:
            schema: JSON Schema dict (may contain JSON Schema 2020-12 features)
            provider: Target provider ("ollama", "gemini", "openai", "anthropic")

        Returns:
            Sanitized schema compatible with the provider
        """
        if provider == "gemini":
            return SchemaSanitizer._for_gemini(schema)
        elif provider == "openai":
            return SchemaSanitizer._for_openai(schema)
        elif provider == "anthropic":
            return SchemaSanitizer._for_anthropic(schema)
        else:
            return SchemaSanitizer._for_ollama(schema)

    @staticmethod
    def _for_gemini(schema: Any) -> Any:
        """
        Convert JSON Schema 2020-12 to OpenAPI 3.0 dialect that Gemini accepts.

        Gemini rejects union "type": ["string", "null"]. Translates to
        single type + "nullable": true.
        """
        if isinstance(schema, list):
            return [SchemaSanitizer._for_gemini(s) for s in schema]
        if not isinstance(schema, dict):
            return schema

        out = dict(schema)
        t = out.get("type")
        if isinstance(t, list):
            non_null = [x for x in t if x != "null"]
            has_null = "null" in t
            if len(non_null) == 1:
                out["type"] = non_null[0]
                if has_null:
                    out["nullable"] = True
            elif not non_null and has_null:
                out["type"] = "string"
                out["nullable"] = True
            else:
                raise ValueError(
                    f"Unsupported Gemini schema union: {t!r}. "
                    "Rewrite as anyOf or pick a single type."
                )

        if "properties" in out and isinstance(out["properties"], dict):
            out["properties"] = {
                k: SchemaSanitizer._for_gemini(v) for k, v in out["properties"].items()
            }
        if "items" in out:
            out["items"] = SchemaSanitizer._for_gemini(out["items"])
        if "additionalProperties" in out and isinstance(out["additionalProperties"], dict):
            out["additionalProperties"] = SchemaSanitizer._for_gemini(out["additionalProperties"])
        for combinator in ("anyOf", "oneOf", "allOf"):
            if combinator in out:
                out[combinator] = SchemaSanitizer._for_gemini(out[combinator])

        return out

    @staticmethod
    def _for_openai(schema: Any) -> Any:
        """
        Sanitize schema for OpenAI function calling.

        OpenAI is relatively permissive but rejects:
        - "type" as a list
        - "$ref" without a "$defs" context
        - "const" in some older models
        """
        if isinstance(schema, list):
            return [SchemaSanitizer._for_openai(s) for s in schema]
        if not isinstance(schema, dict):
            return schema

        out = dict(schema)
        t = out.get("type")
        if isinstance(t, list):
            non_null = [x for x in t if x != "null"]
            has_null = "null" in t
            if len(non_null) <= 1:
                out["type"] = non_null[0] if non_null else "string"
                if has_null:
                    out["nullable"] = True
            else:
                out["type"] = non_null[0]

        if "properties" in out and isinstance(out["properties"], dict):
            out["properties"] = {
                k: SchemaSanitizer._for_openai(v) for k, v in out["properties"].items()
            }
        if "items" in out:
            out["items"] = SchemaSanitizer._for_openai(out["items"])

        return out

    @staticmethod
    def _for_anthropic(schema: Any) -> Any:
        """
        Sanitize schema for Anthropic tool use.

        Anthropic requires strict JSON Schema with:
        - "type" must be a string, not a list
        - "properties" must be present if type is "object"
        - "description" is strongly recommended
        """
        if isinstance(schema, list):
            return [SchemaSanitizer._for_anthropic(s) for s in schema]
        if not isinstance(schema, dict):
            return schema

        out = dict(schema)
        t = out.get("type")
        if isinstance(t, list):
            non_null = [x for x in t if x != "null"]
            has_null = "null" in t
            if len(non_null) <= 1:
                out["type"] = non_null[0] if non_null else "string"
            else:
                out["type"] = non_null[0]

        if out.get("type") == "object" and "properties" not in out:
            out["properties"] = {}

        if "properties" in out and isinstance(out["properties"], dict):
            out["properties"] = {
                k: SchemaSanitizer._for_anthropic(v) for k, v in out["properties"].items()
            }
        if "items" in out:
            out["items"] = SchemaSanitizer._for_anthropic(out["items"])

        return out

    @staticmethod
    def _for_ollama(schema: Any) -> Any:
        """
        Sanitize schema for Ollama (OpenAI-compatible API).

        Ollama follows OpenAI's schema format with minor variations.
        """
        return SchemaSanitizer._for_openai(schema)

    @staticmethod
    def sanitize_tool_definitions(
        tools: List[Dict], provider: str = "ollama"
    ) -> List[Dict]:
        """
        Sanitize a list of tool definitions for the target provider.

        Args:
            tools: List of tool definitions with "name", "description", "parameters"
            provider: Target provider

        Returns:
            Sanitized tool definitions
        """
        sanitized = []
        for tool in tools:
            tool_copy = dict(tool)
            if "parameters" in tool_copy:
                tool_copy["parameters"] = SchemaSanitizer.sanitize(
                    tool_copy["parameters"], provider
                )
            if "input_schema" in tool_copy:
                tool_copy["input_schema"] = SchemaSanitizer.sanitize(
                    tool_copy["input_schema"], provider
                )
            sanitized.append(tool_copy)
        return sanitized
