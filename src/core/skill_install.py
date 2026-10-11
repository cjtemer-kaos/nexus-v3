"""Instalación de skills con gate held-out SkillOpt (S3-pasos 3-5).

Flujo: validar SKILL.md (frontmatter real) → propose en SkillOptimizer →
validate contra held-out → SOLO si accepted se copia a ``data/skills/``.

Conexión con Fase 9 (igual que
``tests/test_phase9_learning.py::test_skillopt_gate_heldout``)::

    sk = SkillOptimizer(state_path=..., heldout_path=...)
    sk.propose("instala skill X", {"skill": name}, change_id="install-x")
    res = await sk.validate("install-x", evaluator)  # sync o async, igual que SkillOpt
    # accepted → instalar | rejected → NO instalar (fail-closed)

Si no se pasa ``evaluator``, la instalación queda PENDIENTE (hook
documentado, ver :data:`SKILLOPT_HOOK_DOC`): se hace ``propose`` y se
retorna ``status=pending_gate`` sin copiar nada. El caller debe re-llamar
con el evaluator del pipeline real cuando lo tenga.
"""
from __future__ import annotations

import asyncio
import inspect
import shutil
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from src.core.environment import data_home, workspace_root
from src.core.skill_frontmatter import parse_frontmatter_file

SKILLOPT_HOOK_DOC = """HOOK SkillOpt (gate held-out, paridad Fase 9):
1) sk = SkillOptimizer(state_path=<tmp|data>/skill_opt_state.json,
                       heldout_path=<tmp|data>/heldout.json)
2) sk.propose(f"instala skill {name}", {"skill": name}, change_id=...)
3) res = await sk.validate(change_id, evaluator, required_score=1.0)
   - evaluator(case)->bool|awaitable, igual que en
     tests/test_phase9_learning.py::test_skillopt_gate_heldout y
     ::test_skillopt_validate_async_evaluator_y_heldout (sync o async).
   - accepted (score>=required) -> install_skill() copia a data/skills/.
   - rejected -> NO se instala (fail-closed).
Sin evaluator: install_skill() deja el cambio en 'proposed' y retorna
status='pending_gate' + este hook. Re-llame con evaluator para cerrar el gate.
"""

DEFAULT_SKILLS_DIR = "data/skills"


def default_skills_dir() -> Path:
    return workspace_root() / DEFAULT_SKILLS_DIR


def validate_skill_dir(
    source: str | Path,
    allowed_roots: Optional[list] = None,
    require_name_match: bool = True,
) -> Dict[str, Any]:
    """Valida un directorio de skill. Retorna ``{ok, errors, frontmatter}``.

    Chequeos: es directorio, contiene SKILL.md, frontmatter válido
    (name kebab-case + description), y —si ``require_name_match``— el
    ``name`` coincide con el nombre del directorio.
    """
    src = Path(source)
    errors: list[str] = []
    if not src.exists() or not src.is_dir():
        return {"ok": False, "errors": [f"no es directorio: {src}"],
                "frontmatter": {}, "name": None}
    md = src / "SKILL.md"
    if not md.exists():
        return {"ok": False, "errors": ["falta SKILL.md"],
                "frontmatter": {}, "name": None}
    parsed = parse_frontmatter_file(md, allowed_roots=allowed_roots)
    if not parsed["valid"]:
        return {"ok": False, "errors": list(parsed["errors"]),
                "frontmatter": {}, "name": None}
    fm = parsed["frontmatter"]
    if require_name_match and fm["name"] != src.name:
        errors.append(
            f"'name' ({fm['name']!r}) != nombre del directorio ({src.name!r})"
        )
    if errors:
        return {"ok": False, "errors": errors, "frontmatter": fm,
                "name": fm["name"]}
    return {"ok": True, "errors": [], "frontmatter": fm, "name": fm["name"]}


def _get_skill_opt(skill_opt: Any = None) -> Any:
    if skill_opt is not None:
        return skill_opt
    from src.core.skill_opt import SkillOptimizer

    return SkillOptimizer()


async def install_skill(
    source: str | Path,
    skills_dir: Optional[str | Path] = None,
    skill_opt: Any = None,
    evaluator: Optional[Callable[[Dict], Any]] = None,
    required_score: float = 1.0,
    change_id: Optional[str] = None,
    overwrite: bool = False,
) -> Dict[str, Any]:
    """Valida + gate SkillOpt + instala en ``skills_dir/<name>/``.

    - Sin ``evaluator``: propose + retorna ``pending_gate`` (hook
      documentado, no copia nada).
    - Con ``evaluator``: ``propose`` + ``await validate`` (sync/async igual
      que Fase 9); solo ``accepted`` copia los archivos.
    """
    validation = validate_skill_dir(source)
    if not validation["ok"]:
        return {"status": "invalid", "errors": validation["errors"],
                "installed": False}
    name = validation["name"]
    dest_root = Path(skills_dir) if skills_dir else default_skills_dir()
    dest = dest_root / name
    if dest.exists() and not overwrite:
        return {"status": "exists", "errors": [f"ya existe: {dest}"],
                "installed": False, "name": name}

    sk = _get_skill_opt(skill_opt)
    cid = change_id or f"install-{name}-{uuid.uuid4().hex[:8]}"
    if cid in getattr(sk, "_state", {}).get("changes", {}):
        cid = f"{cid}-{uuid.uuid4().hex[:4]}"
    sk.propose(f"instala skill {name}",
               {"skill": name, "source": str(source)}, change_id=cid)

    if evaluator is None:
        return {"status": "pending_gate", "change_id": cid, "name": name,
                "installed": False, "hook": SKILLOPT_HOOK_DOC,
                "hint": "re-llama install_skill() con evaluator para cerrar el gate"}

    res = sk.validate(cid, evaluator, required_score=required_score)
    if inspect.isawaitable(res):
        res = await res
    if not isinstance(res, dict) or res.get("status") != "accepted":
        return {"status": "rejected", "change_id": cid, "name": name,
                "installed": False, "gate": res}
    src = Path(source)
    dest_root.mkdir(parents=True, exist_ok=True)
    if dest.exists() and overwrite:
        shutil.rmtree(dest)
    shutil.copytree(src, dest)
    return {"status": "installed", "change_id": cid, "name": name,
            "installed": True, "dest": str(dest), "gate": res,
            "frontmatter": validation["frontmatter"],
            "installed_at": time.time()}


def install_skill_sync(*args: Any, **kwargs: Any) -> Dict[str, Any]:
    """Wrapper sync para callers/tests sin loop (ejecuta el async)."""
    return asyncio.new_event_loop().run_until_complete(
        install_skill(*args, **kwargs)
    )
