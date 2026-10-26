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

import asyncio
import functools
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

    # `from __future__ import annotations` deja las anotaciones como STRINGS
    # ("int", "str"...). Resolverlas con get_type_hints; si falla, mapear el
    # nombre. Sin esto, todos los params int/float/bool quedaban como "string".
    try:
        from typing import get_type_hints
        hints = get_type_hints(fn)
    except Exception:
        hints = {}
    type_map = {str: "string", int: "integer", float: "number", bool: "boolean", list: "array", dict: "object"}
    name_map = {"str": "string", "int": "integer", "float": "number", "bool": "boolean",
                "list": "array", "dict": "object", "Any": "string"}
    props: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in sig.parameters.items():
        if pname in ("self", "cls"):
            continue
        ann = hints.get(pname, param.annotation)
        if isinstance(ann, str):
            jtype = name_map.get(ann, "string")
        elif ann != inspect.Parameter.empty:
            jtype = type_map.get(ann, "string")
        else:
            jtype = "string"
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


def _coerce(val, want):
    """Coerciona escalares que los modelos chicos mandan como string (patrón continue)."""
    try:
        if want == "integer" and isinstance(val, str):
            return int(float(val.strip()))
        if want == "number" and isinstance(val, str):
            return float(val.strip())
        if want == "boolean" and isinstance(val, str):
            s = val.strip().lower()
            if s in ("true", "1", "yes", "si", "sí"):
                return True
            if s in ("false", "0", "no"):
                return False
        if want == "array" and isinstance(val, str):
            return [s.strip() for s in val.split(",") if s.strip()]
        if want == "object" and isinstance(val, str):
            import json as _j
            return _j.loads(val)
    except Exception:
        pass
    return val


