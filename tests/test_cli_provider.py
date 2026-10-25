"""CLIProvider: parseo de cada CLI, errores, timeout y registro en el router.

No ejecuta `claude`, `codex` ni `opencode` reales: usa un script que imita su
salida (incluidos sus errores), asi la suite no depende de logins ni de cuotas.
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import pytest

from src.core.provider_base import LLMMessage
from src.providers.cli_provider import (
    CLIProvider,
    CLIProviderError,
    check_base_url,
    flatten_messages,
    resolve_command,
    trim_prompt,
)

FAKE = r'''
import json, os, sys, time
flavor = sys.argv[1]
args = sys.argv[2:]
mode = os.environ.get("FAKE_MODE", "ok")

if flavor == "claude":
    if mode == "hang":
        time.sleep(60)
    err = mode == "error"
    print(json.dumps({
        "type": "result", "subtype": "success", "is_error": err,
        "result": ("Failed to authenticate. API Error: 401 OAuth access token is invalid."
                   if err else "ok desde claude"),
        "usage": {"input_tokens": 11, "output_tokens": 5},
        "total_cost_usd": 0.002, "duration_ms": 2500,
    }))
elif flavor == "codex":
    out_file = args[args.index("-o") + 1]
    print(json.dumps({"type": "thread.started", "thread_id": "t1"}))
    if mode == "error":
        print(json.dumps({"type": "turn.failed",
                          "error": {"message": "model_not_found: gpt-5.5"}}))
    else:
        with open(out_file, "w", encoding="utf-8") as fh:
            fh.write("ok desde codex")
        print(json.dumps({"type": "turn.completed",
                          "usage": {"input_tokens": 7, "output_tokens": 3}}))
elif flavor == "opencode":
    print(json.dumps({"type": "step_start", "part": {"type": "step-start"}}))
    print(json.dumps({"type": "text", "part": {"type": "text", "text": "ok desde "}}))
    print(json.dumps({"type": "text", "part": {"type": "text", "text": "opencode"}}))
    print(json.dumps({"type": "step_finish", "part": {"type": "step-finish",
                          "tokens": {"input": 9, "output": 4}, "cost": 0.001}}))
elif flavor == "custom":
    print("hola " + args[0] if args else "hola")
elif flavor == "slow":
    time.sleep(60)
sys.stdout.flush()
'''


@pytest.fixture()
def fake(tmp_path):
    """Fabrica providers apuntando al script falso."""
    script = tmp_path / "fake_cli.py"
    script.write_text(FAKE, encoding="utf-8")

    def make(name="fake", flavor="claude", mode="ok", **spec):
        os.environ["FAKE_MODE"] = mode
        base = {"flavor": flavor, "timeout_s": 20, "cooldown_s": 0,
                "command": [sys.executable, str(script), flavor],
                "cwd": str(tmp_path / "work"),
                # Esta PC tiene ANTHROPIC_BASE_URL apuntando a un proxy muerto:
                # el fake no hereda esa variable salvo que el test lo pida.
                "drop_env": ["ANTHROPIC_BASE_URL"]}
        base.update(spec)
        return CLIProvider(name, base)

    return make


def _ask(provider, text="Responde solo: ok", **kw):
    return asyncio.run(provider.chat([LLMMessage(role="user", content=text)], **kw))


# ---------------------------------------------------------------------------
# Parseo por CLI
# ---------------------------------------------------------------------------
def test_claude_parsea_result_y_usage(fake):
    r = _ask(fake(flavor="claude"))
    assert r.content == "ok desde claude"
    assert r.usage["prompt_tokens"] == 11
    assert r.usage["completion_tokens"] == 5
    assert r.usage["total_tokens"] == 16
    assert r.usage["cost_usd"] == 0.002
    assert r.usage["provider"] == "fake"


def test_claude_is_error_se_reporta_con_su_mensaje(fake):
    with pytest.raises(CLIProviderError) as err:
        _ask(fake(flavor="claude", mode="error"))
    assert "401" in str(err.value)
    assert "OAuth access token is invalid" in str(err.value)


def test_codex_lee_el_archivo_de_ultimo_mensaje(fake):
    r = _ask(fake(flavor="codex"))
    assert r.content == "ok desde codex"
    assert r.usage["prompt_tokens"] == 7
    assert r.usage["completion_tokens"] == 3


def test_codex_turn_failed_es_error_no_respuesta_vacia(fake):
    with pytest.raises(CLIProviderError) as err:
        _ask(fake(flavor="codex", mode="error"))
    assert "model_not_found" in str(err.value)


def test_opencode_concatena_las_partes_de_texto(fake):
    r = _ask(fake(flavor="opencode"))
    assert r.content == "ok desde opencode"
    assert r.usage["prompt_tokens"] == 9
    assert r.usage["completion_tokens"] == 4


def test_flavor_custom_devuelve_stdout_crudo(fake):
    script = fake(flavor="custom")
    script.opts["argv"] = ["{prompt}"]
    r = _ask(script, text="mundo")
    assert r.content.startswith('hola "mundo"') or r.content.startswith("hola mundo")


# ---------------------------------------------------------------------------
# Robustez
# ---------------------------------------------------------------------------
def test_timeout_mata_el_proceso_y_avisa(fake):
    p = fake(flavor="claude", mode="hang", timeout_s=2)
    t0 = time.monotonic()
    with pytest.raises(CLIProviderError) as err:
        _ask(p)
    elapsed = time.monotonic() - t0
    assert "no termino en 2s" in str(err.value)
    assert elapsed < 25, f"tardo {elapsed:.1f}s: el timeout no corto"


def test_enfriamiento_evita_reintentar_un_cli_roto(fake):
    p = fake(flavor="claude", mode="error", cooldown_s=300)
    with pytest.raises(CLIProviderError):
        _ask(p)
    assert p.cooling_down is True
    assert asyncio.run(p.health()) is False
    t0 = time.monotonic()
    with pytest.raises(CLIProviderError) as err:
        _ask(p)
    assert "enfriamiento" in str(err.value)
    assert time.monotonic() - t0 < 5, "el segundo intento volvio a lanzar el proceso"


def test_health_falso_si_el_binario_no_existe(fake):
    p = fake(flavor="claude", command=["binario-que-no-existe-xyz"])
    assert asyncio.run(p.health()) is False
    with pytest.raises(CLIProviderError) as err:
        _ask(p)
    assert "no pude ejecutar" in str(err.value)


def test_rechaza_turnos_con_tools(fake):
    p = fake(flavor="claude")
    with pytest.raises(CLIProviderError) as err:
        _ask(p, tools=[{"type": "function", "function": {"name": "write_file"}}])
    assert "no soporta tool-calling" in str(err.value)


def test_supports_tools_y_vision_en_false(fake):
    p = fake(flavor="claude")
    assert asyncio.run(p.supports_tools("x")) is False
    assert asyncio.run(p.supports_vision("x")) is False


# ---------------------------------------------------------------------------
# Preflight del base_url (el cuelgue de claude -p)
# ---------------------------------------------------------------------------
def test_check_base_url_detecta_puerto_muerto():
    assert check_base_url("") == ""
    assert check_base_url("http://127.0.0.1:1") != ""
    assert "no responde" in check_base_url("http://127.0.0.1:1")


def test_preflight_corta_antes_de_lanzar_el_proceso(fake, monkeypatch):
    p = fake(flavor="claude", env={"ANTHROPIC_BASE_URL": "http://127.0.0.1:1"})
    called = {"count": 0}

    async def _no_deberia(*a, **k):
        called["count"] += 1
        raise AssertionError("se lanzo el CLI con el proxy caido")

    monkeypatch.setattr("src.providers.cli_provider.run_cli", _no_deberia)
    t0 = time.monotonic()
    with pytest.raises(CLIProviderError) as err:
        _ask(p)
    assert "no responde" in str(err.value)
    assert called["count"] == 0
    assert time.monotonic() - t0 < 5


def test_claude_ignora_el_preflight_si_se_pide_dropear_la_variable(fake):
    p = fake(flavor="claude", drop_env=["ANTHROPIC_BASE_URL"])
    os.environ["ANTHROPIC_BASE_URL"] = "http://127.0.0.1:1"
    try:
        r = _ask(p)
        assert r.content == "ok desde claude"
    finally:
        os.environ.pop("ANTHROPIC_BASE_URL", None)
# ---------------------------------------------------------------------------
# Utilidades
# ---------------------------------------------------------------------------
def test_flatten_usa_el_ultimo_pedido_y_resume_lo_previo():
    system, user = flatten_messages([
        LLMMessage(role="system", content="sos conciso"),
        LLMMessage(role="user", content="hola"),
        LLMMessage(role="assistant", content="hola, que necesitas"),
        LLMMessage(role="user", content="ahora si: medí esto"),
    ])
    assert system == "sos conciso"
    assert "Pedido actual:\nahora si: medí esto" in user
    assert "respuesta previa" in user


def test_flatten_sin_mensajes_no_rompe():
    assert flatten_messages([]) == ("", "")
    assert flatten_messages([LLMMessage(role="user", content="  ")]) == ("", "")


def test_trim_prompt_conserva_principio_y_final():
    texto = "PRINCIPIO " + ("x" * 40000) + " FINAL"
    out = trim_prompt(texto, limit=2000)
    assert len(out) <= 2000
    assert out.startswith("PRINCIPIO")
    assert out.endswith("FINAL")
    assert "recortado" in out


def test_model_ids_y_normalizacion_del_modelo(fake):
    sin_modelo = fake(flavor="claude")
    assert sin_modelo.model_ids() == ["fake"]
    assert sin_modelo._model_arg(None) == ""
    assert sin_modelo._model_arg("otro-modelo") == "otro-modelo"

    con_modelo = fake(flavor="claude", model="claude-sonnet-4-5")
    assert con_modelo.model_ids() == ["fake:claude-sonnet-4-5"]
    assert con_modelo._model_arg(None) == "claude-sonnet-4-5"
    assert con_modelo._model_arg("fake") == "claude-sonnet-4-5"
    assert con_modelo._model_arg("fake:claude-haiku") == "claude-haiku"


def test_cwd_por_defecto_es_un_directorio_descartable(tmp_path):
    p = CLIProvider("opencode-test", {"flavor": "opencode"})
    assert p.cwd.exists()
    assert "nexus-v3-cli" in str(p.cwd)
    assert "nexus-v3\\src" not in str(p.cwd)


def test_cwd_explicito_se_respeta(fake, tmp_path):
    destino = tmp_path / "proyecto"
    p = fake(flavor="opencode", cwd=str(destino))
    assert p.cwd == destino
    assert destino.exists()


def test_flavor_desconocido_falla_al_construir():
    with pytest.raises(CLIProviderError) as err:
        CLIProvider("raro", {"flavor": "no-existe"})
    assert "desconocido" in str(err.value)


# ---------------------------------------------------------------------------
# Resolucion del comando (shims de npm en Windows)
# ---------------------------------------------------------------------------
def test_resolve_command_acepta_lista():
    assert resolve_command(["python", "x.py"]) == ["python", "x.py"]
    with pytest.raises(CLIProviderError):
        resolve_command([])


def test_resolve_command_falla_con_mensaje_claro():
    with pytest.raises(CLIProviderError) as err:
        resolve_command("binario-inexistente-abc-123")
    assert "no encontre" in str(err.value)


def test_resolve_command_lee_el_shim_cmd_y_llega_al_exe(tmp_path):
    npm = tmp_path / "npm"
    target = npm / "node_modules" / "paquete" / "bin" / "app.exe"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"")
    shim = npm / "cosa.cmd"
    shim.write_text(
        '@ECHO off\n"%dp0%\\node_modules\\paquete\\bin\\app.exe" %*\n', encoding="utf-8")
    assert resolve_command(str(shim)) == [str(target)]


def test_resolve_command_usa_node_para_el_shim_js(tmp_path):
    npm = tmp_path / "npm"
    js = npm / "node_modules" / "paquete" / "bin" / "cli.js"
    js.parent.mkdir(parents=True)
    js.write_text("console.log(1)\n", encoding="utf-8")
    shim = npm / "cosa.cmd"
    shim.write_text('"%dp0%\\node_modules\\paquete\\bin\\cli.js" %*\n', encoding="utf-8")
    resolved = resolve_command(str(shim))
    assert resolved[-1] == str(js)
    assert len(resolved) == 2


# ---------------------------------------------------------------------------
# Registro en el router (apagado por defecto)
# ---------------------------------------------------------------------------
def test_router_no_registra_cli_apagado(monkeypatch):
    from src.api.server import _build_router_from_config

    cfg = {"providers": {"ollama": {"base_url": "http://127.0.0.1:1", "models": []},
                         "cli": {"opencode": {"enabled": False, "flavor": "opencode"}}},
           "router": {"vram_budget_gb": 12, "fallback_order": ["ollama"]}}
    router = _build_router_from_config(cfg)
    assert "opencode" not in router.providers()


def test_router_registra_cli_habilitado_con_su_modelo(fake, tmp_path):
    from src.api.server import _build_router_from_config

    p = fake(flavor="claude", model="claude-sonnet-4-5")
    cfg = {"providers": {"ollama": {"base_url": "http://127.0.0.1:1", "models": []}},
           "router": {"vram_budget_gb": 12, "fallback_order": ["ollama"]}}
    cfg["providers"]["cli"] = {
        "claude": {"enabled": True, "flavor": "claude", "model": "claude-sonnet-4-5",
                   "command": p._command, "cwd": str(tmp_path / "work"), "cooldown_s": 0}}

    router = _build_router_from_config(cfg)
    assert "claude" in router.providers()
    ids = [r.id for r in router.list_models() if r.provider_name == "claude"]
    assert ids == ["claude:claude-sonnet-4-5"]


def test_router_no_se_rompe_si_el_cli_configurado_no_existe():
    from src.api.server import _build_router_from_config

    cfg = {"providers": {"ollama": {"base_url": "http://127.0.0.1:1", "models": []},
                         "cli": {"fantasma": {"enabled": True, "flavor": "custom"}}},
           "router": {"vram_budget_gb": 12, "fallback_order": ["ollama"]}}
    router = _build_router_from_config(cfg)
    assert "fantasma" not in router.providers()

