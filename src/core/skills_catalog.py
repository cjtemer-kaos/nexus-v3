"""Catalogo de skills del proyecto: descubrimiento, busqueda y carga bajo demanda.

opencode expone cientos de skills con divulgacion progresiva (una lista corta en
el prompt y el contenido completo recien cuando se pide). nexus-v3 solo escaneaba
dos carpetas fijas y no tenia forma de cargar ninguna: el modelo nunca podia usar
una skill aunque estuviera en disco.

Aca viven las tres piezas que faltaban: un unico resolutor de roots (siempre
dentro del proyecto, para que nada dependa de otra instalacion), un indice
cacheado con busqueda por relevancia, y la carga del SKILL.md completo.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Iterable

logger = logging.getLogger(__name__)

_PROJECT_ROOT = Path(__file__).resolve().parents[2]

CACHE_TTL_S = 300.0
MAX_BODY_CHARS = 24000
MAX_HINT_CHARS = 1400
HINT_DESC_CHARS = 60

_lock = threading.Lock()
_cache: dict[tuple[str, ...], tuple[float, list[dict[str, Any]]]] = {}


def project_root() -> Path:
    return _PROJECT_ROOT


def _resolve_inside(root_str: str | Path) -> Path | None:
    """Resuelve un root y lo descarta si cae fuera del proyecto.

    Regla del harness: todo funciona dentro de nexus-v3. Un root externo haria
    que el catalogo dependa de otra instalacion (y de que ese disco exista).
    """
    raw = os.path.expandvars(str(root_str))
    try:
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = _PROJECT_ROOT / p
        p = p.resolve()
        p.relative_to(_PROJECT_ROOT)
    except (ValueError, OSError):
        logger.debug("root de skills ignorado por quedar fuera del proyecto: %s", root_str)
        return None
    return p


def skill_roots(cfg: dict | None = None) -> list[Path]:
    """Roots en orden de prioridad. Menor indice = menor rank = gana en empates."""
    entries: list[str] = []
    try:
        if cfg is None:
            from src.core.config import load_config

            cfg = load_config()
    except Exception:
        cfg = cfg or {}
    section = cfg.get("skills") or {}
    for raw in (section.get("roots") or []):
        entries.append(str(raw))
    for extra in (os.environ.get("NEXUS_V3_SKILL_ROOTS") or "").split(os.pathsep):
        if extra.strip():
            entries.append(extra.strip())
    if section.get("include_default_roots", True):
        entries.extend(["./skills", "./data/skills"])

    roots: list[Path] = []
    for raw in entries:
        p = _resolve_inside(raw)
        if p is not None and p.is_dir() and p not in roots:
            roots.append(p)
    return roots


SEARCH_BODY_CHARS = 1200


def _first_line(body: str) -> str:
    for line in (body or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and not s.startswith("```"):
            return s[:220]
    return ""


def _parse_lenient(md: Path) -> dict[str, Any]:
    """Lee un SKILL.md aceptando lo que el formato real trae.

    `scan_roots` es estricto a proposito (valida autores), pero el hub real tiene
    cientos de skills con `name:` en Title Case y algunas sin frontmatter. Con la
    regla estricta quedaban 208 skills invisibles para el agente; aca se rescatan
    usando el nombre de la carpeta y la primera linea del cuerpo como descripcion.
    """
    from src.core.skill_frontmatter import normalize_newlines, split_frontmatter

    text = normalize_newlines(md.read_text(encoding="utf-8", errors="replace"))
    fm_text, body, _err = split_frontmatter(text)
    fm: dict[str, Any] = {}
    if fm_text:
        try:
            import yaml

            data = yaml.safe_load(fm_text)
            if isinstance(data, dict):
                fm = data
        except Exception:
            fm = {}
    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        name = md.parent.name
    name = name.strip()
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        desc = _first_line(body)
    entry: dict[str, Any] = {"name": name, "description": re.sub(r"\s+", " ", desc).strip()}
    when = fm.get("whenToUse")
    if isinstance(when, str) and when.strip():
        entry["whenToUse"] = when.strip()
    # Texto extra para busqueda: en varias skills la descripcion es generica
    # ("Unless otherwise stated...") y el tema real solo aparece en el cuerpo.
    entry["_body"] = body[:SEARCH_BODY_CHARS]
    return entry


MAX_DEPTH = 5
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", "venv", "dist", "build", ".next", ".cache"}


def _iter_skill_files(root: Path):
    """`**/SKILL.md` con profundidad acotada (el hub real anida skills 3-4 niveles)."""
    for dirpath, dirnames, filenames in os.walk(root):
        try:
            rel = Path(dirpath).relative_to(root)
        except ValueError:
            continue
        depth = 0 if rel == Path(".") else len(rel.parts)
        if depth >= MAX_DEPTH:
            dirnames[:] = []
            continue
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and not d.startswith("."))
        if "SKILL.md" in filenames:
            yield Path(dirpath) / "SKILL.md"


def _scan(roots: Iterable[Path]) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    skipped = 0
    for rank, root in enumerate(roots):
        try:
            rroot = root.resolve()
        except OSError:
            continue
        for md in _iter_skill_files(root):
            try:
                if md.is_symlink():
                    resolved = md.resolve()
                    if resolved != rroot and rroot not in resolved.parents:
                        skipped += 1
                        continue
                entry = _parse_lenient(md)
            except OSError:
                skipped += 1
                continue
            key = str(entry["name"]).lower()
            if key in seen:
                continue
            seen.add(key)
            entry["path"] = str(md)
            entry["root"] = str(root)
            entry["rank"] = rank
            entry["dir"] = str(md.parent)
            # Indice de busqueda precalculado: evita re-lowercasear ~2 MB de
            # cuerpo en cada turno (search corre en el loop del agente).
            entry["_search"] = (
                str(entry.get("name") or "").lower(),
                str(entry.get("description") or "").lower(),
                str(entry.get("whenToUse") or "").lower(),
                str(entry.pop("_body", "") or "").lower(),
            )
            entries.append(entry)
    if skipped:
        logger.debug("skills ilegibles o fuera de root: %d", skipped)
    entries.sort(key=lambda s: (int(s.get("rank", 0)), str(s.get("name") or "").lower()))
    return entries


def _entries(cfg: dict | None = None, *, refresh: bool = False) -> list[dict[str, Any]]:
    """Indice compartido (no copiar ni mutar: es el objeto del cache)."""
    roots = skill_roots(cfg)
    if not roots:
        return []
    key = tuple(str(r) for r in roots)
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and not refresh and (now - hit[0]) < CACHE_TTL_S:
            return hit[1]
    entries = _scan(roots)
    with _lock:
        _cache[key] = (now, entries)
    return entries


def list_skills(cfg: dict | None = None, *, refresh: bool = False) -> list[dict[str, Any]]:
    """Skills instaladas (nombre unico, root de mayor prioridad gana)."""
    return [{k: v for k, v in e.items() if not k.startswith("_")} for e in _entries(cfg, refresh=refresh)]


def invalidate() -> None:
    """Olvida el indice (tras copiar o borrar skills en caliente)."""
    with _lock:
        _cache.clear()


def stats(cfg: dict | None = None) -> dict[str, Any]:
    roots = skill_roots(cfg)
    skills = _entries(cfg)
    return {
        "count": len(skills),
        "roots": [str(r) for r in roots],
        "cached": bool(_cache),
    }


_TERM_RE = re.compile(r"\w{3,}", re.UNICODE)


def _terms(text: str) -> list[str]:
    return [t for t in _TERM_RE.findall((text or "").lower())]


def _matcher(term: str) -> re.Pattern[str]:
    """Palabra completa (con plural). Con substring, "unity" matcheaba "community"
    y "excel" matcheaba "excellence", que enterraba los resultados utiles."""
    return re.compile(rf"\b{re.escape(term)}(?:s|es)?\b", re.IGNORECASE)


def _short(desc: str, limit: int = 150) -> str:
    desc = re.sub(r"\s+", " ", (desc or "")).strip()
    return desc if len(desc) <= limit else desc[: limit - 1].rstrip() + "…"


def search(query: str, cfg: dict | None = None, limit: int = 10) -> list[dict[str, Any]]:
    """Busca por nombre/descripcion/whenToUse/cuerpo. El nombre pesa mas que el texto."""
    q = (query or "").strip()
    limit = max(1, min(50, int(limit)))
    skills = _entries(cfg)
    if not q or not skills:
        return []
    terms = _terms(q)
    ql = q.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", ql).strip("-")
    rx_all = _matcher(ql)
    rx_terms = [_matcher(t) for t in terms]
    scored: list[tuple[float, dict[str, Any]]] = []
    for s in skills:
        name, desc, when, body = s.get("_search") or ("", "", "", "")
        score = 0.0
        if ql == name:
            score += 100.0
        elif slug and slug == name:
            score += 80.0
        elif ql in name and rx_all.search(name):
            score += 12.0
        matched = 0
        for t, rx in zip(terms, rx_terms):
            hit = False
            # Prefiltro substring (necesario pero no suficiente) antes del regex:
            # baja la busqueda de ~110 ms a ~25 ms con 1.900 skills.
            if t in name and rx.search(name):
                score += 8.0
                hit = True
            if t in when and rx.search(when):
                score += 3.0
                hit = True
            if t in desc and rx.search(desc):
                score += 2.0
                hit = True
            if not hit and t in body:
                n = len(rx.findall(body))
                if n:
                    # Peso por frecuencia: un SKILL.md que repite "Excel" 20 veces
                    # es mas relevante que uno que lo menciona al pasar.
                    score += 0.9 + 0.45 * min(8, n)
                    hit = True
            if hit:
                matched += 1
        # Cobertura: una skill que matchea todos los terminos vale mas que una
        # que solo roza uno (evita que "excel spreadsheet" devuelva cualquier cosa).
        if terms and score > 0:
            score *= 0.4 + 0.6 * (matched / len(terms))
        if score > 0:
            scored.append((score, s))
    scored.sort(key=lambda x: (-x[0], str(x[1].get("name") or "")))
    out = []
    for score, s in scored[:limit]:
        out.append({
            "name": s.get("name"),
            "description": _short(str(s.get("description") or ""), 220),
            "path": s.get("path"),
            "score": round(score, 2),
        })
    return out


def get_skill(name: str, cfg: dict | None = None) -> dict[str, Any] | None:
    """Devuelve la skill completa (frontmatter + cuerpo del SKILL.md)."""
    wanted = (name or "").strip().lower()
    if not wanted:
        return None
    skills = _entries(cfg)
    match = next((s for s in skills if str(s.get("name") or "").lower() == wanted), None)
    if match is None:
        match = next(
            (s for s in skills
             if Path(str(s.get("path") or "")).parent.name.lower() == wanted),
            None,
        )
    if match is None:
        return None
    path = Path(str(match.get("path") or ""))
    body = ""
    truncated = False
    try:
        from src.core.skill_frontmatter import parse_frontmatter_file

        parsed = parse_frontmatter_file(
            path, allowed_roots=[Path(str(match.get("root") or path.parent))]
        )
        body = str(parsed.get("body") or "")
    except Exception as e:
        logger.debug("no se pudo leer %s: %s", path, e)
        try:
            body = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            body = ""
    if len(body) > MAX_BODY_CHARS:
        body = body[:MAX_BODY_CHARS]
        truncated = True
    return {
        "name": match.get("name"),
        "description": _short(str(match.get("description") or ""), 400),
        "path": str(path),
        "dir": match.get("dir") or str(path.parent),
        "whenToUse": match.get("whenToUse") or "",
        "body": body,
        "truncated": truncated,
    }


def hint_block(task: str = "", cfg: dict | None = None, limit: int = 6) -> str:
    """Bloque corto para el system prompt.

    Con 1.700+ skills no entra la lista completa (ni de lejos): se muestran solo
    las que se parecen a la tarea y se le enseña al modelo a buscar el resto.
    """
    try:
        total = len(_entries(cfg))
    except Exception:
        return ""
    if not total:
        return ""
    lines: list[str] = []
    if task:
        hits = search(task, cfg, limit=limit)
        if hits:
            lines.append("SKILLS QUE PUEDEN SERVIR PARA ESTA TAREA (cargalas con skill(name=...)):")
            for h in hits:
                lines.append(f"- {h['name']}: {_short(h['description'] or '', HINT_DESC_CHARS)}")
    if not lines:
        lines.append(
            f"SKILLS: hay {total} skills instaladas. Antes de resolver algo especializado "
            f'(audio, video, PDF, SEO, diseño, pruebas, despliegue…), buscá con '
            f'skill(query="...") y cargá la elegida con skill(name="...").'
        )
    else:
        lines.append(f'(total instalado: {total}; skill(query="...") busca en todas)')
    text = "\n\n" + "\n".join(lines)
    if len(text) > MAX_HINT_CHARS:
        text = text[: MAX_HINT_CHARS - 1].rstrip() + "…"
    return text
