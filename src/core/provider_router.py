"""Provider Router — tier / VRAM / Thompson routing + auto-detect + fallback chat.

Origen: SuperNEXUS provider_router (HINT_MAPPING + RoutedProvider) + vram_router + adaptive_router (Thompson).
Ver BLUEPRINT.md §10.

Features:
- HINT_MAPPING tier routing (simple=1 … deep=10)
- Fallback chain: try each provider in fallback_order until chat succeeds
- Auto-detect: probe health + list_models, adapt to :8080 vs :8000
- Chat with fallback: router.chat(messages, tools, hint) tries providers sequentially
- VRAM-aware stub (reserves Director 4.3GB, checks vram_budget_gb)
- Thompson stub (weights by fallback_order; real sampling if stats exist)
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Any

from .provider_base import BaseProvider, LLMMessage, LLMResponse

logger = logging.getLogger("nexus-router")

HINT_MAPPING: dict[str, int] = {
    "none": 0, "simple": 1, "categorization": 2, "coding": 3,
    "analysis": 4, "reasoning": 5, "research": 6,
    "planning": 7, "architecture": 7, "vision": 8, "creative": 9, "deep": 10,
}

# Mapa hint -> gema DirectorNexus -> modelo preferido (sobre el tier).
# Prioridad en resolve(): preferred_model > GEMA_MAPPING > tier.
# Evita que tareas generativas (creative/design) caigan en el director-orquestador
# (nexus-director-v6) que se niega a ejecutar y delega; esas van a qwen3.5-9b.
GEMA_MAPPING: dict[str, dict[str, str]] = {
    "none":          {"gema": "opencode",  "model": "qwen3.5-9b"},
    "simple":        {"gema": "analyst",   "model": "nemotron-3-nano-4b"},
    "categorization": {"gema": "analyst",  "model": "nemotron-3-nano-4b"},
    "coding":        {"gema": "code",      "model": "qwen3.5-9b"},
    "analysis":      {"gema": "analyst",   "model": "nemotron-3-nano-4b"},
    "reasoning":     {"gema": "debugger",  "model": "deepseek-r1-8b"},
    "research":      {"gema": "scholar",   "model": "deepseek-r1-8b"},
    "planning":      {"gema": "director",  "model": "nexus-director-v6"},
    "architecture":  {"gema": "architect", "model": "qwen3.5-9b"},
    "vision":        {"gema": "vision",    "model": "qwen3-vl-8b"},
    "creative":      {"gema": "creative",  "model": "qwen3.5-9b"},
    "design":        {"gema": "design",    "model": "qwen3.5-9b"},
    "music":         {"gema": "music",     "model": "qwen3.5-9b"},
    "deep":          {"gema": "director",  "model": "deepseek-r1-8b"},
    "debug":         {"gema": "debugger",  "model": "deepseek-r1-8b"},
    "test":          {"gema": "tester",    "model": "qwen3.5-9b"},
    "security":      {"gema": "security",  "model": "deepseek-r1-8b"},
    "devops":        {"gema": "devops",    "model": "qwen3.5-9b"},
    "train":         {"gema": "trainer",   "model": "qwen3.5-9b"},
    "prompt":        {"gema": "prompter",  "model": "qwen3.5-9b"},
}


def infer_hint(text: str | None) -> str:
    """Infiere un hint a partir del texto de la tarea (palabras clave).

    Se usa cuando la UI no envía hint explícito: enruta a la gema correcta.
    """
    t = (text or "").lower()
    checks = [
        ("creative", ["diseño", "diseno", "design", "landing", "landing page", "ui", "ux",
                      "frontend", "estilo", "color", "maqueta", "prototipo", "página web",
                      "pagina web", "brand", "logo", "creativo", "eslogan", "campaña", "campana",
                      "historia", "poema", "guión", "guion", "video", "imagen promocional"]),
        ("vision", ["imagen", "imágen", "foto", "screenshot", "captura de pantalla", "ocr",
                    "visión", "vision", "ve esta", "analiza esta imagen", "describe la imagen"]),
        ("debug", ["bug", "error", "debug", "falla", "no funciona", "stacktrace", "traceback",
                   "crash", "se rompe", "excepción", "excepcion", "pantalla azul"]),
        ("coding", ["código", "codigo", "codear", "implementa", "programa", "función", "funcion",
                    "api", "script", "refactor", "python", "javascript", "typescript", "react",
                    "componente", "clase", "método", "metodo", "consulta sql", "bash", "powershell"]),
        ("research", ["investiga", "research", "busca", "documenta", "fuentes", "búsqueda",
                      "busqueda", "verifica datos", "referencias", "cita", "informe", "resume esto"]),
        ("reasoning", ["razona", "razonamiento", "por qué", "porque", "explica", "analiza profundamente",
                       "piensa", "deduce", "infiere", "comparación", "comparacion", "pros y contras"]),
        ("planning", ["arquitectura", "arquitect", "sistema", "plan", "planifica", "roadmap",
                      "estrategia", "hoja de ruta", "diseño de sistema", "diseno de sistema",
                      "organiza el proyecto", "fases"]),
        ("categorization", ["clasifica", "categoriza", "ordena", "lista", "resumen", "resumen rápido",
                            "organiza", "agrupa", "tabla", "etiqueta"]),
        ("test", ["test", "prueba", "testing", "qa", "verifica", "validar", "comprueba"]),
    ]
    for hint, kws in checks:
        if any(kw in t for kw in kws):
            return hint
    return "none"

# Model tier hints (fallback if route.tier==0)
MODEL_TIER_OVERRIDES: dict[str, int] = {
    "qwen2.5:0.5b": 1, "qwen2.5-coder:7b": 3, "deepseek-r1:8b": 5,
    "deepseek-r1-8b": 5, "qwen3.5:9b": 4, "qwen3-vl-4b": 8, "qwen3-vl-8b": 8,
    "omnicoder-2-9b": 3, "nemotron-3-nano-4b": 1, "nexus-director-v6": 7,
    "nemotron-3-ultra-free": 5, "nemotron-3.5-lightning-free": 4,
    "ling-3.0-flash-fin-free": 4, "nomic-embed-text": 1,
}


@dataclass(frozen=True, slots=True)
class ModelRoute:
    id: str
    provider_name: str
    context_window: int = 32000
    tier: int = 0  # 0=auto from HINT_MAPPING/MODEL_TIER_OVERRIDES
    vram_gb: float = 0.0  # estimated

    def effective_tier(self) -> int:
        if self.tier:
            return self.tier
        return MODEL_TIER_OVERRIDES.get(self.id, 3)


@dataclass(frozen=True, slots=True)
class RoutedProvider:
    provider: BaseProvider
    model: str
    route: ModelRoute


class ProviderRouter:
    """Tier-aware router with fallback and auto-detect.

    Example:
        router = ProviderRouter(vram_budget_gb=12, fallback_order=["ollama","cloud"])
        router.register_provider("ollama", OllamaProvider())
        router.register_model(ModelRoute(id="qwen2.5-coder:7b", provider_name="ollama", tier=3))
        routed = router.resolve(task_hint="coding")
        resp = await router.chat(messages, hint="coding")
    """

    def __init__(self, vram_budget_gb: float = 12, fallback_order: list[str] | None = None, strategy: str = "tier"):
        self._providers: dict[str, BaseProvider] = {}
        self._models: list[ModelRoute] = []
        self.vram_budget_gb = vram_budget_gb
        self.fallback_order = fallback_order or ["ollama", "cloud"]
        self.strategy = strategy  # thompson|vram|tier|manual
        # Thompson stats stub: {provider_name: {"success": int, "fail": int}}
        self._stats: dict[str, dict[str, int]] = {}

    # ---- registration ----
    def register_provider(self, name: str, p: BaseProvider) -> None:
        self._providers[name] = p
        self._stats.setdefault(name, {"success": 1, "fail": 1})  # Beta(1,1) prior

    def register_model(self, route: ModelRoute) -> None:
        self._models.append(route)

    def register_models(self, routes: list[ModelRoute]) -> None:
        for r in routes:
            self.register_model(r)

    def get_provider(self, name: str) -> Optional[BaseProvider]:
        return self._providers.get(name)

    def list_models(self, min_tier: int = 0) -> list[ModelRoute]:
        return [m for m in self._models if m.effective_tier() >= min_tier]

    def providers(self) -> dict[str, BaseProvider]:
        return dict(self._providers)

    # ---- resolve ----
    def resolve(
        self,
        task_hint: str | None = None,
        preferred_model: str | None = None,
        prefer_speed: bool = False,
        strategy: str | None = None,
    ) -> Optional[RoutedProvider]:
        """Tier >= hint_complexity; then order by fallback_order / strategy.

        Strategy:
          tier     — fallback_order priority
          vram     — prefer lower VRAM within tier
          thompson — sample Beta(success,fail) per provider
          manual   — same as tier but requires explicit hint
        """
        strat = strategy or self.strategy

        # Preferred model direct hit
        if preferred_model:
            for m in self._models:
                if m.id == preferred_model and m.provider_name in self._providers:
                    return RoutedProvider(provider=self._providers[m.provider_name], model=m.id, route=m)
            # Model not in registry but provider may still serve it (e.g., cloud)
            # Try to find provider that claims to have it via fallback_order
            for pname in self.fallback_order:
                if pname in self._providers:
                    # Return a synthetic route
                    route = ModelRoute(id=preferred_model, provider_name=pname, tier=HINT_MAPPING.get(task_hint or "none", 0))
                    return RoutedProvider(provider=self._providers[pname], model=preferred_model, route=route)
            return None

        hint = (task_hint or "none").lower()
        hint_complexity = HINT_MAPPING.get(hint, 0)

        # Gema override: si el hint tiene un modelo preferido en GEMA_MAPPING y está
        # registrado, usarlo directamente (evita que tareas generativas caigan en el
        # director-orquestador o en el modelo más débil).
        gema = GEMA_MAPPING.get(hint)
        if gema:
            gmodel = gema.get("model")
            for m in self._models:
                if m.id == gmodel and m.provider_name in self._providers:
                    return RoutedProvider(provider=self._providers[m.provider_name], model=m.id, route=m)

        candidates = [m for m in self._models if m.effective_tier() >= hint_complexity]
        relaxed = False
        if not candidates:
            # Ninguno alcanza el tier: usar el MÁS fuerte disponible (no el más chico),
            # excluyendo visión salvo que la pista sea vision.
            pool = list(self._models)
            if hint != "vision":
                pool = [m for m in pool if "vl-" not in m.id and "janus" not in m.id and "vision" not in m.id] or pool
            # Preferir un modelo general bueno (qwen3.5-9b) antes que el director-orquestador.
            for m in pool:
                if m.id == "qwen3.5-9b" and m.provider_name in self._providers:
                    return RoutedProvider(provider=self._providers[m.provider_name], model=m.id, route=m)
            candidates = sorted(pool, key=lambda x: -x.effective_tier())
            relaxed = True
        if not candidates:
            # No models registered but providers exist — create synthetic candidates
            for pname in self.fallback_order:
                if pname in self._providers:
                    return RoutedProvider(
                        provider=self._providers[pname],
                        model="default",
                        route=ModelRoute(id="default", provider_name=pname, tier=hint_complexity),
                    )
            return None

        # Strategy-specific ordering
        if strat == "vram":
            # Prefer lower VRAM within tier (cheaper), then fallback_order
            candidates.sort(key=lambda m: (m.vram_gb or 0, self._order_index(m.provider_name), -m.effective_tier()))
            for m in candidates:
                if m.provider_name in self._providers:
                    return RoutedProvider(provider=self._providers[m.provider_name], model=m.id, route=m)
        elif strat == "thompson":
            # Sample Beta for each candidate's provider, pick max sample
            best: ModelRoute | None = None
            best_score = -1
            # Group candidates by provider
            by_provider: dict[str, list[ModelRoute]] = {}
            for m in candidates:
                by_provider.setdefault(m.provider_name, []).append(m)
            for pname, routes in by_provider.items():
                if pname not in self._providers:
                    continue
                s = self._stats.get(pname, {"success": 1, "fail": 1})
                # Beta sampling via Gamma approximation
                try:
                    score = random.betavariate(s["success"], s["fail"])
                except Exception:
                    score = s["success"] / (s["success"] + s["fail"])
                # Prefer higher tier match as tie-breaker (use first route's tier proximity)
                tier_bonus = 0.01 * (10 - abs(routes[0].effective_tier() - hint_complexity))
                score += tier_bonus
                if score > best_score:
                    best_score = score
                    # Pick best route for that provider (closest tier >= hint)
                    best_route = min(routes, key=lambda r: (r.effective_tier() - hint_complexity) if r.effective_tier() >= hint_complexity else 100)
                    best = best_route
            if best is not None:
                return RoutedProvider(provider=self._providers[best.provider_name], model=best.id, route=best)
            # Fallback to tier
            strat = "tier"

        if strat in ("tier", "manual", "thompson"):
            # Prefer provider per fallback_order
            for pname in self.fallback_order:
                ordered = sorted(candidates, key=lambda x: x.effective_tier(), reverse=relaxed)
                for m in ordered:
                    if m.provider_name == pname and pname in self._providers:
                        return RoutedProvider(provider=self._providers[pname], model=m.id, route=m)
            # Any remaining candidate
            for m in candidates:
                if m.provider_name in self._providers:
                    return RoutedProvider(provider=self._providers[m.provider_name], model=m.id, route=m)

        return None

    def _order_index(self, provider_name: str) -> int:
        try:
            return self.fallback_order.index(provider_name)
        except ValueError:
            return 999

    # ---- chat with fallback ----
    async def chat(
        self,
        messages: list[LLMMessage],
        tools: list[dict] | None = None,
        model: str | None = None,
        hint: str | None = None,
        **kw: Any,
    ) -> LLMResponse:
        """Try providers in fallback_order until one succeeds. Records Thompson stats."""
        attempts = self._attempts(model, hint, kw.pop("prefer_speed", False))

        last_err: Exception | None = None
        for pname, m_id in attempts:
            provider = self._providers.get(pname)
            if not provider:
                continue
            try:
                resp = await provider.chat(messages, tools=tools, model=m_id, **kw)
                # Success -> update stats
                self._record_success(pname)
                return resp
            except Exception as e:
                last_err = e
                self._record_fail(pname)
                logger.warning(f"fallo chat enrutador {pname}/{m_id}: {e}")
                continue
        raise RuntimeError(f"todos los proveedores fallaron (pista={hint}): {last_err}")

    def _attempts(self, model: str | None, hint: str | None, prefer_speed: bool) -> list[tuple[str, str]]:
        """Ordena (provider, model) a probar: routing explicito por modelo o resolve+fallback."""
        attempts: list[tuple[str, str]] = []
        if model:
            owners = {m.provider_name for m in self._models if m.id == model}
            if not owners:
                owners = set(self.fallback_order)
            ordered = [p for p in self.fallback_order if p in owners and p in self._providers]
            ordered += [p for p in owners if p in self._providers and p not in ordered]
            for pname in ordered:
                attempts.append((pname, model))
        else:
            routed = self.resolve(task_hint=hint, prefer_speed=prefer_speed)
            if routed:
                attempts.append((routed.route.provider_name, routed.model))
                for pname in self.fallback_order:
                    if pname != routed.route.provider_name and pname in self._providers:
                        hint_tier = HINT_MAPPING.get((hint or "none").lower(), 0)
                        cands = [m for m in self._models if m.provider_name == pname and m.effective_tier() >= hint_tier]
                        m_id = cands[0].id if cands else (self._models[0].id if self._models else "default")
                        attempts.append((pname, m_id))
            else:
                for pname in self.fallback_order:
                    if pname in self._providers:
                        attempts.append((pname, "default"))
        return attempts

    async def stream(self, messages, tools=None, model=None, hint=None, **kw):
        """Streaming con fallback: yield de eventos del primer provider que funcione."""
        attempts = self._attempts(model, hint, kw.pop("prefer_speed", False))
        last_err: Exception | None = None
        for pname, m_id in attempts:
            provider = self._providers.get(pname)
            if not provider:
                continue
            started = False
            try:
                async for ev in provider.stream(messages, tools=tools, model=m_id, **kw):
                    started = True
                    yield ev
                self._record_success(pname)
                return
            except Exception as e:
                self._record_fail(pname)
                if started:
                    # Ya emitimos tokens de este provider: NO caer a otro (mezclaria salidas).
                    logger.warning(f"stream {pname}/{m_id} falló a mitad: {e}")
                    raise
                last_err = e
                logger.warning(f"fallo stream enrutador {pname}/{m_id}: {e}")
                continue
        raise RuntimeError(f"todos los proveedores fallaron (stream, pista={hint}): {last_err}")

    def _record_success(self, provider_name: str) -> None:
        s = self._stats.setdefault(provider_name, {"success": 1, "fail": 1})
        s["success"] += 1

    def _record_fail(self, provider_name: str) -> None:
        s = self._stats.setdefault(provider_name, {"success": 1, "fail": 1})
        s["fail"] += 1

    # ---- auto_detect ----
    async def auto_detect(self) -> dict[str, bool]:
        """Probe health + list_models for each provider. Returns {name: ok}."""
        result: dict[str, bool] = {}
        for name, p in self._providers.items():
            try:
                ok = await p.health()
                result[name] = bool(ok)
                if ok:
                    try:
                        # Cloud: descubrir SOLO gratis verificados (ping real: filtra deepseek off)
                        if name == "cloud" and hasattr(p, "list_free_models"):
                            try:
                                free = await p.list_free_models(verify=True)  # type: ignore[attr-defined]
                            except TypeError:
                                free = await p.list_free_models()  # type: ignore[attr-defined]
                            for mid in free:
                                if not any(m.id == mid and m.provider_name == name for m in self._models):
                                    tier = MODEL_TIER_OVERRIDES.get(mid, 4)
                                    # Tier heuristics for unknown free models
                                    if "ultra" in mid or "pro" in mid:
                                        tier = 5
                                    elif "flash" in mid or "lightning" in mid:
                                        tier = 4
                                    elif "muse" in mid or "mimo" in mid:
                                        tier = 4
                                    self.register_model(ModelRoute(id=mid, provider_name=name, tier=tier, vram_gb=0.0))
                            # Also keep config-registered models but don't re-add all
                        else:
                            models = await p.list_models()
                            if models and not any(m.provider_name == name for m in self._models):
                                # Auto-register discovered models
                                for mid in models:
                                    if not any(m.id == mid and m.provider_name == name for m in self._models):
                                        tier = MODEL_TIER_OVERRIDES.get(mid, 3)
                                        # VRAM estimate
                                        vram = 4.0 if "7b" in mid or "8b" in mid else (0.5 if "0.5b" in mid else 6.0)
                                        self.register_model(ModelRoute(id=mid, provider_name=name, tier=tier, vram_gb=vram))
                    except Exception as e:
                        logger.debug(f"auto_detect list_models {name} failed: {e}")
            except Exception as e:
                logger.debug(f"auto_detect health {name} failed: {e}")
                result[name] = False
        return result

    async def refresh_cloud_free_models(self, force: bool = False, verify: bool = False) -> list[str]:
        """Fuerza descubrimiento de modelos gratis y sincroniza el registro. verify=True filtra deepseek off. Devuelve lista viva."""
        free: list[str] = []
        for name, p in self._providers.items():
            if name != "cloud":
                continue
            if not hasattr(p, "list_free_models"):
                continue
            try:
                free = await p.list_free_models(force=force, verify=verify)  # type: ignore[attr-defined]
            except TypeError:
                # compat: provider viejo sin param verify
                try:
                    free = await p.list_free_models(force=force)  # type: ignore[attr-defined]
                except Exception:
                    free = []
            except Exception as e:
                logger.debug(f"refresh_cloud_free_models failed: {e}")
                try:
                    free = await p.list_free_models(force=False, verify=False)  # type: ignore[attr-defined]
                except Exception:
                    free = []
            if free and not verify:
                # Sin verify: filtrar por la última lista verified en disco (la escribe
                # list_free_models(verify=True) en boot). Evita registrar modelos gated
                # con 403 (Zen: solo space-bunny free responde 200, 2026-09-28).
                try:
                    _cf = Path("data/cloud_free_cache.json")
                    if _cf.exists():
                        _v = json.loads(_cf.read_text()).get("verified")
                        if isinstance(_v, list) and _v:
                            free = [m for m in free if m in _v]
                except Exception:
                    pass
            for mid in free:
                if not any(m.id == mid and m.provider_name == name for m in self._models):
                    tier = MODEL_TIER_OVERRIDES.get(mid, 4)
                    if "ultra" in mid or "pro" in mid:
                        tier = 5
                    self.register_model(ModelRoute(id=mid, provider_name=name, tier=tier, vram_gb=0.0))
            # No prunear antiguos: si Zen retiró un gratis, el entry queda pero health filtrará
            break
        return free

    async def health_all(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name, p in self._providers.items():
            try:
                ok = await p.health()
                models = await p.list_models() if ok else []
                out[name] = {"ok": ok, "models": models, "registered": [m.id for m in self._models if m.provider_name == name]}
            except Exception as e:
                out[name] = {"ok": False, "error": str(e), "models": []}
        return out

    def stats(self) -> dict:
        return {
            "strategy": self.strategy,
            "vram_budget_gb": self.vram_budget_gb,
            "fallback_order": self.fallback_order,
            "providers": list(self._providers.keys()),
            "models": [{"id": m.id, "provider": m.provider_name, "tier": m.effective_tier(), "vram_gb": m.vram_gb} for m in self._models],
            "thompson_stats": dict(self._stats),
        }
