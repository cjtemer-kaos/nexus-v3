"""Upgrades de harness incorporados desde la autopsia de repos (2026-10-19).

Cubre: deny duro de secretos, compactación que preserva pares tool/result,
spill de salidas grandes, jobs en background, apply_patch atómico, MMR en RAG,
persistencia del modo plan, subconjunto de tools por sub-agente y memoria
automática por turno vía router.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from src.core.compaction import CompactionPipeline
from src.core.scope import Scope, ScopeConfig, ScopeDenied
from src.core.spill import clip_output
from src.core.rag import _mmr_rerank

ROOT = Path(__file__).resolve().parent.parent


def _scope(deny=(), permission="full", roots=("..",)):
    return Scope(ScopeConfig(allowed_roots=roots, deny_patterns=deny, permission=permission),
                 project_root=ROOT)


# --- 1. Secretos: la config no puede quitar el deny duro --------------------
def test_deny_duro_no_se_anula_con_full():
    scope = _scope(deny=(), permission="full")
    for rel in (".ssh/id_rsa", ".aws/credentials", ".env", "deploy/service.pem"):
        with pytest.raises(ScopeDenied):
            scope.check_path(Path.home() / rel if rel.startswith(".") else ROOT / rel)


def test_deny_config_se_suma_al_duro():
    scope = _scope(deny=("**/secreto-custom.txt",))
    with pytest.raises(ScopeDenied):
        scope.check_path(ROOT / "secreto-custom.txt")
    with pytest.raises(ScopeDenied):
        scope.check_path(ROOT / ".ssh" / "id_rsa")


# --- 2. Compactación: no romper pares tool_use / tool_result ----------------
def test_snip_no_deja_tool_huerfano():
    pipe = CompactionPipeline({"snip": {"keep_recent": 3}})
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "pedido"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "1"}]},
        {"role": "tool", "content": "resultado", "tool_call_id": "1"},
        {"role": "assistant", "content": "sigo"},
        {"role": "user", "content": "dale"},
    ]
    out = pipe._snip(msgs)
    sin_sistema = [m for m in out if m["role"] != "system"]
    assert sin_sistema[0]["role"] == "assistant"
    assert sin_sistema[0].get("tool_calls"), "el assistant del tool_call debe viajar con su resultado"


def test_micro_conserva_los_ultimos_resultados():
    pipe = CompactionPipeline({"micro": {"keep_recent": 2}, "budget": {"max_tool_result_chars": 100}})
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(5):
        msgs.append({"role": "tool", "content": f"{i}-" + "x" * 500, "tool_call_id": str(i)})
    out = pipe._micro(msgs)
    tools = [m for m in out if m["role"] == "tool"]
    assert tools[-1]["content"].startswith("4-")
    assert tools[-2]["content"].startswith("3-")
    assert all(m["content"] == pipe.micro_placeholder for m in tools[:3])


# --- 3. Spill: cabeza + cola + archivo recuperable -------------------------
def test_clip_output_guarda_el_texto_completo(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_DATA", str(tmp_path))
    texto = "INICIO\n" + ("linea\n" * 5000) + "ERROR FINAL"
    out = clip_output(texto, session_id="s1", tool="exec", call_id="c1", max_inline=900)
    assert out.startswith("INICIO")
    assert out.endswith("ERROR FINAL")
    assert "salida completa en" in out
    saved = list((tmp_path / "spill").rglob("*.txt"))
    assert saved and saved[0].read_text(encoding="utf-8") == texto


# --- 4. Jobs en background -------------------------------------------------
def test_job_start_read_y_wait(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_DATA", str(tmp_path))
    from src.core.jobs import JobRegistry
    reg = JobRegistry()
    job = reg.start(f'"{sys.executable}" -c "print(\'hola-job\')"', tmp_path, label="demo")
    out = reg.wait(job["id"], timeout_s=30)
    assert "hola-job" in out["output"]
    assert out["state"] == "done"
    assert reg.get(job["id"])["state"] == "done"


def test_job_kill(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_DATA", str(tmp_path))
    from src.core.jobs import JobRegistry
    reg = JobRegistry()
    job = reg.start(f'"{sys.executable}" -c "import time; time.sleep(60)"', tmp_path)
    killed = reg.kill(job["id"])
    assert killed["state"] != "running"
    with pytest.raises(KeyError):
        reg.read("job-inexistente")


# --- 5. apply_patch atómico -------------------------------------------------
def _registry(tmp_path, monkeypatch):
    monkeypatch.setenv("NEXUS_V3_DATA", str(tmp_path / "data"))
    from src.api.server import _build_registry
    scope = Scope(ScopeConfig(allowed_roots=(str(tmp_path),), permission="full"), project_root=tmp_path)
    return _build_registry(scope, "test-apply-patch", cfg={})


def test_apply_patch_dry_run_y_aplicacion(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch)
    f = tmp_path / "a.txt"
    f.write_text("uno\ndos\ntres\n", encoding="utf-8")
    patch = ("*** Begin Patch\n*** Update File: " + str(f) + "\n@@\n uno\n-dos\n+DOS\n tres\n"
             "*** End Patch\n")
    handler = reg.get("apply_patch").handler
    import json
    assert json.loads(handler(patch=patch, dry_run=True, confirm=True))["dry_run"] is True
    assert f.read_text(encoding="utf-8") == "uno\ndos\ntres\n"
    handler(patch=patch, confirm=True)
    assert f.read_text(encoding="utf-8") == "uno\nDOS\ntres\n"


def test_apply_patch_es_atomico_si_un_hunk_falla(tmp_path, monkeypatch):
    reg = _registry(tmp_path, monkeypatch)
    good = tmp_path / "b.txt"
    good.write_text("alfa\nbeta\n", encoding="utf-8")
    bad = tmp_path / "c.txt"
    bad.write_text("gamma\n", encoding="utf-8")
    patch = ("*** Begin Patch\n"
             f"*** Update File: {good}\n@@\n-alfa\n+ALFA\n"
             f"*** Update File: {bad}\n@@\n-no-existe\n+NADA\n"
             "*** End Patch\n")
    with pytest.raises(ValueError):
        reg.get("apply_patch").handler(patch=patch, confirm=True)
    assert good.read_text(encoding="utf-8") == "alfa\nbeta\n"
    assert bad.read_text(encoding="utf-8") == "gamma\n"


# --- 6. MMR diversifica el top-k -------------------------------------------
def test_mmr_diversifica_resultados_casi_iguales():
    scored = [(0.9, "a", "misma frase repetida sobre el tema") for _ in range(4)]
    scored.append((0.8, "b", "contenido completamente distinto de otro archivo"))
    top = _mmr_rerank(scored, limit=2)
    fuentes = {src for _, src, _ in top}
    assert "b" in fuentes


# --- 7. Modo plan persistente ----------------------------------------------
def test_modo_plan_sobrevive_al_reinicio(tmp_path, monkeypatch):
    import src.api.server as srv
    monkeypatch.setattr(srv, "_MODE_FILE", tmp_path / "session_modes.json")
    srv._SESSION_MODES.clear()
    srv._SESSION_MODES["s1"] = "plan"
    srv._save_session_modes()
    assert srv._load_session_modes() == {"s1": "plan"}
    srv._SESSION_MODES.pop("s1", None)
    srv._save_session_modes()
    assert srv._load_session_modes() == {}


# --- 8. Higiene: backup consistente, retención y limpieza -------------------
def test_hygiene_backup_retencion_y_workspace(tmp_path, monkeypatch):
    import importlib.util
    import os
    import sqlite3

    spec = importlib.util.spec_from_file_location("hygiene", ROOT / "scripts" / "hygiene.py")
    h = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(h)

    brain = tmp_path / "brain"
    workspace = tmp_path / "workspace"
    backups = tmp_path / "data" / "backups" / "brain"
    brain.mkdir()
    (workspace / "__pycache__").mkdir(parents=True)
    (workspace / "-p").mkdir()
    (workspace / "real.txt").write_text("no tocar", encoding="utf-8")

    db = brain / "knowledge_graph.db"
    with sqlite3.connect(db) as c:
        c.execute("create table t (x text)")
        c.execute("insert into t values ('dato')")
    # Un backup viejo de la misma familia (debe quedar 1 tras keep=1).
    legacy = brain / "knowledge_graph.db.bak-viejo"
    legacy.write_bytes(b"viejo")
    os.utime(legacy, (1, 1))

    for name, value in {"ROOT": tmp_path, "BRAIN": brain, "BACKUPS": backups,
                        "LEGACY": backups / "legacy", "WORKSPACE": workspace,
                        "VENDOR": tmp_path / "vendor" / "llama.cpp"}.items():
        monkeypatch.setattr(h, name, value)

    report = h.run(apply=True, keep=1)
    assert all(b["ok"] for b in report["backups"])
    created = list(backups.glob("knowledge_graph-*.db"))
    assert created, "debe crear un backup consistente"
    with sqlite3.connect(created[0]) as c:
        assert c.execute("select x from t").fetchone()[0] == "dato"
    assert not (backups / "legacy" / legacy.name).exists(), "retención keep=1 debe borrar el viejo"
    assert not (workspace / "__pycache__").exists()
    assert not (workspace / "-p").exists()
    assert (workspace / "real.txt").exists()


# --- 12. Sub-agentes: subconjunto de tools transportado por el spawner ------
def test_registry_subset_acota_tools():
    from src.core.tool_registry import ToolRegistry

    reg = ToolRegistry()

    async def _noop(**kwargs):
        return "ok"

    params = {"type": "object", "properties": {}}
    reg.register_function(_noop, name="read_file", description="", parameters=params)
    reg.register_function(_noop, name="write_file", description="", parameters=params)

    sub = reg.subset(["read_file", "no_existe"])
    assert sub.count() == 1
    assert sub.has("read_file") and not sub.has("write_file")
    assert reg.count() == 2, "el registro original no se modifica"


def test_spawner_transporta_allowed_tools():
    import asyncio

    from src.core.sub_agent_spawner import SubAgentSpec, SubAgentSpawner

    seen = {}

    async def _exec(task, gem="auto", context="", allowed_tools=()):
        seen["tools"] = tuple(allowed_tools)
        return "ok"

    sp = SubAgentSpawner(executor=_exec)
    spec = SubAgentSpec(task="leer", gem="scholar", allowed_tools=("read_file", "web_search"))
    res = asyncio.run(sp.spawn_child(spec))
    assert res.status.value == "completed"
    assert seen["tools"] == ("read_file", "web_search")


def test_spawner_compatible_con_executor_viejo():
    import asyncio

    from src.core.sub_agent_spawner import SubAgentSpec, SubAgentSpawner

    seen = {}

    def _exec(task, gem="auto", context=""):
        seen["called"] = True
        return "ok"

    sp = SubAgentSpawner(executor=_exec)
    res = asyncio.run(sp.spawn_child(SubAgentSpec(task="x", allowed_tools=("read_file",))))
    assert res.status.value == "completed"
    assert seen["called"] is True


# --- 13. Memoria automática: el router del harness extrae y persiste --------
def test_memory_extractor_usa_router(tmp_path, monkeypatch):
    import asyncio
    import json

    import src.core.memory_extractor as me

    monkeypatch.setattr(me, "brain_path", lambda: tmp_path)
    calls = {"n": 0}

    class _Resp:
        content = '[{"text": "El usuario trabaja en nexus-v3 sobre Windows 11", "category": "fact"}]'

    class _Router:
        async def chat(self, messages, **kwargs):
            calls["n"] += 1
            assert kwargs.get("hint") == "simple"
            return _Resp()

    ex = me.MemoryExtractor(router=_Router())
    stats = asyncio.run(ex.after_response(
        [
            {"role": "user", "content": "Trabajo en nexus-v3 sobre Windows 11, acordate"},
            {"role": "assistant", "content": "Anotado."},
        ],
        session_id="s-test",
        max_facts=3,
    ))
    assert calls["n"] == 1, "debe consultar al router, no solo al fallback regex"
    assert stats["added"] >= 1
    data = json.loads((tmp_path / "memories.json").read_text(encoding="utf-8"))
    assert any("nexus-v3" in e.get("text", "") for e in data)


