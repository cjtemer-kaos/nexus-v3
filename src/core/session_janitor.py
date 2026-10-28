"""Borrado completo y auditoría de todo lo que cuelga de una sesión.

`DELETE FROM sessions` sólo limpia `messages`, `session_fts` y `compaction_state`
(FK `ON DELETE CASCADE` + triggers). El resto del rastro sigue en disco, y por
eso borrar desde la UI deja cosas atrás:

    session.db       todos, goals                    (no tienen FK)
    approvals.db     approval_grants                 (otra base)
    event_store.db   events                          (log append-only)
    data/            checkpoints/<id>/
                     git_checkpoints/<id>-<hash8>/   (nombre derivado)
                     spill/<sha1(id)[:12]>/          (carpeta derivada)

Este módulo es la única fuente de verdad de *dónde* vive cada artefacto:

  - `collect(sid)`        inventario de una sesión (exista o no).
  - `audit()`             qué quedó huérfano de sesiones que ya no existen.
  - `purge(apply=False)`  limpia huérfanos; sin `apply` sólo informa.
  - `delete_session(sid)` borra la sesión y TODO su rastro, en orden seguro.

Reglas de diseño:

  - Las rutas derivadas se piden a cada módulo (`checkpoints.session_dir`,
    `git_checkpoints.repo_dir`, `spill.spill_dir`); nunca se recalculan acá.
  - `audit()` abre las bases en `mode=ro`: auditar no puede cambiar nada.
  - `event_store` no se toca por defecto: es la auditoría de lo que pasó. Se
    puede purgar explícitamente (`events=True`) porque sus ids muertos son la
    mayor fuente de ruido.
  - Nada se borra sin que el llamador pida `apply`/`delete`.
"""
from __future__ import annotations

import os
import shutil
import sqlite3
import stat
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from src.core import checkpoints, git_checkpoints, spill
from src.core.environment import data_home

# Tablas de `session.db` que cuelgan de la sesión, con la columna que las une.
# `sessions` va aparte: es la fila raíz (cascade a messages/compaction_state).
_SESSION_TABLES = (
    ("messages", "session_id"),
    ("compaction_state", "session_id"),
    ("todos", "session_id"),
    ("goals", "session_id"),
)

# Lo que sobrevive a un `DELETE FROM sessions` (lo que se acumulaba sin avisar).
_LEFT_AFTER_DELETE = ("todos", "goals", "approval_grants",
                      "checkpoints", "git_checkpoints", "spill")


class SessionNotFound(Exception):
    """El id no existe en `session.db`."""


