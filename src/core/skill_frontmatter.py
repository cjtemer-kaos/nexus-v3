"""Skill frontmatter — parse/validación/policy/merge (S3-pasos 3-5).

Formato upstream: ``SKILL.md`` con bloque YAML inicial delimitado por
``---``::

    ---
    name: mi-skill
    description: Qué hace y cuándo usarla.
    whenToUse: "opcional, triggers en lenguaje natural"
    metadata: {version: "1.0", author: "x"}   # opcional, dict libre
    ---
    <cuerpo markdown: instrucciones, patrones, conocimiento>

Reglas:
- Requeridos: ``name`` (kebab-case ``^[a-z0-9]+(-[a-z0-9]+)*$``) y
  ``description`` (string no vacío).
- Opcionales: ``whenToUse`` (string) y ``metadata`` (dict).
- Catálogo vs cuerpo separados: el frontmatter es el catálogo (qué
  indexar/buscar), el cuerpo NO se indexa, solo se carga con load_skill.
- Ranks por root: a igual ``name``, gana el root de menor rank
  (roots[0] = rank 0 = máxima prioridad). Ver :func:`merge_by_rank`.
- Fail-closed: frontmatter inválido => ``valid=False`` + ``errors``; el
  caller hace fallback al índice viejo (nunca se indexa basura).
- CRLF: se normaliza ``\\r\\n``/``\\r`` a ``\\n`` antes de parsear.
- Symlinks: se resuelven; roto o fuga fuera de ``allowed_roots`` =>
  inválido explícito (no se sigue a ciegas).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

try:
    import yaml  # PyYAML (ya es dependencia del loader de config)
except ImportError:  # pragma: no cover
    yaml = None  # type: ignore

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
REQUIRED_FIELDS = ("name", "description")

INVOCATION_POLICY_DOC = (
    "Orden de invocación: 1) mención explícita del name → 2) keywords de "
    "whenToUse → 3) keywords de description. Cargue el cuerpo SOLO tras "
    "seleccionar por catálogo (name/description/whenToUse)."
)


def normalize_newlines(text: str) -> str:
    """Normaliza CRLF/CR a LF (archivos editados en Windows)."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def split_frontmatter(text: str) -> Tuple[Optional[str], str, Optional[str]]:
    """Divide ``(fm_text|None, body, error|None)``.

    - Sin ``---`` inicial: ``(None, text, None)`` = "sin frontmatter"
      (el caller usa fallback al índice viejo).
    - Con ``---`` inicial pero sin cierre: error (inválido).
    """
    norm = normalize_newlines(text)
    lines = norm.split("\n")
    if not lines or lines[0].strip() != "---":
        return None, norm, None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            fm_text = "\n".join(lines[1:i])
            body = "\n".join(lines[i + 1 :])
            return fm_text, body, None
    return None, norm, "frontmatter sin cierre '---'"


def validate_frontmatter(fm: Any) -> List[str]:
    """Valida el dict parseado. Retorna lista de errores (vacía = válido)."""
    errors: List[str] = []
    if not isinstance(fm, dict):
        return ["frontmatter debe ser un mapping YAML"]
    name = fm.get("name")
    if not isinstance(name, str) or not name.strip():
        errors.append("campo requerido: 'name' (string no vacío)")
    elif not NAME_RE.match(name.strip()):
        errors.append(f"'name' debe ser kebab-case (recibido {name!r})")
    desc = fm.get("description")
    if not isinstance(desc, str) or not desc.strip():
        errors.append("campo requerido: 'description' (string no vacío)")
    if "whenToUse" in fm and fm["whenToUse"] is not None and not isinstance(
        fm["whenToUse"], str
    ):
        errors.append("'whenToUse' debe ser string")
    if "metadata" in fm and fm["metadata"] is not None and not isinstance(
        fm["metadata"], dict
    ):
        errors.append("'metadata' debe ser mapping")
    return errors


def parse_frontmatter_text(text: str, source: str = "<text>") -> Dict[str, Any]:
    """Parsea texto de SKILL.md. Nunca lanza (fail-closed en dict)."""
    _, body, split_err = split_frontmatter(text)
    if split_err:
        return {
            "valid": False, "errors": [split_err], "frontmatter": {},
            "body": body, "source": source, "has_frontmatter": True,
        }
    norm = normalize_newlines(text)
    if not norm.split("\n") or norm.split("\n")[0].strip() != "---":
        return {
            "valid": False, "errors": ["sin frontmatter"],
            "frontmatter": {}, "body": body, "source": source,
            "has_frontmatter": False,
        }
    fm_text, _, _ = split_frontmatter(text)
    assert fm_text is not None
    if yaml is None:  # pragma: no cover
        return {
            "valid": False, "errors": ["PyYAML no instalado"],
            "frontmatter": {}, "body": body, "source": source,
            "has_frontmatter": True,
        }
    try:
        fm = yaml.safe_load(fm_text) if fm_text.strip() else None
    except Exception as e:
        return {
            "valid": False, "errors": [f"YAML inválido: {e}"],
            "frontmatter": {}, "body": body, "source": source,
            "has_frontmatter": True,
        }
    errors = validate_frontmatter(fm)
    if errors:
        return {
            "valid": False, "errors": errors, "frontmatter": {},
            "body": body, "source": source, "has_frontmatter": True,
        }
    assert isinstance(fm, dict)
    clean = {
        "name": str(fm["name"]).strip(),
        "description": str(fm["description"]).strip(),
    }
    if fm.get("whenToUse") is not None:
        clean["whenToUse"] = str(fm["whenToUse"])
    if fm.get("metadata") is not None:
        clean["metadata"] = dict(fm["metadata"])
    return {
        "valid": True, "errors": [], "frontmatter": clean,
        "body": body, "source": source, "has_frontmatter": True,
    }


