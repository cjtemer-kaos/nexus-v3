"""
WebResearcher - Multi-backend web research orchestrator.

Tries backends in order:
  search():    DDG -> Brave Search MCP -> agent-browser -> Playwright
  navigate():  HTTP fetch -> agent-browser -> Playwright -> Chrome DevTools
  snapshot():  agent-browser compact tree with @e1/@e2 refs (80-90% less tokens)
  interact():  click @e1, fill @e2, etc. using deterministic refs

Each fallback activates only when the previous one fails.
"""

import asyncio
import json
import logging
import re
from typing import Dict, List, Optional
from urllib.parse import quote, unquote

import httpx
from bs4 import BeautifulSoup

from src.security.ssrf_guard import ensure_safe_url, SSRFBlocked

logger = logging.getLogger(__name__)


class WebResearcher:
    def __init__(self, mcp_client=None):
        self.mcp = mcp_client
        self._http = None
        self._rate_limits: Dict[str, float] = {}  # dominio -> ultimo_request
        self._min_interval = 1.0  # 1 segundo entre requests al mismo dominio

    async def _check_rate_limit(self, url: str):
        """Rate limiting por dominio."""
        import time
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        now = time.time()
        last = self._rate_limits.get(domain, 0)
        if now - last < self._min_interval:
            await asyncio.sleep(self._min_interval - (now - last))
        self._rate_limits[domain] = time.time()

    async def _get_http(self) -> httpx.AsyncClient:
        if self._http is None or self._http.is_closed:
            self._http = httpx.AsyncClient(
                timeout=20.0,
                follow_redirects=True,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"},
            )
        return self._http

    async def _get_tor_http(self) -> httpx.AsyncClient:
        """Async HTTP client routed through Tor SOCKS5 proxy (localhost:9050)."""
        if not hasattr(self, '_tor_http') or self._tor_http is None or self._tor_http.is_closed:
            try:
                self._tor_http = httpx.AsyncClient(
                    timeout=30.0,
                    follow_redirects=True,
                    proxy="socks5://127.0.0.1:9050",
                    headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:128.0) Gecko/20100101 Firefox/128.0"},
                )
            except Exception as e:
                logger.debug(f"Tor SOCKS5 proxy unavailable: {e}")
                self._tor_http = None
        return self._tor_http

    # ── MCP helper ───────────────────────────────────────────────

    async def _mcp(self, server: str, tool: str, args: dict) -> Optional[dict]:
        if not self.mcp:
            return None
        try:
            await self.mcp.start_server_if_needed(server)
            return await self.mcp.call_tool(f"mcp__{server}__{tool}", args)
        except Exception as e:
            logger.debug(f"MCP {server}/{tool} failed: {e}")
            return None

    def _ok(self, result: Optional[dict]) -> bool:
        return result is not None and result.get("success") is True

    def _content(self, result: Optional[dict]) -> str:
        if result and "result" in result:
            r = result["result"]
            if isinstance(r, dict):
                return r.get("content", json.dumps(r))
            return str(r)
        return ""

    # ── Search backends ─────────────────────────────────────────

    async def search(self, query: str, max_results: int = 5, include_tor: bool = False) -> List[Dict]:
        backends = [self._search_ddg, self._search_brave, self._search_agent_browser, self._search_playwright]
        if include_tor:
            backends.append(self._search_tor)
        for method in backends:
            results = await method(query, max_results)
            if results:
                return results
        return []

    async def _search_tor(self, query: str, max_results: int) -> List[Dict]:
        """Search via Tor network using Ahmia search engine (ahmia.fi).

        Falls back to DuckDuckGo onion if Ahmia is unavailable.
        Requires Tor SOCKS5 proxy running on localhost:9050.
        """
        tor = await self._get_tor_http()
        if not tor:
            return []

        ahmia_queries = [
            f"https://ahmia.fi/search/?q={quote(query)}",
            f"http://3f4hnzxfmfxkycfx6s3heqr4jg4r22pvwrk6rrzqzrvgpi27v5jvqrid.onion/search/?q={quote(query)}",
        ]
        for url in ahmia_queries:
            try:
                r = await tor.get(url, timeout=25.0)
                if r.status_code == 200:
                    soup = BeautifulSoup(r.text, "html.parser")
                    results = []
                    for item in soup.select(".result"):
                        title_el = item.select_one("h4, h3, .title")
                        link_el = item.select_one("a[href]")
                        snippet_el = item.select_one("p, .snippet, .description")
                        if link_el:
                            href = link_el.get("href", "")
                            if href.startswith("/"):
                                href = f"https://ahmia.fi{href}"
                            results.append({
                                "url": href,
                                "title": title_el.get_text(strip=True) if title_el else "",
                                "snippet": snippet_el.get_text(strip=True) if snippet_el else "",
                                "source": "ahmia",
                            })
                            if len(results) >= max_results:
                                break
                    if results:
                        return results
            except Exception as e:
                logger.debug(f"Ahmia search failed ({url[:50]}): {e}")

        try:
            ddg_onion = f"https://duckduckgogg2xj2aqae3fwgq3q5daxcgit3yd22hlmlkzr2cx3rpxnotid.onion/?q={quote(query)}"
            r = await tor.get(ddg_onion, timeout=25.0)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                results = []
                for a in soup.select("a[href]"):
                    href = a.get("href", "")
                    text = a.get_text(strip=True)
                    if text and href.startswith("http") and ".onion" in href:
                        results.append({
                            "url": href,
                            "title": text,
                            "snippet": "",
                            "source": "ddg-onion",
                        })
                        if len(results) >= max_results:
                            break
                if results:
                    return results
        except Exception as e:
            logger.debug(f"DDG onion search failed: {e}")

        return []

    async def navigate_tor(self, url: str) -> Dict:
        """Navigate to a .onion site via Tor and extract content."""
        tor = await self._get_tor_http()
        if not tor:
            return {"url": url, "error": "Tor SOCKS5 proxy not available (localhost:9050)", "status": "proxy_unavailable"}

        try:
            r = await tor.get(url, timeout=30.0)
            if r.status_code == 200:
                soup = BeautifulSoup(r.text, "html.parser")
                title = soup.title.string.strip() if soup.title and soup.title.string else ""
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.extract()
                text = soup.get_text(separator="\n", strip=True)
                return {"url": url, "title": title, "content": text[:5000], "status": "ok", "status_code": r.status_code}
            return {"url": url, "error": f"HTTP {r.status_code}", "status": "http_error", "status_code": r.status_code}
        except Exception as e:
            return {"url": url, "error": str(e), "status": "exception"}

    async def _search_ddg(self, query: str, max_results: int) -> List[Dict]:
        """DDG via official `ddgs` Python lib (formerly duckduckgo-search).

        Runs in a thread because ddgs is sync. Falls back to legacy HTML
        scraping if the lib raises (rate-limit, network, etc.).
        """
        # Path 1 — official lib (preferred)
        try:
            import asyncio as _asyncio
            from ddgs import DDGS  # type: ignore

            def _run() -> List[Dict]:
                try:
                    with DDGS() as ddgs:
                        hits = list(ddgs.text(query, max_results=max_results))
                except Exception as e:
                    logger.debug(f"ddgs lib failed: {e}")
                    return []
                out: List[Dict] = []
                for h in hits[:max_results]:
                    out.append({
                        "url": h.get("href") or h.get("url") or "",
                        "title": h.get("title") or "",
                        "snippet": h.get("body") or h.get("snippet") or "",
                        "source": "ddgs",
                    })
                return out

            results = await _asyncio.to_thread(_run)
            if results:
                return results
        except ImportError:
            logger.debug("ddgs lib not installed; using HTML fallback")
        except Exception as e:
            logger.debug(f"ddgs path failed: {e}")

        # Path 2 — legacy HTML scrape (often blocked by DDG anti-bot)
        try:
            c = await self._get_http()
            r = await c.get(f"https://html.duckduckgo.com/html/?q={quote(query)}")
            if r.status_code != 200:
                return []
            soup = BeautifulSoup(r.text, "html.parser")
            results = []
            for a in soup.select("a.result__snippet"):
                link = a.find_previous_sibling("a", class_="result__url")
                if link and link.get("href"):
                    raw_url = link["href"]
                    if raw_url.startswith("//duckduckgo.com/l/?uddg="):
                        raw_url = unquote(raw_url.split("uddg=")[1].split("&")[0])
                    results.append({
                        "url": raw_url,
                        "title": link.get_text(strip=True),
                        "snippet": a.get_text(strip=True),
                        "source": "ddg-html",
                    })
                    if len(results) >= max_results:
                        break
            return results
        except Exception as e:
            logger.debug(f"DDG HTML fallback failed: {e}")
            return []

    async def _search_brave(self, query: str, max_results: int) -> List[Dict]:
        r = await self._mcp("brave-search", "search", {"query": query, "count": max_results})
        if self._ok(r):
            items = r["result"].get("result", {}).get("web", {}).get("results", [])
            return [
                {"url": x.get("url", ""), "title": x.get("title", ""), "snippet": x.get("description", ""), "source": "brave"}
                for x in items[:max_results]
            ]
        return []

    async def _search_agent_browser(self, query: str, max_results: int) -> List[Dict]:
        url = f"https://www.google.com/search?q={quote(query)}&num={min(max_results, 10)}"
        r = await self._mcp("agent-browser", "browser_open", {"url": url})
        if not self._ok(r):
            return []
        await self._mcp("agent-browser", "browser_wait", {"condition": "load", "value": "networkidle"})
        snap = await self._mcp("agent-browser", "browser_snapshot", {"interactive_only": True})
        if not self._ok(snap):
            return []
        return self._parse_agent_browser_snapshot(self._content(snap), max_results, query)

    def _parse_agent_browser_snapshot(self, text: str, max_results: int, query: str) -> List[Dict]:
        results = []
        lines = text.split("\n")
        for line in lines:
            if "[link]" in line.lower() or "[heading]" in line.lower():
                parts = line.split("]")
                if len(parts) >= 2:
                    label = parts[-1].strip().strip('"').strip("'")
                    if label and len(label) > 3 and "google" not in label.lower():
                        url = f"https://www.google.com/search?q={quote(label)}"
                        results.append({"url": url, "title": label, "snippet": "", "source": "agent-browser"})
                        if len(results) >= max_results:
                            break
        if not results:
            for i, line in enumerate(lines):
                stripped = line.strip()
                if stripped and len(stripped) > 20 and not stripped.startswith("[") and not stripped.startswith("@"):
                    results.append({"url": f"https://www.google.com/search?q={quote(stripped[:50])}", "title": stripped[:100], "snippet": "", "source": "agent-browser"})
                    if len(results) >= max_results:
                        break
        return results

    async def _search_playwright(self, query: str, max_results: int) -> List[Dict]:
        url = f"https://www.google.com/search?q={quote(query)}&num={max_results}"
        r = await self._mcp("playwright", "browser_navigate", {"url": url})
        if not self._ok(r):
            return []
        snap = await self._mcp("playwright", "browser_snapshot", {})
        if not self._ok(snap):
            return []
        raw = self._content(snap)
        results = []
        lines = raw.split("\n")
        for i, line in enumerate(lines):
            if "link" in line.lower() and i + 2 < len(lines):
                title = line.strip()
                url_line = lines[i + 1].strip()
                snippet = lines[i + 2].strip()[:200] if i + 2 < len(lines) else ""
                if "http" in url_line or "www." in url_line:
                    results.append({"url": url_line, "title": title, "snippet": snippet, "source": "playwright"})
                    if len(results) >= max_results:
                        break
        return results

    # ── Navigation backends ─────────────────────────────────────

    async def navigate(self, url: str) -> Optional[str]:
        await self._check_rate_limit(url)  # Rate limiting por dominio
        for method in [self._fetch_http, self._navigate_agent_browser, self._navigate_playwright, self._navigate_chrome]:
            content = await method(url)
            if content:
                return content
        return None

    async def _fetch_http(self, url: str) -> Optional[str]:
        try:
            ensure_safe_url(url)
        except SSRFBlocked as e:
            logger.warning(f"SSRF blocked: {url} — {e}")
            return None
        try:
            c = await self._get_http()
            r = await c.get(url)
            if r.status_code != 200:
                return None
            soup = BeautifulSoup(r.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            text = soup.get_text(separator="\n", strip=True)
            lines = [line for line in text.split("\n") if line.strip()]
            return "\n".join(lines)[:8000]
        except Exception as e:
            logger.debug(f"HTTP fetch failed: {e}")
            return None

    async def _navigate_agent_browser(self, url: str) -> Optional[str]:
        r = await self._mcp("agent-browser", "browser_open", {"url": url})
        if not self._ok(r):
            return None
        await self._mcp("agent-browser", "browser_wait", {"condition": "load", "value": "networkidle"})
        await asyncio.sleep(1)
        r = await self._mcp("agent-browser", "browser_get_text", {"selector": ""})
        if self._ok(r):
            return self._content(r)[:8000]
        return None

    async def _navigate_playwright(self, url: str) -> Optional[str]:
        r = await self._mcp("playwright", "browser_navigate", {"url": url})
        if not self._ok(r):
            return None
        await asyncio.sleep(2)
        r = await self._mcp("playwright", "browser_evaluate", {"function": "() => document.body.innerText"})
        if self._ok(r):
            text = r["result"].get("result", "")
            return text[:8000] if isinstance(text, str) else None
        return None

    async def _navigate_chrome(self, url: str) -> Optional[str]:
        r = await self._mcp("chrome-devtools", "navigate_page", {"url": url})
        if not self._ok(r):
            return None
        await asyncio.sleep(2)
        r = await self._mcp("chrome-devtools", "take_snapshot", {})
        if self._ok(r):
            return json.dumps(r["result"], indent=2)[:8000]
        return None

    # ── Snapshot / Ref system (agent-browser: compact tree with @e1/@e2) ──

    async def snapshot(self, url: str = "", interactive_only: bool = True) -> Dict:
        """
        Navigate to a URL (or use current page) and get compact accessibility tree.
        Returns structured data with refs that agents can use for follow-up actions.
        Similar to agent-browser's snapshot --compact mode: ~300 tokens vs 5000 for DOM.
        """
        if url:
            r = await self._mcp("agent-browser", "browser_open", {"url": url})
            if not self._ok(r):
                return {"success": False, "error": "Failed to open URL", "url": url}
            await self._mcp("agent-browser", "browser_wait", {"condition": "load", "value": "networkidle"})
            await asyncio.sleep(1)

        r = await self._mcp("agent-browser", "browser_snapshot", {"interactive_only": interactive_only})
        if not self._ok(r):
            return {"success": False, "error": "Snapshot failed"}

        raw = self._content(r)
        title = self._content(await self._mcp("agent-browser", "browser_get_title", {}))
        current_url = self._content(await self._mcp("agent-browser", "browser_get_url", {}))

        refs = self._parse_refs(raw)
        compact = self._compact_tree(raw)

        return {
            "success": True,
            "url": current_url or url,
            "title": title,
            "refs": refs,
            "compact": compact,
            "raw": raw,
            "ref_count": len(refs),
            "token_estimate": len(compact.split()) if compact else 0,
        }

    def _parse_refs(self, snapshot_text: str) -> Dict[str, str]:
        """Parse @e1, @e2 refs from agent-browser snapshot output."""
        refs = {}
        if not snapshot_text:
            return refs
        for line in snapshot_text.split("\n"):
            m = re.match(r'\s*@(\w+)\s+(\[.*?\])\s+(.*)', line)
            if m:
                ref_id = f"@{m.group(1)}"
                role = m.group(2)
                label = m.group(3).strip().strip('"').strip("'")
                refs[ref_id] = f"{role} {label}"
            m = re.match(r'\s*@(\w+)\s+\[(.*?)\].*"(.*?)"', line)
            if m and f"@{m.group(1)}" not in refs:
                refs[f"@{m.group(1)}"] = f"[{m.group(2)}] \"{m.group(3)}\""
        return refs

    def _compact_tree(self, snapshot_text: str) -> str:
        """Strip structural lines keeping only refs + content (compact tree mode)."""
        if not snapshot_text:
            return ""
        lines = snapshot_text.split("\n")
        kept = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if "@" in stripped or re.match(r'^[\s]*[^\s]', stripped):
                if any(kw in stripped.lower() for kw in ["heading", "link", "button", "input", "textbox", "checkbox", "radio", "combobox", "listbox", "switch", "tab", "menu"]):
                    kept.append(stripped)
                elif stripped.startswith("@") or re.match(r'^\s*@', stripped):
                    kept.append(stripped)
                elif ": " in stripped and len(stripped) > 10:
                    kept.append(stripped)
        return "\n".join(kept[:50])

    async def interact(self, ref_or_command: str, value: str = "") -> str:
        """
        Interact with the page using deterministic refs from snapshot().
        Examples:
          interact("@e1")          -> click @e1
          interact("@e2", "text")  -> fill @e2 with "text"
          interact("click @e1")    -> click @e1
          interact("#submit")      -> click CSS selector
        """
        if not value and " " in ref_or_command:
            cmd = ref_or_command
        elif value:
            parts = ref_or_command.split(None, 1)
            if len(parts) == 1:
                cmd = f"click {ref_or_command}" if ref_or_command.startswith("@") or ref_or_command.startswith("#") else ref_or_command
            else:
                action, sel = parts
                if action in ("click", "fill", "type", "hover", "select", "check", "uncheck", "press"):
                    cmd = f"{action} {sel} {value}" if action in ("fill", "type", "select") else f"{action} {sel}"
                else:
                    cmd = ref_or_command
        else:
            cmd = f"click {ref_or_command}" if (ref_or_command.startswith("@") or ref_or_command.startswith("#")) else ref_or_command

        return await self.browser_command(cmd)

    async def browser_command(self, command: str) -> str:
        """Run any agent-browser command directly. Browser persists via daemon."""
        import subprocess
        try:
            import shlex
            parts = shlex.split(command)
            r = subprocess.run(
                ["agent-browser"] + parts,
                capture_output=True, text=True, timeout=30,
            )
            if r.returncode != 0:
                return f"[error] {r.stderr.strip() or r.stdout.strip()}"
            return r.stdout.strip() or "(done)"
        except FileNotFoundError:
            return "[error] agent-browser not installed"
        except subprocess.TimeoutExpired:
            return "[timeout]"
        except Exception as e:
            return f"[error] {e}"

    async def close(self):
        if self._http and not self._http.is_closed:
            await self._http.aclose()