class SessionHasChildren(Exception):
    """Hay forks/branches colgando: la FK protege y no se borra el padre."""

    def __init__(self, session_id: str, children: list[str]):
        self.session_id = session_id
        self.children = children
        super().__init__(
            f"tiene {len(children)} sesión(es) hija(s): "
            f"{', '.join(children[:5])}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _approvals_db() -> Path:
    """Archivo real de grants (el del singleton, no una ruta fija)."""
    try:
        from src.core.approvals import approvals_db_path
        return Path(approvals_db_path())
    except Exception:
        return data_home() / "approvals.db"


def paths(db_path: str | Path | None = None) -> dict[str, Path]:
    """Rutas de las bases que intervienen en una sesión.

    `root` es el directorio de datos de esa base: en producción `data/`, y en una
    base secundaria (tests, un archivo suelto) su propio directorio. Así los
    artefactos en disco se buscan donde van con ESA base, no en `data/` global.
    """
    base = data_home()
    session = Path(db_path) if db_path else base / "session.db"
    root = session.parent
    try:
        same_root = root.resolve() == base.resolve()
    except OSError:
        same_root = False
    return {
        "session": session,
        "root": root,
        # Con la base de la instalación se usa el archivo real del singleton;
        # con una base aparte, sus vecinos, para no tocar datos de producción.
        "approvals": _approvals_db() if same_root else root / "approvals.db",
        "events": root / "event_store.db",
    }


def dir_roots(root: Path | None = None) -> dict[str, Path]:
    """Raíces de los artefactos en disco.

    Con el `data/` de la instalación se usan las funciones de cada módulo, que
    respetan `NEXUS_CHECKPOINTS_DIR` y compañía. Con otra raíz (tests) se
    reusan sólo los *nombres* derivados y se cuelga de esa raíz.
    """
    base = Path(root) if root else data_home()
    try:
        default = base.resolve() == data_home().resolve()
    except OSError:
        default = False
    if default:
        return {"checkpoints": checkpoints.checkpoints_root(),
                "git_checkpoints": git_checkpoints._checkpoints_root(),
                "spill": spill._spill_root()}
    return {"checkpoints": base / "checkpoints",
            "git_checkpoints": base / "git_checkpoints",
            "spill": base / "spill"}


def artifact_dir(store: str, session_id: str, roots: dict[str, Path]) -> Path:
    """Carpeta de una sesión en `store`, con el nombre que usa su módulo."""
    sid = session_id or "default"
    name = {
        "checkpoints": checkpoints.session_dir(sid).name,
        "git_checkpoints": git_checkpoints.repo_dir(sid).name,
        "spill": spill.spill_dir(sid).name,
    }[store]
    return roots[store] / name


@contextmanager
def _connect(path: Path, *, write: bool = False):
    """Conexión a una base; en modo lectura abre `mode=ro` o devuelve None.

    Auditar nunca puede escribir: si el archivo no existe, el llamador ve None
    y cuenta 0. En escritura, `foreign_keys=ON` es obligatorio (sqlite lo apaga
    por conexión y sin eso el cascade de `messages` no dispara).
    """
    if not write and not Path(path).exists():
        yield None
        return
    if write:
        conn = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA busy_timeout=10000")
        conn.execute("PRAGMA foreign_keys=ON")
    else:
        conn = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro",
                               uri=True, timeout=10.0)
    try:
        yield conn
    finally:
        conn.close()


def _has_table(conn: sqlite3.Connection | None, name: str) -> bool:
    if conn is None:
        return False
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone()
    return row is not None


def _count(conn: sqlite3.Connection | None, sql: str, args: tuple = ()) -> int:
    if conn is None:
        return 0
    try:
        return int(conn.execute(sql, args).fetchone()[0])
    except sqlite3.Error:
        return 0


def _tree_stats(path: Path) -> tuple[int, int]:
    """(archivos, bytes) de un directorio, sin seguir symlinks."""
    if not path.is_dir():
        return (0, 0)
    files = 0
    size = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            p = Path(root) / n
            try:
                if p.is_symlink():
                    continue
                size += p.stat().st_size
                files += 1
            except OSError:
                continue
    return (files, size)


def _rmtree(path: Path) -> bool:
    """Borra un árbol; en Windows los objetos de git nacen read-only."""
    if not path.exists():
        return False
    try:
        shutil.rmtree(path)
        return True
    except Exception:
        for root, dirs, files in os.walk(path):
            for n in dirs + files:
                try:
                    os.chmod(Path(root) / n, stat.S_IWRITE)
                except OSError:
                    pass
        shutil.rmtree(path)
        return True


def _artifact(store: str, kind: str, path: Path | str, count: int,
              size: int = 0, detail: str = "") -> dict:
    return {"store": store, "kind": kind, "path": str(path), "count": count,
            "bytes": size, "detail": detail}


