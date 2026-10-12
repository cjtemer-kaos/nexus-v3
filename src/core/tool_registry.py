"""Tool Registry — catalogo unificado (local + MCP + skill + gema).

Origen: SuperNEXUS DirectorToolRegistry + Hermes tool_executor/toolsets + Cordis skill/mcp + ECC layers.
Ver BLUEPRINT.md §7.

Features:
- Unified catalog: local tools (callables) + MCP tools (via bridge) + skills (prompt+bundle) + gema tools
- JSON Schema per tool (via explicit dict or inferred from function signature)
- Metrics: call_count, success_count, avg_latency_ms, last_error
- Thread-safe via RLock
- OpenAI-compatible to_openai_tools() for LLM tool-calling
- Skill/gema discovery via optional loader callbacks
- Guarded dispatch: scope.check → hooks pre/post → exec → metrics
"""
from __future__ import annotations

import inspect
import json
import time
import threading
import logging
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Optional

logger = logging.getLogger("nexus-tool-registry")


# ---------------------------------------------------------------------------
# ToolEntry
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ToolEntry:
    name: str
    description: str
    category: str  # local|mcp|skill|gema|registered
    parameters: dict = field(default_factory=dict)  # JSON Schema
    handler: Callable | None = None
    source: str = ""  # module origin or skill name
    call_count: int = 0
    success_count: int = 0
    avg_latency_ms: float = 0.0
    last_error: str = ""

    def to_openai(self) -> dict:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters or {"type": "object", "properties": {}, "additionalProperties": True},
            },
        }

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "parameters": self.parameters,
            "source": self.source,
            "call_count": self.call_count,
            "success_count": self.success_count,
            "avg_latency_ms": round(self.avg_latency_ms, 2),
            "last_error": self.last_error,
            "has_handler": self.handler is not None,
        }


def _infer_schema(fn: Callable) -> dict:
    """Infiere JSON Schema desde la firma de la función (ligero, sin Pydantic).

    - Parámetros requeridos = parámetros sin valor por defecto
    - Tipos mapeados a tipos JSON Schema (str->string, int->integer, etc.)
    - Primera línea del docstring usada como respaldo de descripción
    """
    try:
        sig = inspect.signature(fn)
    except Exception:
        return {"type": "object", "properties": {}, "additionalProperties": True}

    type_map = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        ann = param.annotation
        jtype = type_map.get(ann, "string") if ann != inspect.Parameter.empty else "string"
        prop: dict[str, Any] = {"type": jtype, "description": f"parameter {pname}"}
        props[pname] = prop
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    schema: dict[str, Any] = {"type": "object", "properties": props, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float), "boolean": bool,
    "array": list, "object": dict,
}


def validate_args(name: str, args: dict, schema: dict) -> dict:
    """Valida args contra JSON Schema (ligero). Devuelve args normalizados; lanza ValueError."""
    if not isinstance(schema, dict) or not schema:
        return args
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    for key in required:
        if key not in args or args[key] is None:
            raise ValueError(f"{name}: falta argumento requerido '{key}'")
    if not schema.get("additionalProperties", True):
        extra = [k for k in args if k not in props]
        if extra:
            raise ValueError(f"{name}: argumentos no permitidos {extra}")
    for key, val in list(args.items()):
        spec = props.get(key)
        if not isinstance(spec, dict) or val is None:
            continue
        want = spec.get("type")
        expected = _JSON_TYPES.get(want) if isinstance(want, str) else None
        if expected is not None and not isinstance(val, expected):
            if want == "integer" and isinstance(val, bool):
                raise ValueError(f"{name}: '{key}' debe ser integer")
            if isinstance(expected, tuple) or want in ("number",):
                if isinstance(val, (int, float)) and not isinstance(val, bool):
                    continue
            if want in ("string", "integer", "number", "boolean", "array", "object"):
                raise ValueError(f"{name}: '{key}' debe ser {want}, recibido {type(val).__name__}")
    return args


# ---------------------------------------------------------------------------
# ToolRegistry
# ---------------------------------------------------------------------------