def parse_frontmatter_file(
    path: str | Path, allowed_roots: Optional[Sequence[str | Path]] = None
) -> Dict[str, Any]:
    """Lee y parsea un SKILL.md del disco (resuelve symlinks, normaliza CRLF).

    ``allowed_roots``: si se da, el path resuelto debe vivir dentro de
    alguna raíz (anti-escape de symlinks). Falla cerrado si no.
    """
    p = Path(path)
    is_link = p.is_symlink()
    try:
        resolved = p.resolve()
    except Exception as e:
        return {
            "valid": False, "errors": [f"no se pudo resolver: {e}"],
            "frontmatter": {}, "body": "", "source": str(p),
            "has_frontmatter": False, "is_symlink": is_link,
            "resolved": None,
        }
    if is_link and not resolved.exists():
        return {
            "valid": False, "errors": [f"symlink roto: {p}"],
            "frontmatter": {}, "body": "", "source": str(p),
            "has_frontmatter": False, "is_symlink": True,
            "resolved": str(resolved),
        }
    if allowed_roots:
        roots = [Path(r).expanduser().resolve() for r in allowed_roots]
        if not any(
            resolved == r or r in resolved.parents for r in roots
        ):
            return {
                "valid": False,
                "errors": [f"symlink/path fuera de roots permitidos: {resolved}"],
                "frontmatter": {}, "body": "", "source": str(p),
                "has_frontmatter": False, "is_symlink": is_link,
                "resolved": str(resolved),
            }
    try:
        text = resolved.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {
            "valid": False, "errors": [f"archivo no existe: {p}"],
            "frontmatter": {}, "body": "", "source": str(p),
            "has_frontmatter": False, "is_symlink": is_link,
            "resolved": str(resolved),
        }
    except Exception as e:
        return {
            "valid": False, "errors": [f"no se pudo leer: {e}"],
            "frontmatter": {}, "body": "", "source": str(p),
            "has_frontmatter": False, "is_symlink": is_link,
            "resolved": str(resolved),
        }
    out = parse_frontmatter_text(text, source=str(p))
    out["is_symlink"] = is_link
    out["resolved"] = str(resolved)
    return out


def invocation_policy(frontmatter: Dict[str, Any]) -> Dict[str, Any]:
    """Policy de invocación derivada del frontmatter (catálogo, no cuerpo).

    Orden: mención explícita de ``name`` > keywords de ``whenToUse`` >
    keywords de ``description``. El cuerpo solo se carga tras seleccionar.
    """
    name = str(frontmatter.get("name", ""))
    desc = str(frontmatter.get("description", ""))
    when = str(frontmatter.get("whenToUse", "") or "")

    def _keywords(s: str) -> List[str]:
        return [w.lower() for w in re.findall(r"[A-Za-z0-9]{4,}", s)]

    return {
        "name": name,
        "explicit_mention": name,
        "when_keywords": _keywords(when),
        "description_keywords": _keywords(desc)[:20],
        "rule": INVOCATION_POLICY_DOC,
    }


def merge_by_rank(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Merge por rank: cada entry trae ``rank`` (int, menor = más prioridad).

    A igual ``name``, gana el rank menor (roots[0] primero). Retorna lista
    ordenada por ``(rank, name)`` sin duplicados. No lanza con entries
    malformadas: las que no traen ``name`` se descartan.
    """
    best: Dict[str, Dict[str, Any]] = {}
    for e in entries:
        name = e.get("name")
        if not isinstance(name, str) or not name:
            continue
        rank = e.get("rank", 0)
        try:
            rank = int(rank)
        except Exception:
            rank = 0
        prev = best.get(name)
        if prev is None or rank < int(prev.get("rank", 0)):
            best[name] = {**e, "rank": rank}
    return sorted(best.values(), key=lambda d: (int(d.get("rank", 0)), d["name"]))


def scan_roots(
    roots: Sequence[str | Path],
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Escanea roots buscando ``*/SKILL.md`` con frontmatter válido.

    Retorna ``(skills, errors)``. ``skills``: entries con
    ``{name, description, whenToUse?, metadata?, path, root, rank}`` listas
    para :func:`merge_by_rank`. ``errors``: parseos inválidos con detalle
    (nunca tumban el scan).
    """
    skills: List[Dict[str, Any]] = []
    errors: List[Dict[str, Any]] = []
    for rank, root in enumerate(roots):
        rp = Path(root).expanduser()
        if not rp.is_dir():
            continue
        try:
            resolved_root = rp.resolve()
        except Exception:
            continue
        for skill_md in sorted(rp.glob("*/SKILL.md")):
            parsed = parse_frontmatter_file(
                skill_md, allowed_roots=[resolved_root]
            )
            if not parsed["valid"]:
                errors.append(
                    {"path": str(skill_md), "root": str(rp),
                     "rank": rank, "errors": parsed["errors"]}
                )
                continue
            fm = parsed["frontmatter"]
            entry: Dict[str, Any] = {
                "name": fm["name"], "description": fm["description"],
                "path": str(skill_md), "root": str(rp), "rank": rank,
            }
            if "whenToUse" in fm:
                entry["whenToUse"] = fm["whenToUse"]
            if "metadata" in fm:
                entry["metadata"] = fm["metadata"]
            skills.append(entry)
    return skills, errors