def validate_args(name: str, args: dict, schema: dict) -> dict:
    """Valida args contra JSON Schema (ligero). Coerciona tipos y descarta claves extra
    (los modelos chicos mandan "3"/"true"/campos inventados). Lanza ValueError accionable."""
    if not isinstance(schema, dict) or not schema:
        return args
    props = schema.get("properties", {}) or {}
    required = schema.get("required", []) or []
    # Coerción de tipos por propiedad
    out: dict = {}
    for k, v in (args or {}).items():
        spec = props.get(k)
        want = spec.get("type") if isinstance(spec, dict) else None
        out[k] = _coerce(v, want) if want else v
    # Descartar claves extra (evita "argumentos no permitidos" que rompe el run)
    if not schema.get("additionalProperties", True):
        for k in [k for k in out if k not in props]:
            out.pop(k, None)
    for key in required:
        if key not in out or out[key] is None:
            want = (props.get(key) or {}).get("type", "?")
            raise ValueError(
                f"{name}: falta el argumento requerido '{key}' (tipo {want}). "
                f"Argumentos válidos: {sorted(props)}")
    for key, val in list(out.items()):
        spec = props.get(key)
        if not isinstance(spec, dict) or val is None:
            continue
        want = spec.get("type")
        expected = _JSON_TYPES.get(want) if isinstance(want, str) else None
        if expected is not None and not isinstance(val, expected):
            if want in ("number",) and isinstance(val, (int, float)) and not isinstance(val, bool):
                continue
            raise ValueError(f"{name}: '{key}' debe ser {want} (recibido {type(val).__name__})")
    return out


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
        # Guardrail de secuencia (ToolShield). Lo arma el servidor con config.
        self.sequence_guard: Any = None

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

    def subset(self, names) -> "ToolRegistry":
        """Registro acotado a `names` (comparte los ToolEntry, no los copia).

        Lo usan los sub-agentes para correr con un set mínimo de tools: menos
        superficie de error y menos tokens de esquema en el prompt.
        """
        wanted = [n for n in (names or []) if isinstance(n, str)]
        sub = ToolRegistry()
        with self._lock:
            for n in wanted:
                entry = self._tools.get(n)
                if entry is not None:
                    sub._tools[n] = entry
        sub.current_images = list(getattr(self, "current_images", []) or [])
        return sub

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
    async def dispatch(self, name: str, args: dict[str, Any] | None = None, scope=None, hooks=None,
                       call_log=None, approval=None, session_id: str = "") -> Any:
        """Guarded dispatch: validate -> scope -> approval (HITL) -> hooks pre -> exec -> hooks post -> metrics -> log.

        `approval` es un `ApprovalContext`. Sin contexto el gate se omite (compat);
        los caminos no interactivos (MCP server, subagentes) pasan un contexto con
        `interactive=False`/`policy="never"` para fallar cerrado.
        """
        if call_log is None:
            call_log = getattr(self, "call_log", None)
        args = args or {}
        entry = self.get(name)
        if not entry or not entry.handler:
            import difflib
            names = sorted(self._tools)
            close = difflib.get_close_matches(name, names, n=3, cutoff=0.5)
            hint = f" ¿Quisiste decir: {', '.join(close)}?" if close else ""
            raise KeyError(
                f"herramienta no encontrada: {name!r}.{hint} "
                f"Usá una de: {', '.join(names[:40])}")

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

        # Approval gate (HITL): choke point unico para loop, subagentes y MCP.
        # Sin contexto se omite (compatibilidad); los caminos no interactivos
        # pasan un ApprovalContext con interactive=False para fallar cerrado.
        _approval_required = False
        _declared_read_only = False
        if approval is not None:
            try:
                from src.core.approval_policy import check_tool_approval, authorize
                _check = check_tool_approval(name, args, entry, approval)
                _approval_required = bool(getattr(_check, "required", False))
                # mcp_call ya resuelto como lectura por allowlist: el guardrail de
                # secuencia no debe volver a pedir aprobacion (rompia el uso sin UI).
                _declared_read_only = (name == "mcp_call" and not _approval_required
                                       and str(getattr(_check, "action", "")).startswith("mcp:"))
                _out = await authorize(_check, approval, tool=name)
                if not _out.allowed:
                    raise PermissionError(
                        f"aprobacion denegada para {name}: {_out.reason or _out.decision}")
            except PermissionError as e:
                if call_log is not None:
                    try:
                        call_log({"tool": name, "args": args, "outcome": "approval_denied",
                                  "success": False, "error": str(e)[:300]})
                    except Exception:
                        pass
                raise
            except Exception as e:
                # Fail-closed: si el gate falla, NO se ejecuta la tool.
                logger.warning(f"approval gate error en {name}: {e}")
                if call_log is not None:
                    try:
                        call_log({"tool": name, "args": args, "outcome": "approval_error",
                                  "success": False, "error": str(e)[:300]})
                    except Exception:
                        pass
                raise PermissionError(f"aprobacion no disponible para {name}: {e}")

        # Guardrail de secuencia (ToolShield). El actor no decide su riesgo:
        # HIGH = deny; MEDIUM = aprobacion (o fail-closed sin canal). Si el gate
        # normal ya pidio aprobacion, no se duplica el pedido.
        _sg = getattr(self, "sequence_guard", None)
        # Sin ApprovalContext el gate HITL se omite por compatibilidad; el
        # guardrail de secuencia sigue la misma regla (los caminos no
        # interactivos SI pasan un contexto interactive=False).
        if (_sg is not None and approval is not None and not _approval_required
                and not _declared_read_only):
            try:
                _verdict = await _sg.evaluate(name, args)
            except Exception as e:
                logger.debug("sequence guard error en %s: %s", name, e)
                _verdict = None
            _risk = str(getattr(_verdict, "risk", "LOW") or "LOW").upper()
            _reason = str(getattr(_verdict, "reason", "") or "")
            if _risk == "HIGH":
                if call_log is not None:
                    try:
                        call_log({"tool": name, "args": args,
                                  "outcome": "sequence_denied", "success": False,
                                  "error": _reason[:300]})
                    except Exception:
                        pass
                raise PermissionError(
                    f"secuencia bloqueada (HIGH) para {name}: {_reason or 'riesgo alto'}")
            if _risk == "MEDIUM":
                if approval is None:
                    raise PermissionError(
                        f"secuencia MEDIUM sin canal de aprobacion para {name}: {_reason}")
                try:
                    from src.core.approval_policy import ApprovalCheck, authorize
                    from src.core.approvals import fingerprint
                    _seq_check = ApprovalCheck(
                        required=True,
                        action="sequence:medium",
                        reason=f"secuencia MEDIUM: {_reason}",
                        key_hash=fingerprint(name, "sequence:medium", args),
                        preview=json.dumps(args, ensure_ascii=False,
                                           default=str)[:2000],
                    )
                    _seq_out = await authorize(_seq_check, approval, tool=name)
                    if not _seq_out.allowed:
                        raise PermissionError(
                            f"secuencia MEDIUM no aprobada para {name}: "
                            f"{_seq_out.reason or _seq_out.decision}")
                except PermissionError:
                    if call_log is not None:
                        try:
                            call_log({"tool": name, "args": args,
                                      "outcome": "sequence_denied",
                                      "success": False, "error": _reason[:300]})
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
                # Los handlers sync con I/O corrian EN LINEA en el event loop:
                # job_wait duerme hasta 300 s, web_fetch hace HTTP y
                # read_file/write_file tocan disco, asi que durante esa llamada
                # ningun SSE, /api/status ni keepalive avanzaba. to_thread los
                # saca del loop sin cambiar su contrato ni su firma.
                result = await asyncio.to_thread(functools.partial(entry.handler, **args))  # type: ignore
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
            try:
                if _sg is not None:
                    _sg.record(name, args, success,
                               outcome="ok" if success else "error",
                               error=error_msg)
            except Exception:
                pass
            # Hooks post
            if hooks is not None and hasattr(hooks, "emit"):
                try:
                    await hooks.emit("post_tool_use", {"tool": name, "result": result, "success": success, "latency_ms": dt_ms})
                except Exception as e:
                    logger.debug(f"hooks post_tool_use error: {e}")

