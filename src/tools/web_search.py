"""Web search open-source.

- SearXNG (metabuscador open source) si se configura SEARXNG_URL (self-hosted o
  instancia con API JSON habilitada).
- Fallback: Wikipedia OpenSearch (API abierta, sin key).

Sin dependencias externas de pago. Devuelve {query, count, results[], source}.
"""
from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger("nexus-websearch")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")


async def web_search(query: str, limit: int = 6, timeout: int = 15) -> dict[str, Any]:
    q = (query or "").strip()
    if not q:
        return {"error": "query vacia", "query": query, "results": []}
    results: list[dict] = []
    source = ""

    # 1) SearXNG (open source, self-hosted) — requiere SEARXNG_URL
    base = os.environ.get("SEARXNG_URL", "").rstrip("/")
    if base:
        try:
            async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": _UA}, follow_redirects=True) as c:
                r = await c.get(f"{base}/search", params={"q": q, "format": "json"})
                if "json" in r.headers.get("content-type", ""):
                    for x in (r.json().get("results") or [])[:limit]:
                        results.append({
                            "title": x.get("title", ""),
                            "url": x.get("url", ""),
                            "snippet": x.get("content", ""),
                        })
                    if results:
                        source = "searxng"
        except Exception as e:
            logger.warning(f"SearXNG fallo: {e}")

    # 2) Wikipedia full-text search (API abierta, sin key)
    if not results:
        try:
            async with httpx.AsyncClient(timeout=timeout, headers={"User-Agent": _UA}) as c:
                r = await c.get("https://en.wikipedia.org/w/api.php", params={
                    "action": "query", "list": "search", "srsearch": q, "srlimit": limit, "format": "json",
                })
                hits = (((r.json() or {}).get("query") or {}).get("search") or [])
                import re as _re
                for h in hits[:limit]:
                    title = h.get("title", "")
                    snippet = _re.sub(r"<[^>]+>", "", h.get("snippet", "") or "")
                    url = "https://en.wikipedia.org/wiki/" + title.replace(" ", "_")
                    results.append({"title": title, "url": url, "snippet": snippet})
                if results:
                    source = "wikipedia"
        except Exception as e:
            logger.warning(f"Wikipedia fallo: {e}")

    return {"query": q, "count": len(results), "results": results, "source": source or "none"}
