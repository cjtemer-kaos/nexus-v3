"""
Local Provider Auto-Discovery.
Adaptado del patrón Hermes (local-provider-discovery.ts).
Probea puertos locales conocidos para backends OpenAI-compatibles.
Async, con timeout 800ms, re-probe cada 30s, sin bloqueo en startup.
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class ProviderDef:
    id: str
    name: str
    port: int
    api_base: str
    models_path: str = "/api/tags"
    probe_path: str = "/"
    api_mode: str = "ollama"


LOCAL_PROVIDERS: List[ProviderDef] = [
    ProviderDef(id="ollama", name="Ollama", port=11434, api_base="http://127.0.0.1:11434"),
    ProviderDef(id="freeqwen", name="FreeQwen", port=3264, api_base="http://127.0.0.1:3264/v1", models_path="/models", probe_path="/v1", api_mode="openai"),
    ProviderDef(id="lm-studio", name="LM Studio", port=1234, api_base="http://127.0.0.1:1234/v1", models_path="/models", probe_path="/v1", api_mode="openai"),
    ProviderDef(id="text-gen-webui", name="Text Gen WebUI", port=5000, api_base="http://127.0.0.1:5000/v1", models_path="/models", probe_path="/v1", api_mode="openai"),
]


@dataclass
class DiscoveredModel:
    id: str
    name: str
    provider: str
    source: str = "local-discovery"
    size: Optional[int] = None


@dataclass
class DiscoveredProvider:
    def_: ProviderDef
    online: bool = False
    models: List[DiscoveredModel] = field(default_factory=list)
    last_probe: float = 0.0


_PROBE_TTL = 30.0
_PROBE_TIMEOUT = 0.8
_state: Dict[str, DiscoveredProvider] = {}


def _make_probe_url(pd: ProviderDef) -> str:
    return f"http://127.0.0.1:{pd.port}{pd.probe_path}"


def _make_models_url(pd: ProviderDef) -> str:
    return f"http://127.0.0.1:{pd.port}{pd.models_path}"


async def _probe_one(pd: ProviderDef) -> Optional[DiscoveredProvider]:
    try:
        probe_url = _make_probe_url(pd)
        async with asyncio.timeout(_PROBE_TIMEOUT):
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(probe_url) as resp:
                    if resp.status >= 400:
                        return None

        dp = DiscoveredProvider(def_=pd, online=True, last_probe=time.monotonic())

        try:
            models_url = _make_models_url(pd)
            async with asyncio.timeout(_PROBE_TIMEOUT):
                async with aiohttp.ClientSession() as session:
                    async with session.get(models_url) as resp:
                        if resp.status == 200:
                            data = await resp.json()
                            if pd.api_mode == "ollama":
                                for m in data.get("models", []):
                                    name = m.get("name", "").replace(":latest", "")
                                    dp.models.append(DiscoveredModel(
                                        id=name, name=name, provider=pd.id
                                    ))
                            elif pd.api_mode == "openai":
                                for m in data.get("data", []):
                                    mid = m.get("id", "")
                                    dp.models.append(DiscoveredModel(
                                        id=mid, name=mid, provider=pd.id
                                    ))
        except (asyncio.TimeoutError, Exception):
            pass

        return dp
    except (asyncio.TimeoutError, Exception):
        return None


async def discover_providers(force: bool = False) -> List[DiscoveredProvider]:
    now = time.monotonic()
    results = []

    for pd in LOCAL_PROVIDERS:
        existing = _state.get(pd.id)
        if existing and existing.online and not force and (now - existing.last_probe) < _PROBE_TTL:
            results.append(existing)
            continue

        dp = await _probe_one(pd)
        if dp:
            _state[pd.id] = dp
            results.append(dp)
        else:
            offline = DiscoveredProvider(def_=pd, online=False)
            _state[pd.id] = offline

    return results


def get_discovered_models() -> List[DiscoveredModel]:
    models = []
    for dp in _state.values():
        if dp.online:
            models.extend(dp.models)
    return models


def get_discovered_providers() -> List[DiscoveredProvider]:
    return list(_state.values())
