"""Cliente de embeddings — Ollama local (OpenAI-compat /v1/embeddings).

URL desde config providers.ollama.embed_url (default http://localhost:11434).
API OpenAI-compatible: POST /v1/embeddings {model, input} -> data[0].embedding.
"""
from __future__ import annotations

import json
import urllib.request


def _cfg_embed_url() -> str:
    """URL de embeddings: OLLAMA_HOST > providers.ollama.embed_url > base_url > default.

    Antes se hacía `re.search(r"embed_url: ...")` sobre el YAML crudo, así que
    ignoraba OLLAMA_HOST, los `${ENV}` sin expandir y el parser de config: en una
    PC con Ollama en otra dirección, RAG/embeddings fallaban sin explicación.
    """
    from src.core.environment import ollama_base_url

    cfg_base = None
    try:
        from src.core.config import load_config

        oc = (load_config().get("providers", {}) or {}).get("ollama", {}) or {}
        cfg_base = oc.get("embed_url") or oc.get("base_url")
    except Exception:
        pass
    return ollama_base_url(cfg_base)


def embed_url() -> str:
    return _cfg_embed_url()


def embed(texts: str | list[str], model: str = "nomic-embed-text", timeout: int = 60) -> list[list[float]]:
    """Devuelve embeddings (768d) para uno o varios textos."""
    single = isinstance(texts, str)
    items = [texts] if single else list(texts)
    base = _cfg_embed_url().rstrip("/")
    payload = json.dumps({"model": model, "input": items}).encode()
    req = urllib.request.Request(
        f"{base}/v1/embeddings", data=payload,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    vecs = [d["embedding"] for d in sorted(data.get("data", []), key=lambda d: d.get("index", 0))]
    return vecs


def health() -> bool:
    try:
        with urllib.request.urlopen(_cfg_embed_url().rstrip("/") + "/health", timeout=5):
            return True
    except Exception:
        try:
            with urllib.request.urlopen(_cfg_embed_url().rstrip("/") + "/v1/models", timeout=5) as r:
                return r.status == 200
        except Exception:
            return False
