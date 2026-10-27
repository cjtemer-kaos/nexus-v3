"""Tests del catalogo de skills (descubrimiento, busqueda y carga).

El catalogo tiene que comportarse como el de opencode: ver TODAS las skills del
proyecto (incluidas las anidadas y las que no cumplen el kebab-case) y no
depender de rutas fuera de nexus-v3.
"""

from __future__ import annotations

import pytest

from src.core import skills_catalog as sk


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


@pytest.fixture
def proyecto(tmp_path, monkeypatch):
    """Aisla el catalogo en un 'proyecto' temporal (sin las 1.900 reales)."""
    monkeypatch.setattr(sk, "_PROJECT_ROOT", tmp_path)
    sk.invalidate()
    yield tmp_path
    sk.invalidate()


def _cfg(*roots: str, defaults: bool = False) -> dict:
    return {"skills": {"roots": list(roots), "include_default_roots": defaults}}


def _skill_md(name: str, desc: str, body: str = "") -> str:
    return f"---\nname: {name}\ndescription: {desc}\n---\n\n{body or 'Cuerpo.'}\n"


def test_roots_fuera_del_proyecto_se_ignoran(proyecto, tmp_path_factory):
    afuera = tmp_path_factory.mktemp("ajeno")
    _write(afuera / "intrusa" / "SKILL.md", _skill_md("intrusa", "no debe verse"))
    adentro = proyecto / "data" / "skills"
    _write(adentro / "propia" / "SKILL.md", _skill_md("propia", "si debe verse"))

    roots = sk.skill_roots(_cfg(str(afuera), str(adentro)))
    assert [r.name for r in roots] == ["skills"]
    nombres = [s["name"] for s in sk.list_skills(_cfg(str(afuera), str(adentro)))]
    assert nombres == ["propia"]


def test_skill_sin_frontmatter_se_indexa(proyecto):
    base = proyecto / "data" / "skills"
    _write(base / "cruda" / "SKILL.md", "# Titulo\n\nHace cosas utiles de verdad.\n")
    skill = sk.list_skills(_cfg(str(base)))[0]
    assert skill["name"] == "cruda"
    assert skill["description"] == "Hace cosas utiles de verdad."


def test_nombre_title_case_se_acepta(proyecto):
    """El formato estricto exige kebab-case; el hub real trae Title Case."""
    base = proyecto / "data" / "skills"
    _write(base / "agency-x" / "SKILL.md", _skill_md("Anthropologist", "estudia culturas"))
    skill = sk.list_skills(_cfg(str(base)))[0]
    assert skill["name"] == "Anthropologist"
    assert sk.get_skill("anthropologist", _cfg(str(base)))["body"].strip() == "Cuerpo."


def test_skill_anidada_se_encuentra(proyecto):
    base = proyecto / "data" / "skills"
    _write(base / "grupo" / "sub" / "SKILL.md", _skill_md("sub-skill", "anidada a 3 niveles"))
    nombres = [s["name"] for s in sk.list_skills(_cfg(str(base)))]
    assert nombres == ["sub-skill"]


def test_node_modules_no_se_escanea(proyecto):
    base = proyecto / "data" / "skills"
    _write(base / "ok" / "SKILL.md", _skill_md("ok", "valida"))
    _write(base / "roto" / "node_modules" / "x" / "SKILL.md", _skill_md("basura", "no"))
    nombres = [s["name"] for s in sk.list_skills(_cfg(str(base)))]
    assert nombres == ["ok"]


def test_busqueda_es_palabra_completa(proyecto):
    base = proyecto / "data" / "skills"
    _write(base / "unity-architect" / "SKILL.md", _skill_md("unity-architect", "Unity experto"))
    _write(base / "community-builder" / "SKILL.md", _skill_md("community-builder", "reddit marketing"))
    hits = sk.search("unity", cfg=_cfg(str(base)))
    assert [h["name"] for h in hits] == ["unity-architect"]


def test_busqueda_prefiere_el_nombre(proyecto):
    base = proyecto / "data" / "skills"
    _write(base / "seo-audit" / "SKILL.md", _skill_md("seo-audit", "audita sitios"))
    _write(base / "otra" / "SKILL.md", _skill_md("otra", "audit y audit de nuevo"))
    hits = sk.search("seo audit", cfg=_cfg(str(base)))
    assert hits[0]["name"] == "seo-audit"
    # slug exacto (80) + los dos terminos en el nombre (8+8)
    assert hits[0]["score"] >= 90.0


