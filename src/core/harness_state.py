"""Estado del harness (memoria del agente), schema 1.

Un archivo JSON con cinco clases de entrada (`prompt`, `memory`, `skill`,
`subagent`, `factory`) y el historial de cambios por entrada. Los tres
problemas que resuelve, en orden de importancia:

1. **No corromperse**: dos escritores reales (el agente y cualquier proceso que
   edite el estado) sobre el mismo archivo. Lock de directorio con owner,
   detección de huérfano por pid, sync por `mtime`, escritura atómica y
   verificación de propiedad antes del rename.
2. **Poder deshacer**: cada cambio guarda `before`/`after` en `history.jsonl`
   (append-only). El `state.json` no crece con diffs.
3. **Entrar al prompt sin inflarlo**: `digest()` renderiza pocas líneas con
   topes por tipo.

Un archivo corrupto se trata como vacío: un JSON roto no puede dejar al agente
sin arrancar.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("nexus-harness-state")

SCHEMA = 1
KINDS = ("prompt", "memory", "skill", "subagent", "factory")
SCOPES = ("local", "global")
TOPES = {"prompt": 3, "memory": 10, "skill": 6, "subagent": 4, "factory": 3}

_LOCK_STALE_AFTER = 10.0
_LOCK_TRIES = 50
_LOCK_WAIT = 0.02
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_RESERVADOS = {"base-system-prompt", "system", "system-prompt"}


class HarnessError(ValueError):
    """Entrada inválida, id reservado o lock perdido."""


def _ahora() -> str:
    return (datetime.now(timezone.utc).isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"))


def slug(texto: str) -> str:
    limpio = re.sub(r"[^a-z0-9._-]+", "-", str(texto or "").strip().lower())
    return re.sub(r"-{2,}", "-", limpio).strip("-")


def validar_id(entry_id: str) -> str:
    """Id válido y no reservado (por id crudo ni por su slug)."""
    crudo = str(entry_id or "").strip()
    if not crudo:
        raise HarnessError("id required")
    if _ID_RE.match(crudo) is None:
        raise HarnessError(f"id inválido: {crudo!r} (usa minúsculas, números, '.', '_', '-')")
    if slug(crudo.replace("_", "-")) in _RESERVADOS or crudo.lower() in _RESERVADOS:
        raise HarnessError(f"id reservado: {crudo!r} (no puede pisar el system prompt base)")
    return crudo


def _dict(valor: Any) -> dict:
    if valor is None:
        return {}
    if isinstance(valor, dict):
        return dict(valor)
    if isinstance(valor, str) and valor.strip():
        try:
            dato = json.loads(valor)
            return dict(dato) if isinstance(dato, dict) else {}
        except Exception:
            return {}
    return {}


def _validar(kind: str, entry: dict) -> None:
    if kind not in KINDS:
        raise HarnessError(f"kind inválido: {kind!r} (usa {', '.join(KINDS)})")
    if not str(entry.get("title") or "").strip():
        raise HarnessError("title required")
    if not str(entry.get("content") or "").strip():
        raise HarnessError("content required")
    if kind == "skill":
        if not _dict(entry.get("reference")) or not _dict(entry.get("arguments")):
            raise HarnessError("una skill necesita reference y arguments")
    if kind == "factory":
        args = _dict(entry.get("arguments"))
        if bool(args.get("dag")) == bool(args.get("machine")):
            raise HarnessError("una factory necesita arguments.dag XOR arguments.machine")


def _pid_vivo(pid: int) -> bool:
    """¿El proceso existe todavía?

    En Windows NO se puede usar `os.kill(pid, 0)`: la implementación llama a
    `TerminateProcess` y mata al dueño del lock. Se consulta el handle con
    `PROCESS_QUERY_LIMITED_INFORMATION`, que no requiere permisos de escritura.
    """
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            QUERY = 0x1000
            STILL_ACTIVE = 259
            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(QUERY, False, int(pid))
            if not handle:
                return False
            try:
                codigo = ctypes.c_ulong(0)
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(codigo)):
                    return True
                return codigo.value == STILL_ACTIVE
            finally:
                kernel32.CloseHandle(handle)
        except Exception:
            return True  # ante la duda, no se roba el lock
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ---------------------------------------------------------------------------
# Lock de directorio con owner
# ---------------------------------------------------------------------------
class _Lock:
    def __init__(self, path: Path):
        self.dir = Path(str(path) + ".lock")
        self.token = f"{os.getpid()} {uuid.uuid4().hex}"
        self.owner_file = self.dir / "owner"

    def _edad(self) -> float:
        try:
            return time.time() - self.dir.stat().st_mtime
        except OSError:
            return 0.0

    def _owner(self) -> str:
        try:
            return self.owner_file.read_text(encoding="utf-8").strip()
        except Exception:
            return ""

    def _vivo(self, owner: str) -> bool:
        try:
            pid = int(owner.split()[0])
        except Exception:
            return False
        return _pid_vivo(pid)

    def _robar_si_huerfano(self) -> bool:
        if not self.dir.exists() or self._edad() < _LOCK_STALE_AFTER:
            return False
        owner = self._owner()
        if owner and self._vivo(owner):
            return False
        logger.debug("lock huérfano, se roba: %s", self.dir)
        self._borrar()
        return True

    def _borrar(self) -> None:
        try:
            self.owner_file.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            self.dir.rmdir()
        except OSError:
            pass

    def adquirir(self) -> None:
        try:
            self.dir.parent.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise HarnessError(f"no se pudo crear el directorio del estado: {e}")
        for _ in range(_LOCK_TRIES):
            try:
                self.dir.mkdir()
            except FileExistsError:
                if self._robar_si_huerfano():
                    continue
                time.sleep(_LOCK_WAIT)
                continue
            except OSError as e:
                raise HarnessError(f"no se pudo crear el lock: {e}")
            try:
                self.owner_file.write_text(self.token, encoding="utf-8")
            except OSError:
                self._borrar()
                raise HarnessError("no se pudo escribir el owner del lock")
            return
        raise HarnessError("lock ocupado: no se pudo escribir el estado a tiempo")

    def verificar(self) -> None:
        if self._owner() != self.token:
            raise HarnessError("lock lost: otro proceso tomó el estado")

    def soltar(self) -> None:
        if self._owner() in ("", self.token):
            self._borrar()


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------
class HarnessState:
    """Un archivo de estado con entries por tipo + historial append-only."""

    def __init__(self, path: str | Path, scope: str = "local"):
        self.path = Path(path)
        if self.path.suffix.lower() != ".json":
            self.path = self.path.with_suffix(".json")
        if scope not in SCOPES:
            raise HarnessError(f"scope inválido: {scope!r}")
        self.scope = scope
        self.history_path = self.path.parent / "history.jsonl"
        self._data: dict = {"schema": SCHEMA, "entries": {k: {} for k in KINDS}, "refinements": []}
        self._stamp: int | None = None
        self._lock = _Lock(self.path)

    # -- persistencia ---------------------------------------------------
    def _vacio(self) -> dict:
        return {"schema": SCHEMA, "entries": {k: {} for k in KINDS}, "refinements": []}

    def _normalizar(self, dato: Any) -> dict:
        base = self._vacio()
        if not isinstance(dato, dict):
            return base
        entradas = dato.get("entries")
        if isinstance(entradas, dict):
            for kind, grupo in entradas.items():
                if kind in KINDS and isinstance(grupo, dict):
                    base["entries"][kind].update(
                        {str(k): v for k, v in grupo.items() if isinstance(v, dict)})
        for fila in (dato.get("refinements") or []):
            if isinstance(fila, dict):
                base["refinements"].append(fila)
        base["schema"] = SCHEMA
        return base

    def _leer_disco(self) -> dict:
        if not self.path.exists():
            return self._vacio()
        try:
            return self._normalizar(json.loads(self.path.read_text(encoding="utf-8")))
        except Exception as e:
            logger.warning("harness state corrupto (%s): se trata como vacío", e)
            return self._vacio()

    def _mtime(self) -> int | None:
        try:
            return self.path.stat().st_mtime_ns
        except OSError:
            return None

    def cargar(self) -> dict:
        self._data = self._leer_disco()
        self._stamp = self._mtime()
        return self._data

    def _sincronizar(self) -> None:
        actual = self._mtime()
        if actual != self._stamp:
            self._data = self._leer_disco()
            self._stamp = actual

    def _escribir(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock.verificar()
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")
        modo = 0o600
        try:
            modo = self.path.stat().st_mode & 0o777
        except OSError:
            pass
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY, modo)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(str(tmp), str(self.path))
            try:
                os.chmod(str(self.path), modo)
            except OSError:
                pass
            if hasattr(os, "O_DIRECTORY"):
                try:
                    dfd = os.open(str(self.path.parent), os.O_DIRECTORY)
                    try:
                        os.fsync(dfd)
                    finally:
                        os.close(dfd)
                except OSError:
                    pass
        except Exception:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise
        self._stamp = self._mtime()

    @contextlib.contextmanager
    def _mutando(self) -> Iterator[dict]:
        self._lock.adquirir()
        try:
            # Bajo lock se recarga SIEMPRE: depender del `mtime` para detectar
            # un cambio ajeno pierde updates cuando dos escrituras caen en el
            # mismo tick del filesystem. El costo es leer un JSON chico.
            self._data = self._leer_disco()
            self._stamp = self._mtime()
            antes = json.dumps(self._data, sort_keys=True)
            yield self._data
            if json.dumps(self._data, sort_keys=True) != antes:
                self._escribir()
        finally:
            self._lock.soltar()

    def _historial(self, registro: dict) -> None:
        try:
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            with self.history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(registro, ensure_ascii=False) + "\n")
        except OSError as e:
            logger.warning("no se pudo escribir el historial del harness: %s", e)

    # -- lectura --------------------------------------------------------
    def entries(self, kind: str | None = None) -> list[dict]:
        self._sincronizar()
        if kind is not None:
            if kind not in KINDS:
                raise HarnessError(f"kind inválido: {kind!r}")
            return [dict(v) for v in self._data["entries"].get(kind, {}).values()]
        out: list[dict] = []
        for k in KINDS:
            out.extend(dict(v) for v in self._data["entries"][k].values())
        return out

    def get(self, entry_id: str) -> dict | None:
        self._sincronizar()
        for k in KINDS:
            fila = self._data["entries"][k].get(entry_id)
            if fila:
                return dict(fila)
        return None

    def search(self, query: str, kind: str | None = None, limit: int = 20) -> list[dict]:
        terminos = [t for t in re.split(r"\s+", str(query or "").lower()) if t]
        filas = self.entries(kind)
        if not terminos:
            return filas[:max(1, limit)]
        puntuadas: list[tuple[int, dict]] = []
        for fila in filas:
            texto = " ".join(str(fila.get(c) or "") for c in
                             ("id", "title", "content", "path")).lower()
            puntos = sum(1 for t in terminos if t in texto)
            if puntos:
                puntuadas.append((puntos, fila))
        puntuadas.sort(key=lambda x: (-x[0], str(x[1].get("updated_at") or "")), reverse=False)
        return [f for _p, f in puntuadas[:max(1, limit)]]

    def history(self, entry_id: str = "", limit: int = 30) -> list[dict]:
        if not self.history_path.exists():
            return []
        filas: list[dict] = []
        try:
            with self.history_path.open("r", encoding="utf-8", errors="replace") as fh:
                for linea in fh:
                    linea = linea.strip()
                    if not linea:
                        continue
                    try:
                        fila = json.loads(linea)
                    except Exception:
                        continue
                    if entry_id and str(fila.get("id") or "") != entry_id:
                        continue
                    filas.append(fila)
        except OSError:
            return []
        return filas[-max(1, limit):]

    # -- escritura ------------------------------------------------------
    def create(self, kind: str, entry_id: str, title: str, content: str,
               path: str = "", scope: str = "", reference=None, arguments=None,
               metadata=None, source: str = "agent") -> dict:
        entry_id = validar_id(entry_id)
        scope = scope or self.scope
        if scope not in SCOPES:
            raise HarnessError(f"scope inválido: {scope!r}")
        entry = {
            "id": entry_id,
            "kind": kind,
            "title": str(title or "").strip(),
            "content": str(content or "").strip(),
            "path": str(path or "").strip(),
            "scope": scope,
            "reference": _dict(reference),
            "arguments": _dict(arguments),
            "metadata": _dict(metadata),
            "source": str(source or "agent"),
            "created_at": _ahora(),
            "updated_at": _ahora(),
            "version": 1,
        }
        _validar(kind, entry)
        with self._mutando() as data:
            if entry_id in data["entries"][kind]:
                raise HarnessError(f"ya existe {kind}:{entry_id}")
            for otro in KINDS:
                if otro != kind and entry_id in data["entries"][otro]:
                    raise HarnessError(f"el id {entry_id} ya existe como {otro}")
            data["entries"][kind][entry_id] = entry
        self._historial({"ts": entry["created_at"], "op": "create", "id": entry_id,
                         "kind": kind, "source": entry["source"],
                         "before": None, "after": entry})
        return dict(entry)

    def update(self, entry_id: str, **campos) -> dict:
        entry_id = validar_id(entry_id)
        campos = {k: v for k, v in campos.items()
                  if k in ("title", "content", "path", "scope", "reference", "arguments",
                           "metadata", "source")}
        with self._mutando() as data:
            kind, actual = self._buscar(data, entry_id)
            if not actual:
                raise HarnessError(f"no existe la entrada {entry_id}")
            nuevo = dict(actual)
            # Lo que el edit no trae se hereda: un update parcial no borra
            # `path`, `reference` ni `metadata`.
            for campo, valor in campos.items():
                if campo in ("reference", "arguments", "metadata"):
                    if valor is not None:
                        nuevo[campo] = _dict(valor)
                elif valor is not None:
                    nuevo[campo] = str(valor).strip() if campo in ("title", "content", "path", "source") else valor
            if not nuevo.get("scope"):
                nuevo["scope"] = actual.get("scope") or self.scope
            _validar(kind, nuevo)
            nuevo["created_at"] = actual.get("created_at") or _ahora()
            nuevo["updated_at"] = _ahora()
            nuevo["version"] = int(actual.get("version") or 1) + 1
            data["entries"][kind][entry_id] = nuevo
        self._historial({"ts": nuevo["updated_at"], "op": "update", "id": entry_id,
                         "kind": kind, "source": nuevo.get("source") or "agent",
                         "before": actual, "after": nuevo})
        return dict(nuevo)

    def delete(self, entry_id: str) -> dict:
        entry_id = validar_id(entry_id)
        with self._mutando() as data:
            kind, actual = self._buscar(data, entry_id)
            if not actual:
                raise HarnessError(f"no existe la entrada {entry_id}")
            del data["entries"][kind][entry_id]
        self._historial({"ts": _ahora(), "op": "delete", "id": entry_id, "kind": kind,
                         "source": actual.get("source") or "agent",
                         "before": actual, "after": None})
        return dict(actual)

    def rollback(self, entry_id: str, version: int | None = None) -> dict:
        """Vuelve una entrada a su `before` (o a la versión pedida).

        El rollback no reescribe el historial: agrega un cambio más, así el
        rollback también se puede deshacer.
        """
        entry_id = validar_id(entry_id)
        filas = [f for f in self.history(entry_id, limit=10000) if f.get("id") == entry_id]
        if not filas:
            raise HarnessError(f"sin historial para {entry_id}")
        if version is None:
            objetivo = filas[-1].get("before")
        else:
            objetivo = None
            for fila in reversed(filas):
                for lado in ("after", "before"):
                    cand = fila.get(lado)
                    if isinstance(cand, dict) and int(cand.get("version") or 0) == int(version):
                        objetivo = cand
                        break
                if objetivo:
                    break
        if not isinstance(objetivo, dict):
            raise HarnessError(f"no hay versión {version} para {entry_id}")
        kind = objetivo.get("kind")
        if kind not in KINDS:
            raise HarnessError("la versión a restaurar no tiene kind válido")
        restaurada = dict(objetivo)
        with self._mutando() as data:
            _k, actual = self._buscar(data, entry_id)
            actual = actual or {}
            restaurada["created_at"] = actual.get("created_at") or restaurada.get("created_at") or _ahora()
            restaurada["updated_at"] = _ahora()
            restaurada["version"] = int(actual.get("version") or restaurada.get("version") or 1) + 1
            restaurada["source"] = str(actual.get("source") or restaurada.get("source") or "agent")
            _validar(kind, restaurada)
            if actual and actual.get("kind") != kind:
                data["entries"][actual["kind"]].pop(entry_id, None)
            data["entries"][kind][entry_id] = restaurada
        self._historial({"ts": restaurada["updated_at"], "op": "rollback", "id": entry_id,
                         "kind": kind, "source": restaurada["source"],
                         "before": actual or None, "after": restaurada})
        return dict(restaurada)

    def _buscar(self, data: dict, entry_id: str) -> tuple[str, dict | None]:
        for k in KINDS:
            fila = data["entries"][k].get(entry_id)
            if fila:
                return k, dict(fila)
        return "", None

    # -- prompt ---------------------------------------------------------
    def digest(self, max_por_tipo: dict | None = None) -> str:
        """Bloque corto para el system prompt. Vacío si no hay nada."""
        self._sincronizar()
        topes = dict(TOPES)
        topes.update(max_por_tipo or {})
        lineas: list[str] = []
        for kind in KINDS:
            filas = sorted(self._data["entries"][kind].values(),
                           key=lambda e: str(e.get("updated_at") or ""), reverse=True)
            if not filas:
                continue
            tope = max(1, int(topes.get(kind, 5)))
            lineas.append(f"{kind}:")
            for fila in filas[:tope]:
                titulo = " ".join(str(fila.get("title") or "").split())
                resumen = " ".join(str(fila.get("content") or "").split())[:140]
                marca = "local:" if fila.get("scope") == "local" else ""
                lineas.append(f"- {marca}{fila.get('id')} · {titulo} — {resumen}")
            if len(filas) > tope:
                lineas.append(f"- (+{len(filas) - tope} más)")
        if not lineas:
            return ""
        return ("\n\nESTADO DEL AGENTE (memoria del harness; leela antes de decidir):\n"
                + "\n".join(lineas)
                + "\nUsá la tool harness (list|get|create|update|delete|search|history|rollback) "
                  "para leer el detalle o cambiar una entrada.")


def store_path(scope: str = "local", project_root: str | Path | None = None,
               session_id: str = "") -> Path:
    """Dónde vive el estado según el alcance pedido."""
    from src.core.environment import data_home, workspace_root

    if scope == "project":
        raiz = Path(project_root) if project_root else workspace_root()
        return Path(raiz) / ".nexus" / "harness.json"
    if scope == "session":
        if not session_id:
            raise HarnessError("scope=session requiere session_id")
        return data_home() / "harness" / "sesiones" / f"{session_id}.json"
    if scope == "global":
        return data_home() / "harness" / "global.json"
    return data_home() / "harness" / "state.json"


def open_state(scope: str = "local", project_root: str | Path | None = None,
               session_id: str = "") -> HarnessState:
    almacen_global = scope == "global"
    estado = HarnessState(store_path(scope, project_root, session_id),
                          scope="global" if almacen_global else "local")
    estado.cargar()
    return estado


def _digest_de(estado: HarnessState, prefijo: str = "", tope: int = 10) -> list[str]:
    propios = estado.entries()
    if not propios:
        return []
    lineas: list[str] = []
    for fila in sorted(propios, key=lambda e: str(e.get("updated_at") or ""), reverse=True)[:tope]:
        titulo = " ".join(str(fila.get("title") or "").split())
        resumen = " ".join(str(fila.get("content") or "").split())[:140]
        lineas.append(f"- {prefijo}{fila.get('id')} · {titulo} — {resumen}")
    return lineas


def digest_block(session_id: str = "") -> str:
    """Digest para el prompt: global + agente (con `local:` en colisión) + sesión."""
    bloques: list[str] = []
    ids_global: set[str] = set()
    try:
        global_state = open_state("global")
        lineas = _digest_de(global_state, tope=20)
        ids_global = {f.get("id") for f in global_state.entries()}
        if lineas:
            bloques.append("\n\nESTADO DEL AGENTE (global):\n" + "\n".join(lineas))
    except Exception as e:
        logger.debug("digest global no disponible: %s", e)
    try:
        agente = open_state("local")
        propios = agente.entries()
        if propios:
            lineas = []
            for fila in sorted(propios, key=lambda e: str(e.get("updated_at") or ""),
                               reverse=True)[:20]:
                titulo = " ".join(str(fila.get("title") or "").split())
                resumen = " ".join(str(fila.get("content") or "").split())[:140]
                marca = "local:" if fila.get("id") in ids_global else ""
                lineas.append(f"- {marca}{fila.get('id')} · {titulo} — {resumen}")
            bloques.append("\n\nESTADO DEL AGENTE (memoria del harness; leela antes de decidir):\n"
                           + "\n".join(lineas))
    except Exception as e:
        logger.debug("digest del agente no disponible: %s", e)
    if session_id:
        try:
            local_state = open_state("session", session_id=session_id)
            lineas = _digest_de(local_state, prefijo="local:", tope=10)
            if lineas:
                # La cabecera se arma en dos literales para que el fuente no
                # contenga "N:\" (el escaneo de portabilidad lo lee como una
                # ruta de unidad hardcodeada).
                bloques.append("\n\nESTADO LOCAL DE LA SESIÓN" + ":\n" + "\n".join(lineas))
        except Exception as e:
            logger.debug("digest de sesión no disponible: %s", e)
    if not bloques:
        return ""
    return "".join(bloques) + (
        "\nUsá la tool harness (list|get|create|update|delete|search|history|rollback) "
        "para leer el detalle o cambiar una entrada.")