def collect(session_id: str, *, db_path: str | Path | None = None,
            events: bool = False) -> dict:
    """Inventario completo de una sesión, exista o no en `sessions`.

    Devuelve `leftovers`: los stores que hoy sobrevivirían a un
    `DELETE FROM sessions` (es decir, los que se acumulaban).
    """
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id requerido")
    p = paths(db_path)
    roots = dir_roots(p["root"])
    artifacts: list[dict] = []

    with _connect(p["session"]) as conn:
        rows = {"sessions": _count(
            conn, "SELECT COUNT(*) FROM sessions WHERE id=?", (sid,))}
        for table, col in _SESSION_TABLES:
            rows[table] = _count(
                conn, f"SELECT COUNT(*) FROM {table} WHERE {col}=?", (sid,))
    for table in ("sessions",) + tuple(t for t, _ in _SESSION_TABLES):
        artifacts.append(_artifact(table, "row", p["session"], rows.get(table, 0)))

    with _connect(p["approvals"]) as conn:
        n_grants = _count(
            conn, "SELECT COUNT(*) FROM approval_grants WHERE session_id=?", (sid,))
    artifacts.append(_artifact("approval_grants", "row", p["approvals"], n_grants))

    for store, path in (
            ("checkpoints", artifact_dir("checkpoints", sid, roots)),
            ("git_checkpoints", artifact_dir("git_checkpoints", sid, roots)),
            ("spill", artifact_dir("spill", sid, roots))):
        files, size = _tree_stats(path)
        artifacts.append(_artifact(store, "dir", path, files, size))

    if events:
        with _connect(p["events"]) as conn:
            n_events = _count(
                conn, "SELECT COUNT(*) FROM events WHERE session_id=?", (sid,))
        artifacts.append(_artifact("events", "row", p["events"], n_events,
                                   detail="log append-only, no se borra solo"))

    leftovers = [a["store"] for a in artifacts
                 if a["count"] > 0 and a["store"] in _LEFT_AFTER_DELETE]
    return {
        "session_id": sid,
        "exists": rows.get("sessions", 0) > 0,
        "artifacts": artifacts,
        "leftovers": leftovers,
        "total_count": sum(a["count"] for a in artifacts),
        "total_bytes": sum(a["bytes"] for a in artifacts),
    }


def _orphan_dirs(root: Path, live_names: set[str]) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(d for d in root.iterdir()
                  if d.is_dir() and d.name not in live_names)


def _distinct(conn: sqlite3.Connection | None, sql: str,
              args: tuple = ()) -> list[str]:
    if conn is None:
        return []
    try:
        return [r[0] for r in conn.execute(sql, args)]
    except sqlite3.Error:
        return []


