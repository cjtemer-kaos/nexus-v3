"""Guardián de portabilidad — impide regresiones de hardcodes (§13).

Reglas del proyecto: un copiado de esta carpeta a otra PC (distinto SO,
otros modelos Ollama) debe funcionar sin editar código. Este test escanea
src/ y config/ buscando violaciones conocidas:

- rutas de unidad (C:\\, D:/) fuera de allowlist
- IPs LAN hardcodeadas fuera de allowlist
- referencias al proyecto viejo (supernexus-v2)
- vínculos a ~/.nexus (los datos viven en ./data y ./brain)

Más tests unitarios de environment.py (adaptación al entorno).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
CONFIG_DIR = ROOT / "config"

# Archivos con patrones intencionales (no son vínculos de datos):
# - ai_defence.py:   regexes de defensa (C:\Windows\..., AppData)
# - ai_tools.py:     descripciones de tools para el LLM (ejemplos cross-plat)
# - web_fetch.py:    blocklist SSRF (IPs de metadata cloud)
# - webhook_manager.py: rangos privados 192.168.x/10.x para SSRF guard
ALLOWLIST = {
    "ai_defence.py",
    "ai_tools.py",
    "web_fetch.py",
    "webhook_manager.py",
}

# Ruta de unidad = UNA sola letra antes de ':' (descarta 'Context:', 'name:',
# 'Status:', 'http:' — todos multi-letra). '(?!/)' descarta 'x://'.
DRIVE_RE = re.compile(r"(?<![A-Za-z])[A-Za-z]:[\\/](?![/])")
LAN_RE = re.compile(r"\b192\.168\.\d{1,3}\.\d{1,3}\b|\b100\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")
V2_RE = re.compile(r"supernexus-v2")
NEXUS_HOME_RE = re.compile(
    r"""Path\.home\(\)\s*/\s*["']\.nexus"""
    r"""|expanduser\(\s*["']~/\.nexus"""
    r"""|expanduser\(\s*["']~["']\s*\)\s*/\s*["']\.nexus"""
    r"""|os\.path\.join\(\s*os\.path\.expanduser\(\s*["']~["']\s*\)\s*,\s*["']\.nexus["']"""
)


def _code_lines(text: str):
    """Lineas escaneables: sin comentarios de linea, sin cuerpo de docstring."""
    in_doc = False
    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if in_doc:
            if '"""' in line or "'''" in line:
                in_doc = False
            continue
        if not line or line.startswith("#"):
            continue
        m = re.search(r'''"""|''' + r"'''", line)
        if m:
            seq = m.group(0)
            rest = line[m.end():]
            if seq in rest:
                continue  # docstring/str de una sola linea -> contenido, no codigo
            in_doc = True
            line = line[:m.start()].strip()
            if not line:
                continue
        yield i, line


def _py_files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.py") if p.name not in ALLOWLIST)


def _yaml_files() -> list[Path]:
    if not CONFIG_DIR.exists():
        return []
    return sorted(p for p in CONFIG_DIR.rglob("*") if p.suffix in (".yaml", ".yml", ".json"))


def _scan(pattern: re.Pattern) -> list[str]:
    hits = []
    for f in _py_files() + _yaml_files():
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for i, line in _code_lines(text):
            if pattern.search(line):
                hits.append(f"{f.relative_to(ROOT)}:{i}: {line[:140]}")
    return hits


# --------------------------------------------------------------------------
# Escaneos de regresion
# --------------------------------------------------------------------------

def test_no_hardcoded_drive_paths():
    hits = _scan(DRIVE_RE)
    assert not hits, "rutas de unidad hardcodeadas:\n" + "\n".join(hits)


def test_no_hardcoded_lan_ips():
    hits = _scan(LAN_RE)
    assert not hits, "IPs LAN hardcodeadas:\n" + "\n".join(hits)


def test_no_v2_references():
    hits = _scan(V2_RE)
    assert not hits, "referencias al proyecto viejo (supernexus-v2):\n" + "\n".join(hits)


