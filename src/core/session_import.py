"""Importación de sesiones ajenas en JSONL (claude / codex).

Trae historial de agentes CLI que guardan la conversación como JSONL y lo
convierte en `messages` de una sesión propia. Dos reglas:

- el cliente nunca manda una ruta: manda un **handle opaco** (base64 de
  `source|root|relativo`) que se valida contra las raíces conocidas, así el
  endpoint no se convierte en un lector arbitrario de disco;
- primero `preview` (turnos, conteos, qué se descarta) y después `apply`. Nada
  se escribe hasta que el usuario ve lo que va a entrar.

Se descartan los bloques de herramientas y el razonamiento: el formato propio
persiste `tool_calls` con su resultado y una cadena importada a medias no los
tiene, así que reenviarlos como texto sería inventar contexto.
"""
from __future__ import annotations

import base64
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

logger = logging.getLogger("nexus-session-import")

FUENTES = ("claude", "codex")
_MAX_TURNOS = 5000
_MAX_LINEAS_LISTA = 2000
_PREVIEW_CHARS = 600


def _raices() -> dict[str, list[Path]]:
    home = Path.home()
    return {
        "claude": [home / ".claude" / "projects"],
        "codex": [home / ".codex" / "sessions"],
    }


def _b64(texto: str) -> str:
    return base64.urlsafe_b64encode(texto.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(texto: str) -> str:
    relleno = "=" * (-len(texto) % 4)
    return base64.urlsafe_b64decode((texto + relleno).encode("ascii")).decode("utf-8")


def _handle(source: str, indice: int, ruta: Path, raiz: Path) -> str:
    rel = ruta.relative_to(raiz).as_posix()
    return _b64(f"{source}|{indice}|{rel}")


def resolver(handle: str) -> tuple[str, Path]:
    """Handle -> (fuente, ruta absoluta), validado contra las raíces."""
    try:
        source, indice, rel = _unb64(handle).split("|", 2)
        idx = int(indice)
    except Exception as e:
        raise ValueError("handle inválido") from e
    raices = _raices().get(source)
    if not raices or not (0 <= idx < len(raices)):
        raise ValueError("fuente desconocida en el handle")
    raiz = raices[idx].resolve()
    ruta = (raiz / rel).resolve()
    if raiz != ruta and raiz not in ruta.parents:
        raise ValueError("el handle sale de la raíz permitida")
    if ruta.suffix.lower() != ".jsonl" or not ruta.is_file():
        raise ValueError("el handle no apunta a un JSONL existente")
    return source, ruta


def _texto(valor) -> str:
    """Texto plano de un `content` (string o lista de bloques)."""
    if isinstance(valor, str):
        return valor
    if not isinstance(valor, list):
        return ""
    partes: list[str] = []
    for bloque in valor:
        if isinstance(bloque, str):
            partes.append(bloque)
        elif isinstance(bloque, dict):
            tipo = str(bloque.get("type") or "")
            if tipo in ("text", "input_text", "output_text"):
                partes.append(str(bloque.get("text") or ""))
    return "\n".join(p for p in partes if p).strip()


@dataclass
class Turno:
    role: str
    content: str
    timestamp: str = ""
    kind: str = ""


@dataclass
class Parseo:
    turnos: list[Turno] = field(default_factory=list)
    descartado: dict[str, int] = field(default_factory=dict)
    cwd: str = ""
    modelo: str = ""
    session_id: str = ""
    truncado: bool = False

    def descartar(self, motivo: str) -> None:
        self.descartado[motivo] = self.descartado.get(motivo, 0) + 1


def _parsear_claude(lineas: Iterable[str], limite: int = 0) -> Parseo:
    out = Parseo()
    for linea in lineas:
        linea = linea.strip()
        if not linea:
            continue
        try:
            obj = json.loads(linea)
        except Exception:
            out.descartar("linea_ilegible")
            continue
        tipo = str(obj.get("type") or "")
        if obj.get("cwd") and not out.cwd:
            out.cwd = str(obj["cwd"])
        if obj.get("sessionId") and not out.session_id:
            out.session_id = str(obj["sessionId"])
        if tipo not in ("user", "assistant"):
            continue
        if obj.get("isSidechain"):
            out.descartar("subagente")
            continue
        mensaje = obj.get("message") or {}
        modelo = str(mensaje.get("model") or "")
        if modelo and not modelo.startswith("<") and not out.modelo:
            out.modelo = modelo
        crudo = mensaje.get("content")
        if isinstance(crudo, list):
            for bloque in crudo:
                tipo_bloque = str((bloque or {}).get("type") or "") if isinstance(bloque, dict) else ""
                if tipo_bloque in ("tool_use", "tool_result", "thinking", "redacted_thinking"):
                    out.descartar(tipo_bloque)
        texto = _texto(crudo)
        if not texto:
            out.descartar("sin_texto")
            continue
        out.turnos.append(Turno(role=tipo, content=texto,
                                timestamp=str(obj.get("timestamp") or "")))
        if limite and len(out.turnos) >= limite:
            out.truncado = True
            break
    return out


def _parsear_codex(lineas: Iterable[str], limite: int = 0) -> Parseo:
    out = Parseo()
    por_evento: list[Turno] = []
    for linea in lineas:
        linea = linea.strip()
        if not linea:
            continue
        try:
            obj = json.loads(linea)
        except Exception:
            out.descartar("linea_ilegible")
            continue
        tipo = str(obj.get("type") or "")
        payload = obj.get("payload") or {}
        if not isinstance(payload, dict):
            continue
        if tipo == "session_meta":
            if payload.get("cwd") and not out.cwd:
                out.cwd = str(payload["cwd"])
            if payload.get("id") and not out.session_id:
                out.session_id = str(payload["id"])
            continue
        if tipo == "turn_context":
            if payload.get("cwd") and not out.cwd:
                out.cwd = str(payload["cwd"])
            continue
        if tipo == "event_msg":
            sub = str(payload.get("type") or "")
            if sub == "user_message":
                texto = _texto(payload.get("message"))
                if texto:
                    por_evento.append(Turno(role="user", content=texto,
                                            timestamp=str(obj.get("timestamp") or "")))
            elif sub == "agent_message":
                texto = _texto(payload.get("message"))
                if texto:
                    por_evento.append(Turno(role="assistant", content=texto,
                                            timestamp=str(obj.get("timestamp") or "")))
            continue
        if tipo != "response_item":
            continue
        sub = str(payload.get("type") or "")
        if sub in ("function_call", "function_call_output", "reasoning",
                   "local_shell_call", "custom_tool_call", "custom_tool_call_output"):
            out.descartar(sub)
            continue
        if sub != "message":
            continue
        rol = str(payload.get("role") or "")
        if rol == "assistant" and not out.modelo:
            out.modelo = "codex"
        if rol not in ("user", "assistant"):
            out.descartar(f"rol_{rol or 'vacio'}")
            continue
        texto = _texto(payload.get("content"))
        if not texto:
            out.descartar("sin_texto")
            continue
        if texto.lstrip().startswith(("<environment_context", "<user_instructions")):
            out.descartar("contexto_entorno")
            continue
        out.turnos.append(Turno(role=rol, content=texto,
                                timestamp=str(obj.get("timestamp") or "")))
        if limite and len(out.turnos) >= limite:
            out.truncado = True
            break
    if not out.turnos and por_evento:
        # Rollouts viejos sólo traen los mensajes como `event_msg`.
        out.turnos = por_evento[:limite] if limite else por_evento
        out.truncado = bool(limite and len(por_evento) > limite)
    return out


def _parsear(source: str, ruta: Path, limite: int = 0) -> Parseo:
    with ruta.open("r", encoding="utf-8", errors="replace") as fh:
        if source == "claude":
            return _parsear_claude(fh, limite)
        return _parsear_codex(fh, limite)


def _resumen_archivo(source: str, indice: int, ruta: Path, raiz: Path) -> dict:
    try:
        st = ruta.stat()
    except OSError as e:
        return {"handle": "", "source": source, "error": str(e)}
    turnos = 0
    titulo = ""
    cwd = ""
    lineas = 0
    try:
        with ruta.open("r", encoding="utf-8", errors="replace") as fh:
            for linea in fh:
                lineas += 1
                if lineas > _MAX_LINEAS_LISTA:
                    break
                if not titulo:
                    try:
                        obj = json.loads(linea)
                    except Exception:
                        continue
                    if obj.get("cwd") and not cwd:
                        cwd = str(obj["cwd"])
                    payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
                    if obj.get("type") == "session_meta" and payload.get("cwd"):
                        cwd = str(payload["cwd"])
                    rol = ""
                    if obj.get("type") in ("user", "assistant"):
                        rol = str(obj["type"])
                    elif obj.get("type") == "response_item" and payload.get("type") == "message":
                        rol = str(payload.get("role") or "")
                    if rol == "user":
                        texto = _texto((obj.get("message") or {}).get("content")) \
                            if obj.get("type") == "user" else _texto(payload.get("content"))
                        if texto:
                            titulo = " ".join(texto.split())[:90]
                if '"type":"user"' in linea or '"type":"assistant"' in linea \
                        or '"role":"user"' in linea or '"role":"assistant"' in linea:
                    turnos += 1
    except OSError as e:
        return {"handle": "", "source": source, "error": str(e)}
    return {
        "handle": _handle(source, indice, ruta, raiz),
        "source": source,
        "name": ruta.name,
        "cwd": cwd,
        "title": titulo or ruta.stem,
        "mtime": datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).isoformat(),
        "size": st.st_size,
        "turns_aprox": turnos,
        "lines_scanned": lineas,
    }


