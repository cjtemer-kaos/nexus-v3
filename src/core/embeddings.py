"""Cliente de embeddings — apunta al servidor :8001 (llama.cpp autocontenido).

URL desde config providers.llamacpp.embed_url (default http://127.0.0.1:8001).
API OpenAI-compatible: POST /v1/embeddings {model, input} -> data[0].embedding.
"""
from __future__ import annotations

import json
import urllib.request
from pathlib import Path


def _cfg_embed_url() -> str:
    try:
        import os
        root = Path(os.environ.get("NEXUS_ROOT", "")) if os.environ.get("NEXUS_ROOT") else Path(__file__).parents[2]
        import re
        txt = (root / "config" / "config.yaml").read_text(encoding="utf-8")
        m = re.search(r"embed_url:\s*(\S+)", txt)
        if m:
            return m.group(1).strip()
    except Exception:
        pass
    return "http://127.0.0.1:8001"


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
