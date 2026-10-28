"""El resumen de compactacion no puede costar el arranque del turno.

Medido en la corrida de eMule: el resumen pidio turno al modelo escalado, el
provider corto por `ReadTimeout` a los 65 s y todo ese tiempo se pago como
`setup` antes de la primera vuelta del agente. Con techo configurable, el
resumen se abandona y se usa L1-L3 (snip) en su lugar.
"""
from __future__ import annotations

import asyncio
import time

from src.core.compaction import CompactionPipeline


class _ProveedorLento:
    async def chat(self, messages, model=None, **kw):
        await asyncio.sleep(5)
        raise AssertionError("no deberia llegar aca: el techo tiene que cortar antes")


class _Ruta:
    def __init__(self):
        self.model = "modelo-lento"
        self.provider = _ProveedorLento()


class _Router:
    aux_model = "modelo-lento"

    def resolve(self, **kw):
        return _Ruta()


def _mensajes(n: int = 40) -> list[dict]:
    out = [{"role": "system", "content": "sos un agente"}]
    for i in range(n):
        out.append({"role": "user", "content": f"pedido {i} " + "x" * 400})
        out.append({"role": "assistant", "content": f"respuesta {i} " + "y" * 400})
    return out


def test_resumen_lento_se_corta_y_cae_a_snip():
    mgr = CompactionPipeline({"summary": {"timeout_s": 0.3, "failure_cooldown_s": 60}})
    t0 = time.monotonic()
    salida, metrics = asyncio.run(
        mgr.summarize_with_llm(_mensajes(), limit=1000, router=_Router()))
    transcurrido = time.monotonic() - t0
    assert transcurrido < 3, f"el techo no corto: {transcurrido:.1f}s"
    assert salida and len(salida) < len(_mensajes())
    detalles = [str(getattr(c, "detail", "")) for c in (metrics.layers or [])]
    assert not any("llm=" in d for d in detalles), f"no debio usar el LLM: {detalles}"
    # Cooldown armado: el siguiente resumen no vuelve a esperar al modelo lento.
    assert mgr._summary_failed_at > 0