def test_busqueda_vacia_o_desconocida(proyecto):
    base = proyecto / "data" / "skills"
    _write(base / "algo" / "SKILL.md", _skill_md("algo", "existe"))
    assert sk.search("", cfg=_cfg(str(base))) == []
    assert sk.search("kubernetes", cfg=_cfg(str(base))) == []


def test_get_skill_desconocida_devuelve_none(proyecto):
    base = proyecto / "data" / "skills"
    _write(base / "algo" / "SKILL.md", _skill_md("algo", "existe"))
    assert sk.get_skill("no-existe", _cfg(str(base))) is None
    assert sk.get_skill("", _cfg(str(base))) is None


def test_hint_block_acotado(proyecto):
    base = proyecto / "data" / "skills"
    for i in range(40):
        _write(base / f"skill-{i:02d}" / "SKILL.md",
               _skill_md(f"skill-{i:02d}", f"skill numero {i} para pruebas de video"))
    hint = sk.hint_block("crear un video", _cfg(str(base)))
    assert len(hint) <= sk.MAX_HINT_CHARS
    assert "skill(query=" in hint or "skill(name=" in hint
    assert "40" in hint  # el total instalado se informa


def test_hint_block_sin_skills_es_vacio(proyecto):
    assert sk.hint_block("cualquier cosa", _cfg()) == ""


def test_invalidate_ve_skills_nuevas(proyecto):
    base = proyecto / "data" / "skills"
    _write(base / "una" / "SKILL.md", _skill_md("una", "primera"))
    cfg = _cfg(str(base))
    assert [s["name"] for s in sk.list_skills(cfg)] == ["una"]
    _write(base / "dos" / "SKILL.md", _skill_md("dos", "segunda"))
    assert [s["name"] for s in sk.list_skills(cfg)] == ["una"]
    assert [s["name"] for s in sk.list_skills(cfg, refresh=True)] == ["dos", "una"]
    sk.invalidate()
    assert [s["name"] for s in sk.list_skills(cfg)] == ["dos", "una"]


def test_stats_reporta_roots_y_total(proyecto):
    base = proyecto / "data" / "skills"
    _write(base / "una" / "SKILL.md", _skill_md("una", "primera"))
    st = sk.stats(_cfg(str(base)))
    assert st["count"] == 1
    assert st["roots"] == [str(base)]


def test_duplicado_gana_el_root_de_mayor_prioridad(proyecto):
    a = proyecto / "no" / "skills"
    b = proyecto / "si" / "skills"
    _write(a / "dup" / "SKILL.md", _skill_md("dup", "version A"))
    _write(b / "dup" / "SKILL.md", _skill_md("dup", "version B"))
    skills = sk.list_skills(_cfg(str(a), str(b)))
    assert len(skills) == 1
    assert skills[0]["description"] == "version A"


def test_system_prompt_incluye_bloque_de_skills(proyecto):
    """El loop tiene que inyectar el bloque (sin esto el prompt era ciego a skills)."""
    import asyncio

    from src.core.agent_loop import AgentLoop
    from src.core.provider_base import LLMResponse
    from src.core.tool_registry import ToolRegistry

    base = proyecto / "data" / "skills"
    _write(base / "video-skill" / "SKILL.md", _skill_md("video-skill", "crea videos con remotion"))
    sk.invalidate()

    class StubRouter:
        def __init__(self):
            self.payload = None

        async def chat(self, payload, tools=None, hint=None, force_tools=False,
                       model=None, require_vision=False, **kw):
            self.payload = payload
            return LLMResponse(content="listo", finish_reason="stop")

        def resolve(self, *a, **k):
            return None

    router = StubRouter()
    loop = AgentLoop(provider_router=router, tool_registry=ToolRegistry(),
                     max_iterations=1, config={})
    asyncio.run(loop.run("crear un video", session_id="t-skills-prompt"))

    assert router.payload, "el loop no llamo al provider"
    system = "\n".join(
        (getattr(m, "content", None) or "")
        for m in router.payload if getattr(m, "role", "") == "system"
    )
    assert "video-skill" in system
    assert "skill(" in system