def listar(source: str = "", limit: int = 50) -> dict:
    """Sesiones externas disponibles, sin abrir más que las primeras líneas."""
    fuentes = [source] if source else list(FUENTES)
    salida: list[dict] = []
    raices_todas = _raices()
    for nombre in fuentes:
        if nombre not in FUENTES:
            raise ValueError(f"fuente desconocida: {nombre}")
        for indice, raiz in enumerate(raices_todas[nombre]):
            if not raiz.is_dir():
                continue
            for ruta in sorted(raiz.rglob("*.jsonl"),
                               key=lambda p: p.stat().st_mtime if p.exists() else 0,
                               reverse=True):
                if len(salida) >= max(1, limit):
                    break
                salida.append(_resumen_archivo(nombre, indice, ruta, raiz))
    salida.sort(key=lambda x: x.get("mtime") or "", reverse=True)
    return {"sessions": salida[:max(1, limit)],
            "roots": {k: [str(p) for p in v] for k, v in raices_todas.items()},
            "available": {k: any(p.is_dir() for p in v) for k, v in raices_todas.items()}}


def preview(handle: str, limit: int = 30) -> dict:
    """Qué va a entrar: primeros `limit` turnos + lo que se descarta."""
    source, ruta = resolver(handle)
    parseo = _parsear(source, ruta, limite=0)
    turnos = parseo.turnos
    primeros = turnos[:max(1, limit)]
    return {
        "source": source,
        "name": ruta.name,
        "cwd": parseo.cwd,
        "model": parseo.modelo,
        "external_session": parseo.session_id,
        "turns_total": len(turnos),
        "truncated": parseo.truncado,
        "skipped": parseo.descartado,
        "turns": [{"role": t.role,
                   "content": t.content[:_PREVIEW_CHARS],
                   "chars": len(t.content),
                   "timestamp": t.timestamp} for t in primeros],
        "first": str(ruta),
    }


