"""Ventana de contexto por modelo: cada modelo tiene la suya y el harness debe respetarla."""
from __future__ import annotations

from src.core import context_limits as cl
from src.core.agent_loop import AgentLoop
from src.core.iteration_budget import IterationBudget


CFG_16K = {"providers": {"ollama": {"options": {"num_ctx": 16384}}}}


def test_register_y_conocido():
    cl.register("modelo-de-prueba:latest", 4096)
    assert cl.cached("modelo-de-prueba") == 4096
    assert cl.known("modelo-de-prueba:latest") == 4096


def test_normaliza_el_sufijo_latest():
    assert cl.normalize("qwen3-vl-8b:latest") == "qwen3-vl-8b"
    assert cl.normalize("qwen3-vl-8b") == "qwen3-vl-8b"


def test_ventana_usable_es_el_minimo_entre_nativo_y_pedido():
    # Modelo chico (4k) con un num_ctx pedido de 16k: manda el modelo.
    cl.register("chico-4k", 4096)
    assert cl.usable_window("chico-4k", provider="ollama", cfg=CFG_16K) == 4096
    # Modelo grande con el mismo pedido: manda lo que pedimos (VRAM).
    cl.register("grande-128k", 131072)
    assert cl.usable_window("grande-128k", provider="ollama", cfg=CFG_16K) == 16384


def test_clamp_baja_num_ctx_al_limite_nativo():
    cl.register("chico-4k", 4096)
    assert cl.clamp_num_ctx("chico-4k", 16384) == 4096
    cl.register("grande-128k", 131072)
    assert cl.clamp_num_ctx("grande-128k", 16384) == 16384


def test_sin_dato_usa_el_num_ctx_configurado():
    # Un id que no esta en la tabla ni fue sondeado: manda lo que pedimos.
    assert cl.usable_window("modelo-desconocido-xyz", provider="ollama", cfg=CFG_16K) == 16384
    # Cloud sin dato: ventana conservadora, nunca 8192 de Ollama.
    assert cl.usable_window("modelo-cloud-xyz", provider="opencode-go", cfg={}) == cl.DEFAULT_CLOUD_CONTEXT


def test_limit_from_error_lee_los_formatos_que_usan_los_proveedores():
    # Ollama/llama.cpp/openai describen el limite de formas distintas.
    assert cl.limit_from_error("context_length 8192 exceeded") == 8192
    assert cl.limit_from_error("context_limit 300, got 5000 prompt too long") == 300
    assert cl.limit_from_error("the prompt is too long for context window 4096") == 4096
    assert cl.limit_from_error("max_tokens 32768 reached") == 32768
    # Sin numero no se inventa nada: el loop decide bajar a ciegas.
    assert cl.limit_from_error("the input length exceeds the context length") is None
    assert cl.limit_from_error("") is None


def test_el_loop_compacta_segun_el_modelo_no_segun_config():
    loop = AgentLoop(
        provider_router=None,
        tool_registry=None,
        context_max_tokens=100000,
        budget=IterationBudget(max_iterations=1),
    )
    # Sin dato del modelo: manda el presupuesto del loop.
    assert loop._effective_context_max_tokens("s1") == 100000
    # Con modelo de 8k: el presupuesto baja al del modelo (si no, Ollama da 400).
    loop._model_ctx_tokens = 8192
    assert loop._effective_context_max_tokens("s1") == 8192
    # Nunca por encima del presupuesto del loop.
    loop._model_ctx_tokens = 262144
    assert loop._effective_context_max_tokens("s1") == 100000
