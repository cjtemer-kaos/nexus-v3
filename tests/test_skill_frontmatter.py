"""Tests S3-pasos 3-5: frontmatter skills + install + reminder tools.

Cubre:
- parse válido / inválido / CRLF / symlink / sin-frontmatter (fallback).
- policy de invocación + merge por rank.
- SkillGateway: fallback al índice viejo + override con frontmatter real.
- skill_install: gate SkillOpt fail-closed (paridad
  tests/test_phase9_learning.py::test_skillopt_*) + hook pendiente.
- reminder_create/list/delete (prefijo reminder_*, sin colisión con
  schedule_*/cron_*) + config reminders/skills + schema válido.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.core.skill_frontmatter import (  # noqa: E402
    invocation_policy,
    merge_by_rank,
    parse_frontmatter_file,
    parse_frontmatter_text,
    scan_roots,
)

VALID_MD = """---
name: mi-skill
description: Hace cosas útiles con datos.
whenToUse: "cuando el usuario pida procesar datos"
metadata:
  version: "1.0"
---
# Mi Skill
Cuerpo con instrucciones.
"""

NO_FM = "# Sin frontmatter\nSolo cuerpo.\n"


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


# -- parse ---------------------------------------------------------------

def test_parse_valido():
    out = parse_frontmatter_text(VALID_MD, source="t")
    assert out["valid"] is True and out["errors"] == []
    assert out["frontmatter"]["name"] == "mi-skill"
    assert "datos" in out["frontmatter"]["description"]
    assert out["frontmatter"]["whenToUse"].startswith("cuando")
    assert out["frontmatter"]["metadata"] == {"version": "1.0"}
    assert "Mi Skill" in out["body"]  # catálogo vs cuerpo separados


def test_parse_invalido_falta_description():
    out = parse_frontmatter_text("---\nname: ok-name\n---\nbody\n")
    assert out["valid"] is False
    assert any("description" in e for e in out["errors"])


def test_parse_invalido_nombre_no_kebab():
    for bad in ("Mi_Skill", "UPPER", "con espacios", "-lider", ""):
        out = parse_frontmatter_text(
            f"---\nname: {bad}\ndescription: d\n---\nbody\n"
        )
        assert out["valid"] is False, bad


def test_parse_invalido_sin_cierre_y_yaml_roto():
    out = parse_frontmatter_text("---\nname: x\nsin cierre")
    assert out["valid"] is False
    out2 = parse_frontmatter_text("---\nname: [no cerrado\ndescription: d\n---\n")
    assert out2["valid"] is False


def test_parse_crlf(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_bytes(VALID_MD.replace("\n", "\r\n").encode("utf-8"))
    out = parse_frontmatter_file(p)
    assert out["valid"] is True
    assert out["frontmatter"]["name"] == "mi-skill"


def test_parse_sin_frontmatter_es_fallback(tmp_path):
    p = tmp_path / "SKILL.md"
    p.write_text(NO_FM, encoding="utf-8")
    out = parse_frontmatter_file(p)
    assert out["valid"] is False
    assert out["has_frontmatter"] is False  # el gateway usa índice viejo


def test_parse_symlink_ok_y_roto(tmp_path):
    real = tmp_path / "real" / "SKILL.md"
    real.parent.mkdir()
    real.write_text(VALID_MD, encoding="utf-8")
    link = tmp_path / "SKILL.md"
    try:
        link.symlink_to(real)
    except OSError:
        pytest.skip("sin permiso de symlinks en este entorno")
    out = parse_frontmatter_file(link, allowed_roots=[tmp_path])
    assert out["valid"] is True and out["is_symlink"] is True
    # symlink roto
    broken = tmp_path / "broken.md"
    try:
        broken.symlink_to(tmp_path / "no-existe.md")
    except OSError:
        pytest.skip("sin permiso de symlinks en este entorno")
    out2 = parse_frontmatter_file(broken, allowed_roots=[tmp_path])
    assert out2["valid"] is False
    assert any("roto" in e for e in out2["errors"])


def test_parse_symlink_escape_rechazado(tmp_path):
    outside = tmp_path / "outside" / "SKILL.md"
    outside.parent.mkdir()
    outside.write_text(VALID_MD, encoding="utf-8")
    link = tmp_path / "root" / "SKILL.md"
    link.parent.mkdir()
    try:
        link.symlink_to(outside)
    except OSError:
        pytest.skip("sin permiso de symlinks en este entorno")
    out = parse_frontmatter_file(link, allowed_roots=[link.parent])
    assert out["valid"] is False
    assert any("fuera de roots" in e for e in out["errors"])


# -- policy + merge -------------------------------------------------------

def test_invocation_policy_orden():
    pol = invocation_policy(
        {"name": "mi-skill", "description": "Procesa datos crudos",
         "whenToUse": "cuando pidan procesar datos"}
    )
    assert pol["explicit_mention"] == "mi-skill"
    assert "procesar" in pol["when_keywords"]
    assert "rule" in pol and "name" in pol["rule"].lower() or "mención" in pol["rule"]


def test_merge_by_rank_gana_root_0(tmp_path):
    entries = [
        {"name": "dup", "description": "del root 1", "rank": 1},
        {"name": "dup", "description": "del root 0", "rank": 0},
        {"name": "otra", "description": "x", "rank": 1},
    ]
    merged = merge_by_rank(entries)
    assert [e["name"] for e in merged] == ["dup", "otra"]
    assert next(e for e in merged if e["name"] == "dup")["description"] == "del root 0"


def test_scan_roots_con_rank_y_errores(tmp_path):
    r0 = tmp_path / "r0" / "mi-skill"
    r0.mkdir(parents=True)
    (r0 / "SKILL.md").write_text(VALID_MD, encoding="utf-8")
    r1 = tmp_path / "r1" / "mi-skill"
    r1.mkdir(parents=True)
    (r1 / "SKILL.md").write_text(
        VALID_MD.replace("cosas útiles", "otra variante"), encoding="utf-8"
    )
    bad = tmp_path / "r1" / "mala"
    bad.mkdir()
    (bad / "SKILL.md").write_text("---\nname: Mala_Nombre\n---\n", encoding="utf-8")
    skills, errors = scan_roots([tmp_path / "r0", tmp_path / "r1"])
    merged = merge_by_rank(skills)
    assert next(s for s in merged if s["name"] == "mi-skill")["rank"] == 0
    assert any("mala" in e["path"] for e in errors)


# -- gateway ---------------------------------------------------------------

def test_gateway_fallback_sin_frontmatter(tmp_path):
    from src.tools.skill_gateway import SkillGateway

    gw = SkillGateway(skills_root=str(tmp_path), roots=[str(tmp_path)])
    assert gw.skills_cache == []  # índice viejo inexistente + sin SKILL.md
    assert gw.search_skills("x") == []
    assert "error" in gw.get_skill_info("x")


def test_gateway_lee_frontmatter_real(tmp_path):
    from src.tools.skill_gateway import SkillGateway

    d = tmp_path / "roots" / "mi-skill"
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(VALID_MD, encoding="utf-8")
    gw = SkillGateway(skills_root=str(tmp_path), roots=[str(tmp_path / "roots")])
    hits = gw.search_skills("útiles")
    assert [h["name"] for h in hits] == ["mi-skill"]
    info = gw.get_skill_info("mi-skill")
    assert info["frontmatter"]["name"] == "mi-skill"
    assert "Mi Skill" in info["body_preview"]


# -- install + gate SkillOpt -------------------------------------------------

GOOD_MD = VALID_MD.replace("mi-skill", "nueva-skill")


def _skill_src(tmp_path, name="nueva-skill", body=GOOD_MD):
    d = tmp_path / "src" / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(body, encoding="utf-8")
    return d


def test_install_aceptado_por_gate(tmp_path):
    from src.core.skill_install import install_skill_sync
    from src.core.skill_opt import SkillOptimizer

    src = _skill_src(tmp_path)
    sk = SkillOptimizer(state_path=tmp_path / "opt.json",
                        heldout_path=tmp_path / "held.json")
    res = install_skill_sync(src, skills_dir=tmp_path / "skills", skill_opt=sk,
                             evaluator=lambda case: True)
    assert res["status"] == "installed" and res["installed"] is True
    assert (tmp_path / "skills" / "nueva-skill" / "SKILL.md").exists()
    assert sk.get(res["change_id"])["status"] == "accepted"


def test_install_rechazado_no_copia(tmp_path):
    from src.core.skill_install import install_skill_sync
    from src.core.skill_opt import SkillOptimizer

    src = _skill_src(tmp_path)
    sk = SkillOptimizer(state_path=tmp_path / "opt.json",
                        heldout_path=tmp_path / "held.json")
    res = install_skill_sync(src, skills_dir=tmp_path / "skills", skill_opt=sk,
                             evaluator=lambda case: False)
    assert res["status"] == "rejected" and res["installed"] is False
    assert not (tmp_path / "skills" / "nueva-skill").exists()


def test_install_sin_evaluator_deja_hook_pendiente(tmp_path):
    from src.core.skill_install import SKILLOPT_HOOK_DOC, install_skill_sync
    from src.core.skill_opt import SkillOptimizer

    src = _skill_src(tmp_path)
    sk = SkillOptimizer(state_path=tmp_path / "opt.json",
                        heldout_path=tmp_path / "held.json")
    res = install_skill_sync(src, skills_dir=tmp_path / "skills", skill_opt=sk)
    assert res["status"] == "pending_gate"
    assert res["installed"] is False
    assert "validate" in res["hook"] and "held-out" in res["hook"]
    assert "test_skillopt" in SKILLOPT_HOOK_DOC  # hook documentado con paridad Fase 9
    assert not (tmp_path / "skills" / "nueva-skill").exists()
    # el change quedó proposed: se puede cerrar luego con evaluator real
    res2 = install_skill_sync(src, skills_dir=tmp_path / "skills", skill_opt=sk,
                              evaluator=lambda case: True,
                              change_id=res["change_id"] + "-bis")
    assert res2["status"] == "installed"


def test_install_invalido_no_pasa_gate(tmp_path):
    from src.core.skill_install import install_skill_sync

    src = _skill_src(tmp_path, body="---\nname: Mala\n---\n")
    res = install_skill_sync(src, skills_dir=tmp_path / "skills",
                             evaluator=lambda case: True)
    assert res["status"] == "invalid" and res["installed"] is False


# -- reminder tools + config -------------------------------------------------

REMINDER_SESSION = "test-reminders-s3"


@pytest.fixture()
def isolated_reminders(tmp_path, monkeypatch):
    monkeypatch.setattr("src.core.session_reminders.data_home",
                        lambda: tmp_path / "data")
    import src.bridges.mcp_bridge_server as bridge

    return bridge


def test_reminder_create_list_delete(isolated_reminders):
    bridge = isolated_reminders
    created = json.loads(
        _run(bridge.reminder_create(REMINDER_SESSION, "toma agua",
                                    kind="after", delay_s=60)))
    assert created.get("created") is True
    rid = created["reminder"]["id"]
    listed = json.loads(_run(bridge.reminder_list(REMINDER_SESSION)))
    assert listed["count"] == 1
    assert listed["statuses"][rid] == "scheduled"
    deleted = json.loads(_run(bridge.reminder_delete(REMINDER_SESSION, rid)))
    assert deleted == {"removed": True, "reminder_id": rid,
                       "session_id": REMINDER_SESSION}
    listed2 = json.loads(_run(bridge.reminder_list(REMINDER_SESSION)))
    assert listed2["count"] == 0


def test_reminder_every_respeta_minimo(isolated_reminders):
    bridge = isolated_reminders
    bad = json.loads(
        _run(bridge.reminder_create(REMINDER_SESSION, "tick",
                                    kind="every", interval_s=60)))
    assert "error" in bad and "300" in bad["error"]
    ok = json.loads(
        _run(bridge.reminder_create(REMINDER_SESSION, "tick",
                                    kind="every", interval_s=300)))
    assert ok.get("created") is True


def test_reminder_max_active(isolated_reminders, monkeypatch):
    import src.bridges.mcp_bridge_server as bridge

    monkeypatch.setattr(bridge, "_reminder_config",
                        lambda: {"min_every_s": 300, "max_active": 1})
    first = json.loads(
        _run(bridge.reminder_create(REMINDER_SESSION, "uno", kind="after",
                                    delay_s=60)))
    assert first.get("created") is True
    second = json.loads(
        _run(bridge.reminder_create(REMINDER_SESSION, "dos", kind="after",
                                    delay_s=60)))
    assert "error" in second and "max_active" in second["error"]


def test_reminder_sin_colision_con_schedule(tmp_path):
    import src.bridges.mcp_bridge_server as bridge

    names = {n for n in dir(bridge) if n.startswith(("reminder_", "schedule_", "cron_"))}
    assert {"reminder_create", "reminder_list", "reminder_delete"} <= names
    # prefijo reminder_* no pisa ni duplica schedule_*/cron_* existentes
    assert not (set(dir(bridge)) & {"schedule_create", "schedule_list", "schedule_delete"})


def test_config_reminders_y_skills_y_schema():
    from src.core.config import load_config, validate_config

    cfg = load_config()
    assert cfg["reminders"] == {"min_every_s": 300, "max_active": 50}
    assert cfg["skills"] == {"roots": ["./data/skills"], "watch": False}
    assert validate_config(cfg) == []