def _titulo(turnos: list[Turno], alternativo: str) -> str:
    for t in turnos:
        if t.role == "user":
            limpio = " ".join(t.content.replace("**", "").replace("`", "").split())
            if limpio:
                return limpio[:80]
    return alternativo[:80]


def aplicar(handle: str, session_log, project: str = "", limite: int = 0) -> dict:
    """Crea la sesión y escribe los turnos. Devuelve el id nuevo."""
    source, ruta = resolver(handle)
    tope = min(limite or _MAX_TURNOS, _MAX_TURNOS)
    parseo = _parsear(source, ruta, limite=tope)
    turnos = parseo.turnos
    if not turnos:
        raise ValueError("la sesión externa no tiene turnos de texto")
    project = (project or f"importado-{source}").strip()[:60]
    titulo = _titulo(turnos, ruta.stem)
    sess = session_log.create_session(project=project)
    session_log.update_session(sess.id, title=titulo)
    escritos = 0
    for t in turnos:
        try:
            session_log.append(sess.id, _mensaje(t))
            escritos += 1
        except Exception as e:
            logger.debug("turno no importado: %s", e)
    return {
        "session_id": sess.id,
        "project": project,
        "title": titulo,
        "imported": escritos,
        "skipped": parseo.descartado,
        "truncated": parseo.truncado or (tope < len(turnos)),
        "source": source,
    }


def _mensaje(turno: Turno):
    from src.core.session_log import Message
    return Message(role=turno.role, content=turno.content,
                   created_at=turno.timestamp or datetime.now(timezone.utc).isoformat())