class ToolRegistry:
    """Unified tool catalog. Thread-safe."""

    def __init__(self):
        self._tools: dict[str, ToolEntry] = {}
        self._lock = threading.RLock()
        self._skill_loaders: list[Callable] = []
        self._mcp_loaders: list[Callable] = []
        # Imagenes (data-URI) del turno actual: la delegacion (gema_run/agent_run)
        # las hereda para que el sub-agente vea la imagen.
        self.current_images: list = []

    # ---- registration ----
    def register(self, entry: ToolEntry) -> None:
        with self._lock:
            if entry.name in self._tools:
                logger.warning(f"re-registro herramienta: {entry.name} (was {self._tools[entry.name].category})")
            self._tools[entry.name] = entry

    def register_function(
        self,
        fn: Callable,
        name: str | None = None,
        description: str = "",
        category: str = "local",
        parameters: dict | None = None,
        source: str = "",
    ) -> ToolEntry:
        n = name or fn.__name__
        desc = description or (inspect.getdoc(fn) or "").split("\n")[0].strip()
        params = parameters if parameters is not None else _infer_schema(fn)
        e = ToolEntry(
            name=n,
            description=desc or f"herramienta {n}",
            category=category,
            parameters=params,
            handler=fn,
            source=source or getattr(fn, "__module__", ""),
        )
        self.register(e)
        return e

    def tool(self, description: str = "", category: str = "local", parameters: dict | None = None, name: str | None = None):
        """Decorator: @registry.tool(description=..., category='local')"""
        def deco(fn: Callable):
            self.register_function(fn, name=name, description=description, category=category, parameters=parameters)
            return fn
        return deco

    def register_skill_tool(self, name: str, description: str, parameters: dict, handler: Callable, skill_name: str = "") -> ToolEntry:
        return self.register_function(handler, name=name, description=description, category="skill", parameters=parameters, source=skill_name)

    def register_mcp_tool(self, name: str, description: str, parameters: dict, handler: Callable, server: str = "") -> ToolEntry:
        return self.register_function(handler, name=name, description=description, category="mcp", parameters=parameters, source=server)

    def register_gema_tool(self, name: str, description: str, parameters: dict, handler: Callable, gema: str = "") -> ToolEntry:
        return self.register_function(handler, name=name, description=description, category="gema", parameters=parameters, source=gema)

    # ---- discovery ----
    def list(self, category: str | None = None) -> list[ToolEntry]:
        with self._lock:
            if category:
                return [t for t in self._tools.values() if t.category == category]
            return list(self._tools.values())

    def categories(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for t in self._tools.values():
                counts[t.category] = counts.get(t.category, 0) + 1
            return counts

    def get(self, name: str) -> ToolEntry | None:
        with self._lock:
            return self._tools.get(name)

    def has(self, name: str) -> bool:
        with self._lock:
            return name in self._tools

    def remove(self, name: str) -> bool:
        with self._lock:
            return self._tools.pop(name, None) is not None

    def count(self) -> int:
        with self._lock:
            return len(self._tools)

    def to_openai_tools(self) -> list[dict]:
        with self._lock:
            return [t.to_openai() for t in self._tools.values()]

    # Alias per blueprint
    def to_openai(self) -> list[dict]:
        return self.to_openai_tools()

    # ---- bulk loading ----
    def load_skill_bundle(self, skill_name: str, tools: list[dict[str, Any]], handler_map: dict[str, Callable] | None = None) -> int:
        """Load a skill bundle: list of {name, description, parameters} + handler_map."""
        n = 0
        for t in tools:
            handler = (handler_map or {}).get(t["name"])
            self.register_skill_tool(t["name"], t.get("description", ""), t.get("parameters", {}), handler or (lambda **kw: f"[skill {skill_name} simulado]"), skill_name)
            n += 1
        return n

    def load_mcp_tools(self, server_name: str, tools: list[dict[str, Any]], handler_map: dict[str, Callable] | None = None) -> int:
        n = 0
        for t in tools:
            handler = (handler_map or {}).get(t["name"])
            self.register_mcp_tool(t["name"], t.get("description", ""), t.get("parameters", {}), handler or (lambda **kw: f"[mcp {server_name} simulado]"), server_name)
            n += 1
        return n

    def stats(self) -> dict:
        with self._lock:
            total = len(self._tools)
            by_cat: dict[str, int] = {}
            total_calls = 0
            total_success = 0
            for t in self._tools.values():
                by_cat[t.category] = by_cat.get(t.category, 0) + 1
                total_calls += t.call_count
                total_success += t.success_count
            return {
                "total_tools": total,
                "by_category": by_cat,
                "total_calls": total_calls,
                "total_success": total_success,
                "success_rate": round(total_success / total_calls, 3) if total_calls else 0.0,
            }

    # ---- dispatch ----
    async def dispatch(self, name: str, args: dict[str, Any] | None = None, scope=None, hooks=None, call_log=None) -> Any:
        """Guarded dispatch: validate → scope → hooks pre → exec → hooks post → metrics → log."""
        if call_log is None:
            call_log = getattr(self, "call_log", None)
        args = args or {}
        entry = self.get(name)
        if not entry or not entry.handler:
            raise KeyError(f"herramienta no encontrada o sin manejador: {name}")

        try:
            args = validate_args(name, args, entry.parameters)
        except ValueError:
            if call_log is not None:
                try:
                    call_log({"tool": name, "args": args, "outcome": "invalid_args", "success": False})
                except Exception:
                    pass
            raise

        # Scope guard
        if scope is not None:
            try:
                guard_fn = getattr(scope, "guard", None)
                if callable(guard_fn):
                    args = guard_fn(name, args)
                else:
                    check_fn = getattr(scope, "check_tool", None)
                    if callable(check_fn):
                        check_fn(name, args)
            except Exception as e:
                if call_log is not None:
                    try:
                        call_log({"tool": name, "args": args, "outcome": "scope_denied", "success": False, "error": str(e)})
                    except Exception:
                        pass
                raise

        # Hooks pre (load-bearing: STOP/COMPACT bloquea en código)
        if hooks is not None and hasattr(hooks, "emit"):
            try:
                _hr = await hooks.emit("pre_tool_use", {"tool": name, "args": args})
                _act = str(getattr(getattr(_hr, "action", None), "value", getattr(_hr, "action", "")))
                if _act in ("stop", "compact"):
                    raise PermissionError(f"hook bloqueó {name}: {getattr(_hr, 'message', '') or 'política de seguridad'}")
            except PermissionError:
                if call_log is not None:
                    try:
                        call_log({"tool": name, "args": args, "outcome": "hook_blocked", "success": False})
                    except Exception:
                        pass
                raise
            except Exception as e:
                logger.debug(f"hooks pre_tool_use error: {e}")

        t0 = time.monotonic()
        success = False
        result: Any = None
        error_msg = ""
        try:
            if inspect.iscoroutinefunction(entry.handler):
                result = await entry.handler(**args)  # type: ignore
            else:
                result = entry.handler(**args)  # type: ignore
            success = True
            return result
        except Exception as e:
            error_msg = str(e)
            if hooks is not None and hasattr(hooks, "emit"):
                try:
                    await hooks.emit("on_error", {"tool": name, "error": error_msg, "args": args})
                except Exception:
                    pass
            raise
        finally:
            dt_ms = (time.monotonic() - t0) * 1000
            # Update metrics atomically
            with self._lock:
                cur = self._tools.get(name)
                if cur is not None:
                    new_avg = (cur.avg_latency_ms * cur.call_count + dt_ms) / (cur.call_count + 1) if cur.call_count else dt_ms
                    updated = replace(
                        cur,
                        call_count=cur.call_count + 1,
                        success_count=cur.success_count + (1 if success else 0),
                        avg_latency_ms=new_avg,
                        last_error="" if success else error_msg[:500],
                    )
                    self._tools[name] = updated
            if call_log is not None:
                try:
                    call_log({"tool": name, "args": args, "outcome": "ok" if success else "error",
                              "success": success, "latency_ms": round(dt_ms, 2),
                              "error": "" if success else error_msg[:300]})
                except Exception:
                    pass
            # Hooks post
            if hooks is not None and hasattr(hooks, "emit"):
                try:
                    await hooks.emit("post_tool_use", {"tool": name, "result": result, "success": success, "latency_ms": dt_ms})
                except Exception as e:
                    logger.debug(f"hooks post_tool_use error: {e}")