def audit(*, db_path: str | Path | None = None,
          legacy_spill: bool = True) -> dict:
    """Radiografía de la base y de todo lo que dejó atrás. Sólo lectura.

    `ok` es True cuando no hay ningún hallazgo de severidad `error`
    (inconsistencia real). Los huérfanos por borrados viejos son `warn`: no
    rompen nada, pero son exactamente lo que el usuario ve como "quedó".
    """
    p = paths(db_path)
    findings: list[dict] = []

    def add(code: str, severity: str, count: int, detail: str,
            samples: list | None = None, fixable: bool = True) -> None:
        findings.append({"code": code, "severity": severity, "count": int(count),
                         "detail": detail, "samples": (samples or [])[:5],
                         "fixable": fixable})

    with _connect(p["session"]) as conn:
        n_sessions = _count(conn, "SELECT COUNT(*) FROM sessions")
        n_messages = _count(conn, "SELECT COUNT(*) FROM messages")
        live = _distinct(conn, "SELECT id FROM sessions")
        live_set = set(live)

        orphan_msgs = _count(
            conn, "SELECT COUNT(*) FROM messages "
                  "WHERE session_id NOT IN (SELECT id FROM sessions)")
        add("orphan_messages", "error", orphan_msgs,
            "mensajes de sesiones inexistentes (la FK no está protegiendo)",
            _distinct(conn, "SELECT DISTINCT session_id FROM messages "
                            "WHERE session_id NOT IN (SELECT id FROM sessions)"))

        dangling = _count(
            conn, "SELECT COUNT(*) FROM sessions "
                  "WHERE parent_session_id IS NOT NULL AND parent_session_id<>'' "
                  "AND parent_session_id NOT IN (SELECT id FROM sessions)")
        add("dangling_parent", "error", dangling,
            "sesiones que dicen venir de un padre borrado (fork roto)",
            _distinct(conn, "SELECT id FROM sessions "
                            "WHERE parent_session_id IS NOT NULL "
                            "AND parent_session_id<>'' AND parent_session_id "
                            "NOT IN (SELECT id FROM sessions)"))

        fts_missing = _count(
            conn, "SELECT COUNT(*) FROM messages "
                  "WHERE id NOT IN (SELECT rowid FROM session_fts)")
        fts_extra = _count(
            conn, "SELECT COUNT(*) FROM session_fts "
                  "WHERE rowid NOT IN (SELECT id FROM messages)")
        add("fts_mismatch", "error", fts_missing + fts_extra,
            "índice de búsqueda desincronizado con los mensajes "
            f"(faltan={fts_missing}, sobran={fts_extra})")

        empties = [sid for sid in live if _count(
            conn, "SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)) == 0]
        add("empty_sessions", "info", len(empties),
            "sesiones sin mensajes (la UI crea una vacía al abrir; no es fantasma)",
            empties, fixable=False)

        for table, col in _SESSION_TABLES:
            if table == "messages":
                continue
            n = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE {col} "
                             f"NOT IN (SELECT id FROM sessions)")
            add(f"orphan_{table}", "warn", n,
                f"filas de `{table}` de sesiones borradas "
                "(sin FK: el DELETE de la sesión no las toca)")

    with _connect(p["approvals"]) as conn:
        dangling_grants = {sid for sid in _distinct(
            conn, "SELECT DISTINCT session_id FROM approval_grants")} - live_set
        add("orphan_approval_grants", "warn", len(dangling_grants),
            "permisos otorgados a sesiones que ya no existen",
            sorted(dangling_grants))

    roots = dir_roots(p["root"])
    live_ck = {artifact_dir("checkpoints", sid, roots).name for sid in live}
    ck_orphans = _orphan_dirs(roots["checkpoints"], live_ck)
    ck_files = sum(_tree_stats(d)[0] for d in ck_orphans)
    ck_size = sum(_tree_stats(d)[1] for d in ck_orphans)
    add("orphan_checkpoints", "warn", len(ck_orphans),
        f"carpetas de snapshot de sesiones borradas ({ck_files} archivos, "
        f"{ck_size // 1024} KB)", [d.name for d in ck_orphans])

    live_git = {artifact_dir("git_checkpoints", sid, roots).name for sid in live}
    git_orphans = _orphan_dirs(roots["git_checkpoints"], live_git)
    git_size = sum(_tree_stats(d)[1] for d in git_orphans)
    add("orphan_git_checkpoints", "warn", len(git_orphans),
        f"repos de checkpoints de sesiones borradas ({git_size // 1024} KB)",
        [d.name for d in git_orphans])

    live_spill = {artifact_dir("spill", sid, roots).name for sid in live}
    spill_orphans = _orphan_dirs(roots["spill"], live_spill)
    spill_size = sum(_tree_stats(d)[1] for d in spill_orphans)
    add("orphan_spill", "warn", len(spill_orphans),
        f"volcados de tools de sesiones borradas ({spill_size // 1024} KB)",
        [d.name for d in spill_orphans])

    unattributed: list[dict] = []
    if legacy_spill:
        root = roots["spill"]
        legacy = [f for f in root.iterdir()
                  if f.is_file() and not f.is_symlink()] if root.is_dir() else []
        if legacy:
            size = sum(f.stat().st_size for f in legacy)
            add("legacy_spill_files", "info", len(legacy),
                "volcados de formato viejo sin carpeta de sesión: no se pueden "
                f"atribuir ({size // 1024} KB)", [f.name for f in legacy])
            unattributed.append({"store": "spill_legacy", "count": len(legacy),
                                 "bytes": size})

    with _connect(p["events"]) as conn:
        n_events = _count(conn, "SELECT COUNT(*) FROM events")
        event_ids = set(_distinct(conn, "SELECT DISTINCT session_id FROM events "
                                        "WHERE session_id IS NOT NULL"))
        dead_events = event_ids - live_set
        n_dead = 0
        if dead_events:
            marcas = ",".join("?" * len(dead_events))
            n_dead = _count(conn, f"SELECT COUNT(*) FROM events "
                                  f"WHERE session_id IN ({marcas})",
                            tuple(dead_events))
    add("orphan_events", "info", len(dead_events),
        f"sesiones con eventos y ya inexistentes ({n_dead} de {n_events} eventos): "
        "el log es append-only por diseño")

    return {
        "ok": not any(f["severity"] == "error" and f["count"] > 0
                      for f in findings),
        "checked_at": _now(),
        "database": str(p["session"]),
        "sessions": n_sessions,
        "messages": n_messages,
        "findings": findings,
        "unattributed": unattributed,
        "totals": {
            "errors": sum(f["count"] for f in findings if f["severity"] == "error"),
            "warnings": sum(f["count"] for f in findings
                            if f["severity"] == "warn"),
            "orphan_bytes": ck_size + git_size + spill_size,
        },
    }


def _orphan_plan(*, db_path: str | Path | None = None,
                 events: bool = False,
                 legacy_spill: bool = False) -> dict:
    """Qué borraría `purge`. Todo el cálculo es de sólo lectura."""
    p = paths(db_path)
    roots = dir_roots(p["root"])
    plan: dict[str, dict] = {}

    with _connect(p["session"]) as conn:
        live = set(_distinct(conn, "SELECT id FROM sessions"))
        plan["_live"] = {"kind": "meta", "count": len(live)}
        for table in ("messages", "todos", "goals", "compaction_state"):
            n = _count(conn, f"SELECT COUNT(*) FROM {table} "
                             f"WHERE session_id NOT IN (SELECT id FROM sessions)")
            plan[table] = {"kind": "row", "count": n}
        fts_missing = _count(
            conn, "SELECT COUNT(*) FROM messages "
                  "WHERE id NOT IN (SELECT rowid FROM session_fts)")
        fts_extra = _count(
            conn, "SELECT COUNT(*) FROM session_fts "
                  "WHERE rowid NOT IN (SELECT id FROM messages)")
        plan["session_fts"] = {"kind": "repair", "count": fts_missing + fts_extra}

    with _connect(p["approvals"]) as conn:
        dead_grants = [sid for sid in _distinct(
            conn, "SELECT DISTINCT session_id FROM approval_grants") if sid not in live]
        plan["approval_grants"] = {"kind": "row", "count": len(dead_grants),
                                   "ids": dead_grants}

    live_ck = {artifact_dir("checkpoints", sid, roots).name for sid in live}
    ck = _orphan_dirs(roots["checkpoints"], live_ck)
    plan["checkpoints"] = {"kind": "dir", "count": len(ck),
                           "bytes": sum(_tree_stats(d)[1] for d in ck),
                           "items": [str(d) for d in ck]}

    live_git = {artifact_dir("git_checkpoints", sid, roots).name for sid in live}
    git = _orphan_dirs(roots["git_checkpoints"], live_git)
    plan["git_checkpoints"] = {"kind": "dir", "count": len(git),
                               "bytes": sum(_tree_stats(d)[1] for d in git),
                               "items": [str(d) for d in git]}

    live_spill = {artifact_dir("spill", sid, roots).name for sid in live}
    sp = _orphan_dirs(roots["spill"], live_spill)
    plan["spill"] = {"kind": "dir", "count": len(sp),
                     "bytes": sum(_tree_stats(d)[1] for d in sp),
                     "items": [str(d) for d in sp]}

    if legacy_spill:
        root = roots["spill"]
        files = [f for f in root.iterdir()
                 if f.is_file() and not f.is_symlink()] if root.is_dir() else []
        plan["spill_legacy"] = {
            "kind": "file", "count": len(files),
            "bytes": sum(f.stat().st_size for f in files),
            "items": [str(f) for f in files]}

    if events:
        with _connect(p["events"]) as conn:
            dead = [sid for sid in _distinct(
                conn, "SELECT DISTINCT session_id FROM events "
                      "WHERE session_id IS NOT NULL") if sid not in live]
        plan["events"] = {"kind": "row", "count": len(dead), "ids": dead}

    return plan


def purge(*, apply: bool = False, db_path: str | Path | None = None,
          events: bool = False, legacy_spill: bool = False,
          force: bool = False) -> dict:
    """Limpia lo que dejaron sesiones inexistentes. Sin `apply` es dry-run.

    `events=True` purga también el log de eventos de sesiones muertas. Está
    apagado por defecto: el event-store es el registro de lo que pasó y borrarlo
    destruye evidencia, no sólo basura.

    Con la base en cero no se tocan los directorios salvo `force=True`: sin
    ninguna sesión viva no se puede distinguir "base limpia" de "base perdida"
    y borrar todo `data/checkpoints` por error no tiene vuelta atrás.
    """
    p = paths(db_path)
    plan = _orphan_plan(db_path=db_path, events=events,
                        legacy_spill=legacy_spill)
    total = {k: v["count"] for k, v in plan.items() if not k.startswith("_")}
    resumen = {"dry_run": not apply, "plan": total,
               "bytes": sum(v.get("bytes", 0) for v in plan.values())}
    if not apply:
        return resumen

    deleted: dict[str, int] = {}
    errores: list[str] = []
    proteger_dirs = plan.get("_live", {}).get("count", 0) == 0 and not force

    with _connect(p["session"], write=True) as conn:
        try:
            conn.execute("BEGIN IMMEDIATE")
            for table in ("messages", "todos", "goals", "compaction_state"):
                if not _has_table(conn, table):
                    deleted[table] = 0
                    continue
                cur = conn.execute(
                    f"DELETE FROM {table} WHERE session_id "
                    f"NOT IN (SELECT id FROM sessions)")
                deleted[table] = cur.rowcount or 0
            if plan["session_fts"]["count"] and _has_table(conn, "session_fts"):
                conn.execute("INSERT INTO session_fts(session_fts) VALUES('rebuild')")
                deleted["session_fts"] = plan["session_fts"]["count"]
            conn.execute("COMMIT")
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"no se pudo purgar session.db: {e}") from e

    ids = plan["approval_grants"].get("ids") or []
    if ids:
        with _connect(p["approvals"], write=True) as conn:
            deleted["approval_grants"] = _delete_ids(conn, "approval_grants", ids)

    ids = plan.get("events", {}).get("ids") or []
    if ids:
        with _connect(p["events"], write=True) as conn:
            deleted["events"] = _delete_ids(conn, "events", ids)

    for store in ("checkpoints", "git_checkpoints", "spill"):
        n = 0
        if proteger_dirs:
            deleted[store] = 0
            continue
        for item in plan[store].get("items", []):
            try:
                if _rmtree(Path(item)):
                    n += 1
            except Exception as e:
                errores.append(f"{store}: {item}: {e}")
        deleted[store] = n

    if proteger_dirs:
        errores.append(
            "no se tocaron los directorios: la base no tiene ninguna sesión "
            "(¿se movió el archivo?). Pasá force=true si es intencional.")

    n = 0
    for item in plan.get("spill_legacy", {}).get("items", []):
        if proteger_dirs:
            break
        try:
            Path(item).unlink()
            n += 1
        except OSError as e:
            errores.append(f"spill_legacy: {item}: {e}")
    if n:
        deleted["spill_legacy"] = n

    return {**resumen, "dry_run": False, "deleted": deleted, "errors": errores}