def test_no_nexus_home_links():
    hits = _scan(NEXUS_HOME_RE)
    assert not hits, "vinculos a ~/.nexus (usar data_home()/brain_path()):\n" + "\n".join(hits)


# --------------------------------------------------------------------------
# Locacion de datos: dentro del proyecto por defecto
# --------------------------------------------------------------------------

def test_data_home_inside_project(monkeypatch):
    from src.core.environment import data_home

    monkeypatch.delenv("NEXUS_V3_DATA", raising=False)
    dh = data_home().resolve()
    assert dh == (ROOT / "data").resolve()
    assert ROOT.resolve() in dh.parents


def test_brain_path_default_inside_project(monkeypatch):
    from src.brain.link import brain_path

    monkeypatch.delenv("NEXUS_V3_BRAIN", raising=False)
    bp = brain_path().resolve()
    assert bp == (ROOT / "brain").resolve()


# --------------------------------------------------------------------------
# Adaptacion al entorno (environment.py)
# --------------------------------------------------------------------------

def test_ollama_base_url_normalization(monkeypatch):
    from src.core.environment import ollama_base_url

    monkeypatch.setenv("OLLAMA_HOST", "127.0.0.1:9999")
    assert ollama_base_url() == "http://127.0.0.1:9999"
    monkeypatch.setenv("OLLAMA_HOST", "http://otro-host:11434/")
    assert ollama_base_url() == "http://otro-host:11434"
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    assert ollama_base_url() == "http://localhost:11434"


def test_family_match_exact_and_family():
    from src.core.environment import family_match

    avail = ["deepseek-r1:14b", "gemma4:latest"]
    assert family_match("deepseek-r1:14b", avail) == "deepseek-r1:14b"
    assert family_match("deepseek-r1:8b", avail) == "deepseek-r1:14b"
    assert family_match("gpt-4o", avail) is None


def test_adapt_models_passthrough_without_detection():
    from src.core.environment import adapt_models

    configured = ["a:1", "b:2"]
    registered, dropped = adapt_models(configured, [])
    assert registered == configured and dropped == []


def test_adapt_models_family_intersection():
    from src.core.environment import adapt_models

    registered, dropped = adapt_models(
        ["deepseek-r1:8b", "gemma4", "gpt-4o-mini"],
        ["deepseek-r1:14b", "gemma4:latest"],
    )
    assert registered == ["deepseek-r1:8b", "gemma4"]
    assert dropped == ["gpt-4o-mini"]


def test_adapt_models_adopts_environment_on_zero_match():
    from src.core.environment import adapt_models

    # PC nueva con otros modelos: nada de lo configurado existe aqui
    registered, dropped = adapt_models(
        ["nexus-director-v6", "qwen3.5-9b"],
        ["llama3.2:3b", "mistral:7b", "nomic-embed-text"],
    )
    assert registered == ["llama3.2:3b", "mistral:7b"]
    assert dropped == ["nexus-director-v6", "qwen3.5-9b"]


def test_adapt_models_adds_chat_when_only_embed_matches():
    from src.core.environment import adapt_models

    registered, _ = adapt_models(
        ["nomic-embed-text"],
        ["nomic-embed-text", "qwen2.5:7b"],
    )
    assert registered[0] == "qwen2.5:7b"  # chat primero
    assert "nomic-embed-text" in registered


def test_pick_embed_model():
    from src.core.environment import pick_embed_model

    assert pick_embed_model("nomic-embed-text", []) == "nomic-embed-text"
    assert pick_embed_model("nomic-embed-text", ["qwen2.5:7b", "bge-m3"]) == "bge-m3"
    assert pick_embed_model("nomic-embed-text", ["qwen2.5:7b"]) == "nomic-embed-text"


def test_pick_model_prefers_configured_families():
    from src.core.environment import pick_model

    assert pick_model([], prefer=("deepseek-r1",)) is None
    assert pick_model(
        ["gemma4:latest", "deepseek-r1:8b"], prefer=("deepseek-r1",)
    ) == "deepseek-r1:8b"
    # modelos embed se excluyen por defecto
    assert pick_model(["nomic-embed-text", "qwen2.5:7b"]) == "qwen2.5:7b"