def _delete_ids(conn: sqlite3.Connection, table: str, ids: list[str]) -> int:
    """Borra por lista de ids en lotes (SQLite tiene tope de parámetros)."""
    total = 0
    for i in range(0, len(ids), 400):
        lote = ids[i:i + 400]
        marcas = ",".join("?" * len(lote))
        cur = conn.execute(f"DELETE FROM {table} WHERE session_id IN ({marcas})",
                           tuple(lote))
        total += cur.rowcount or 0
    return total


def delete_session(session_id: str, *, db_path: str | Path | None = None,
                   events: bool = False) -> dict:
    """Borra una sesión con TODO su rastro.

    Lanza `SessionNotFound` si no existe y `SessionHasChildren` si hay forks
    colgando (la FK protege el árbol; el llamador decide borrar los hijos).

    El borrado de `session.db` va en una transacción: si la fila raíz falla,
    `todos`/`goals` no se pierden. Los directorios son best-effort y se
    reportan en `errors` en vez de dejar la sesión a medio borrar.
    """
    sid = (session_id or "").strip()
    if not sid:
        raise ValueError("session_id requerido")
    p = paths(db_path)
    roots = dir_roots(p["root"])
    deleted: dict[str, int] = {}
    errores: list[str] = []

    dirs = {store: artifact_dir(store, sid, roots)
            for store in ("checkpoints", "git_checkpoints", "spill")}
    bytes_liberados = sum(_tree_stats(d)[1] for d in dirs.values())

    with _connect(p["session"], write=True) as conn:
        if _count(conn, "SELECT COUNT(*) FROM sessions WHERE id=?", (sid,)) == 0:
            raise SessionNotFound(sid)
        children = _distinct(
            conn, "SELECT id FROM sessions WHERE parent_session_id=?", (sid,))
        if children:
            raise SessionHasChildren(sid, children)

        antes = {
            "messages": _count(conn, "SELECT COUNT(*) FROM messages "
                                     "WHERE session_id=?", (sid,)),
            "compaction_state": _count(
                conn, "SELECT COUNT(*) FROM compaction_state WHERE session_id=?",
                (sid,)),
            "todos": _count(conn, "SELECT COUNT(*) FROM todos WHERE session_id=?",
                            (sid,)),
            "goals": _count(conn, "SELECT COUNT(*) FROM goals WHERE session_id=?",
                            (sid,)),
        }
        try:
            conn.execute("BEGIN IMMEDIATE")
            # `todos`/`goals` pueden no existir todavía (una base nueva las crea
            # sólo cuando alguien escribe una tarea).
            for table in ("todos", "goals"):
                if _has_table(conn, table):
                    conn.execute(f"DELETE FROM {table} WHERE session_id=?", (sid,))
            cur = conn.execute("DELETE FROM sessions WHERE id=?", (sid,))
            if not (cur.rowcount or 0):
                raise SessionNotFound(sid)
            conn.execute("COMMIT")
        except SessionNotFound:
            conn.execute("ROLLBACK")
            raise
        except Exception as e:
            conn.execute("ROLLBACK")
            raise RuntimeError(f"no se pudo borrar {sid}: {e}") from e
        deleted.update(antes)
        deleted["sessions"] = 1

    # A partir de acá la sesión ya no existe en `session.db`: lo que falle fuera
    # se reporta en `errors` y no convierte un borrado exitoso en un 409.
    if p["approvals"].exists():
        try:
            with _connect(p["approvals"], write=True) as conn:
                if _has_table(conn, "approval_grants"):
                    cur = conn.execute(
                        "DELETE FROM approval_grants WHERE session_id=?", (sid,))
                    deleted["approval_grants"] = cur.rowcount or 0
        except Exception as e:
            errores.append(f"approval_grants: {e}")

    if events and p["events"].exists():
        try:
            with _connect(p["events"], write=True) as conn:
                if _has_table(conn, "events"):
                    cur = conn.execute(
                        "DELETE FROM events WHERE session_id=?", (sid,))
                    deleted["events"] = cur.rowcount or 0
        except Exception as e:
            errores.append(f"events: {e}")

    for store, path in dirs.items():
        if not path.exists():
            deleted[store] = 0
            continue
        try:
            _rmtree(path)
            deleted[store] = 1
        except Exception as e:
            deleted[store] = 0
            errores.append(f"{store}: {path}: {e}")

    return {"ok": not errores, "session_id": sid, "deleted": deleted,
            "bytes_freed": bytes_liberados, "errors": errores}
