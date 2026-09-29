import asyncio
import json
import logging
import os
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

logger = logging.getLogger("nexus-cdp")


@dataclass
class BrowserState:
    url: str = ""
    title: str = ""
    status: str = "disconnected"
    pages: List[Dict] = field(default_factory=list)
    active_page_id: str = ""


@dataclass
class CdpResult:
    success: bool = False
    data: Any = None
    error: str = ""
    duration_ms: float = 0.0


class AccessibilitySnapshotNode:
    def __init__(self, node_id: str, role: str, name: str, value: str = "",
                 children: List['AccessibilitySnapshotNode'] = None,
                 properties: Dict = None):
        self.node_id = node_id
        self.role = role
        self.name = name
        self.value = value
        self.children = children or []
        self.properties = properties or {}

    def to_text(self, indent: int = 0) -> str:
        prefix = "  " * indent
        parts = [f"{prefix}{self.role}: \"{self.name}\""]
        if self.value:
            parts[0] += f" = \"{self.value[:80]}\""
        if self.properties:
            extra = "; ".join(f"{k}={v}" for k, v in self.properties.items() if v)
            if extra:
                parts[0] += f" [{extra}]"
        for child in self.children:
            parts.append(child.to_text(indent + 1))
        return "\n".join(parts)

    def to_dict(self) -> Dict:
        return {
            "node_id": self.node_id,
            "role": self.role,
            "name": self.name,
            "value": self.value,
            "properties": self.properties,
            "children": [c.to_dict() for c in self.children],
        }


class CdpBrowser:
    def __init__(self, browser_url: str = "http://127.0.0.1:9222",
                 headless: bool = False, executable_path: str = ""):
        self.browser_url = browser_url
        self.headless = headless
        self.executable_path = executable_path
        self._ws_url: Optional[str] = None
        self._pages: Dict[str, Dict] = {}
        self._state = BrowserState()
        self._page_counter = 0
        self._collectors: Dict[str, Dict] = {}
        self._chrome_proc = None
        self._temp_dir = None

    async def connect(self) -> bool:
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.browser_url}/json/version")
                if resp.status_code == 200:
                    data = resp.json()
                    self._ws_url = data.get("webSocketDebuggerUrl", "")
                    self._state.status = "connected"
                    logger.info(f"CDP connected: {self.browser_url}")
                    await self._refresh_pages()
                    return True
        except Exception as e:
            logger.warning(f"CDP connect failed: {e}")

        if self.executable_path or not self.headless:
            return await self._launch_browser()
        return False

    async def _launch_browser(self) -> bool:
        import subprocess
        try:
            chrome = self.executable_path or self._find_chrome()
            if not chrome:
                return False
            self._temp_dir = tempfile.mkdtemp(prefix="nexus-cdp-")
            cmd = [chrome, "--remote-debugging-port=9222",
                   f"--user-data-dir={self._temp_dir}"]
            if self.headless:
                cmd.append("--headless=new")
            self._chrome_proc = subprocess.Popen(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            for attempt in range(15):
                if await self.connect():
                    return True
                await asyncio.sleep(0.5)
        except Exception as e:
            logger.error(f"Browser launch failed: {e}")
        return False

    async def disconnect(self):
        """Clean up browser process and temp directory."""
        self._state.status = "disconnected"
        self._ws_url = None
        self._pages.clear()
        # Kill Chrome process if we launched it
        proc = getattr(self, '_chrome_proc', None)
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
        # Clean temp dir
        import shutil
        temp_dir = getattr(self, '_temp_dir', None)
        if temp_dir and os.path.exists(temp_dir):
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except Exception:
                pass

    def _find_chrome(self) -> Optional[str]:
        candidates = [
            "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe",
            "C:\\Program Files (x86)\\Google\\Chrome\\Application\\chrome.exe",
            os.path.expanduser("~\\AppData\\Local\\Google\\Chrome\\Application\\chrome.exe"),
            "/usr/bin/google-chrome",
            "/usr/bin/chromium",
            "/usr/bin/chromium-browser",
        ]
        for path in candidates:
            if os.path.exists(path):
                return path
        return None

    async def _refresh_pages(self):
        try:
            import httpx
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{self.browser_url}/json")
                if resp.status_code == 200:
                    pages = resp.json()
                    self._pages = {}
                    for p in pages:
                        pid = p.get("id", str(len(self._pages)))
                        self._pages[pid] = p
                        if not self._state.active_page_id:
                            self._state.active_page_id = pid
                    self._state.pages = pages
        except Exception:
            pass

    async def navigate_page(self, url: str, page_id: str = "") -> CdpResult:
        start = time.time()
        await self._refresh_pages()
        target = page_id or self._state.active_page_id
        if not target:
            return CdpResult(error="No page available")
        ws_url = self._pages.get(target, {}).get("webSocketDebuggerUrl", "")
        if not ws_url:
            return CdpResult(error=f"Page {target} not found")
        try:
            import httpx
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(
                    f"{self.browser_url}/json/activate/{target}")
                if resp.status_code == 200:
                    nav_resp = await client.get(
                        f"{self.browser_url}/json/new?{url}")
            self._state.url = url
            self._state.active_page_id = target
            await self._wait_for_page_ready()
            return CdpResult(success=True, data={"url": url, "page_id": target},
                             duration_ms=(time.perf_counter() - start) * 1000)
        except Exception as e:
            return CdpResult(error=str(e))

    async def evaluate_script(self, script: str, page_id: str = "") -> CdpResult:
        start = time.time()
        if self._pages:
            target = page_id or self._state.active_page_id
            target_info = self._pages.get(target, {})
            ws_url = target_info.get("webSocketDebuggerUrl", "")
            if not ws_url:
                return CdpResult(error="No page WebSocket URL")
            return await self._send_cdp(ws_url, "Runtime.evaluate",
                                        {"expression": script, "returnByValue": True, "awaitPromise": True})
        if not self._ws_url:
            return CdpResult(error="No browser connected", duration_ms=(time.time() - start) * 1000)
        return CdpResult(error="No pages available", duration_ms=(time.time() - start) * 1000)

    async def _send_cdp(self, ws_url: str, method: str, params: Dict = None) -> CdpResult:
        start = time.time()
        _msg_id = [1]
        payload = {"id": _msg_id[0], "method": method, "params": params or {}}
        _msg_id[0] += 1
        try:
            import websockets
            async with websockets.connect(ws_url, max_size=10_000_000) as ws:
                await ws.send(json.dumps(payload))
                resp = await asyncio.wait_for(ws.recv(), timeout=30.0)
                data = json.loads(resp)
                return CdpResult(success=True, data=data.get("result", data),
                                 duration_ms=(time.perf_counter() - start) * 1000)
        except ImportError:
            import httpx
            try:
                http_url = ws_url.replace("ws://", "http://").replace("wss://", "https://")
                async with httpx.AsyncClient(timeout=30.0) as client:
                    resp = await client.post(http_url, json=payload)
                    return CdpResult(success=True, data=resp.json(),
                                     duration_ms=(time.perf_counter() - start) * 1000)
            except Exception as e:
                return CdpResult(error=f"CDP HTTP fallback failed: {e}",
                                 duration_ms=(time.perf_counter() - start) * 1000)
        except Exception as e:
            return CdpResult(error=f"CDP command failed: {e}",
                             duration_ms=(time.perf_counter() - start) * 1000)

    async def take_screenshot(self, page_id: str = "", format: str = "png",
                               quality: int = 80, full_page: bool = False) -> CdpResult:
        start = time.time()
        target = page_id or self._state.active_page_id
        target_info = self._pages.get(target, {})
        ws_url = target_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return CdpResult(error="No page available")
        params = {"format": format, "quality": quality}
        if full_page:
            result = await self.evaluate_script(
                "({width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})",
                page_id)
            if result.success and isinstance(result.data, dict):
                params["clip"] = {"x": 0, "y": 0,
                                  "width": result.data.get("result", {}).get("value", {}).get("width", 1920),
                                  "height": result.data.get("result", {}).get("value", {}).get("height", 1080)}
        result = await self._send_cdp(ws_url, "Page.captureScreenshot", params)
        if result.success:
            data = result.data if isinstance(result.data, dict) else {}
            screenshot_data = data.get("data", "")
            if screenshot_data:
                tmp_path = os.path.join(tempfile.gettempdir(),
                                        f"nexus-screenshot-{int(time.time())}.{format}")
                import base64
                with open(tmp_path, "wb") as f:
                    f.write(base64.b64decode(screenshot_data))
                result.data = {"path": tmp_path, "format": format,
                               "size": len(screenshot_data), "page_id": target}
        result.duration_ms = (time.time() - start) * 1000
        return result

    async def get_accessibility_snapshot(self, page_id: str = "",
                                          verbose: bool = False) -> CdpResult:
        start = time.time()
        target = page_id or self._state.active_page_id
        target_info = self._pages.get(target, {})
        ws_url = target_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return CdpResult(error="No page available")
        result = await self._send_cdp(ws_url, "Accessibility.getFullAXTree", {})
        if not result.success:
            return result
        nodes_data = result.data
        if isinstance(nodes_data, dict):
            nodes_data = nodes_data.get("nodes", [])
        if isinstance(nodes_data, dict) and "result" in nodes_data:
            nodes_data = nodes_data["result"].get("nodes", [])
        root = self._build_ax_tree(nodes_data, verbose)
        result.data = {
            "text": root.to_text() if root else "(empty)",
            "tree": root.to_dict() if root else {},
            "node_count": len(nodes_data),
        }
        result.duration_ms = (time.time() - start) * 1000
        return result

    def _build_ax_tree(self, nodes: List[Dict], verbose: bool = False,
                        parent_id: str = "") -> Optional[AccessibilitySnapshotNode]:
        """Build accessibility tree from flat node list. Returns root node."""
        if not nodes:
            return None
        
        # First pass: find root nodes (no parentId or parentId not in list)
        node_ids = {n.get("nodeId") for n in nodes}
        roots = []
        for n in nodes:
            pid = n.get("parentId", "")
            if not pid or pid not in node_ids:
                roots.append(n)
        
        if not roots:
            # Fallback: use first node as root
            roots = [nodes[0]]
        
        def build_node(n, depth=0):
            if depth > 10:  # Prevent infinite recursion
                return None
            node_id = n.get("nodeId", "")
            role = n.get("role", {}).get("value", "unknown")
            
            # Name is directly in name.value
            name_obj = n.get("name", {})
            name = name_obj.get("value", "") if isinstance(name_obj, dict) else str(name_obj)
            
            # Value is directly in value.value
            value_obj = n.get("value", {})
            value = value_obj.get("value", "") if isinstance(value_obj, dict) else str(value_obj) if value_obj else ""
            
            # Find children
            children = []
            for child_n in nodes:
                if child_n.get("parentId") == node_id:
                    child = build_node(child_n, depth + 1)
                    if child:
                        children.append(child)
            
            return AccessibilitySnapshotNode(node_id, role, name, value, children)
        
        # Build tree from first root
        root = build_node(roots[0])
        return root

    async def _wait_for_page_ready(self, timeout: float = 5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            await self._refresh_pages()
            if self._pages:
                return
            await asyncio.sleep(0.2)

    async def get_console_messages(self, page_id: str = "") -> CdpResult:
        target = page_id or self._state.active_page_id
        target_info = self._pages.get(target, {})
        ws_url = target_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return CdpResult(error="No page available")
        result = await self._send_cdp(ws_url, "Runtime.evaluate", {
            "expression": "console.log('__nexus_console_check__');",
            "returnByValue": True,
        })
        return CdpResult(success=result.success)

    async def new_page(self, url: str = "about:blank") -> CdpResult:
        start = time.time()
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.browser_url}/json/new?{url}")
                if resp.status_code == 200:
                    data = resp.json()
                    pid = data.get("id", str(len(self._pages)))
                    self._pages[pid] = data
                    self._state.active_page_id = pid
                    self._state.pages = list(self._pages.values())
                    self._page_counter += 1
                    return CdpResult(success=True, data={"page_id": pid, "url": url},
                                     duration_ms=(time.time() - start) * 1000)
            return CdpResult(error=f"Failed to create page: {resp.status_code}")
        except Exception as e:
            return CdpResult(error=str(e))

    async def close_page(self, page_id: str = "") -> CdpResult:
        start = time.time()
        target = page_id or self._state.active_page_id
        try:
            import httpx
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(f"{self.browser_url}/json/close/{target}")
                if resp.status_code == 200:
                    self._pages.pop(target, None)
                    if self._state.active_page_id == target:
                        self._state.active_page_id = next(iter(self._pages.keys()), "")
                    return CdpResult(success=True, data={"closed": target},
                                     duration_ms=(time.time() - start) * 1000)
        except Exception as e:
            return CdpResult(error=str(e))

    async def start_performance_trace(self, page_id: str = "") -> CdpResult:
        target = page_id or self._state.active_page_id
        target_info = self._pages.get(target, {})
        ws_url = target_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return CdpResult(error="No page available")
        categories = [
            "-*", "blink.console", "blink.user_timing",
            "devtools.timeline", "disabled-by-default-devtools.screenshot",
            "disabled-by-default-devtools.timeline",
            "disabled-by-default-v8.cpu_profiler",
            "latencyInfo", "loading", "v8.execute", "v8",
        ]
        return await self._send_cdp(ws_url, "Tracing.start", {
            "traceConfig": {
                "recordMode": "recordUntilFull",
                "includedCategories": categories,
                "enableSystrace": False,
            },
            "bufferUsageReportingInterval": 1000,
        })

    async def stop_performance_trace(self, page_id: str = "") -> CdpResult:
        target = page_id or self._state.active_page_id
        target_info = self._pages.get(target, {})
        ws_url = target_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return CdpResult(error="No page available")
        return await self._send_cdp(ws_url, "Tracing.end", {})

    async def click(self, selector: str = "", x: float = 0, y: float = 0,
                     page_id: str = "") -> CdpResult:
        target = page_id or self._state.active_page_id
        target_info = self._pages.get(target, {})
        ws_url = target_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return CdpResult(error="No page available")
        if selector:
            script = f"""(() => {{
                const el = document.querySelector({json.dumps(selector)});
                if (!el) throw new Error('Element not found: {selector}');
                const rect = el.getBoundingClientRect();
                return {{x: rect.x + rect.width/2, y: rect.y + rect.height/2}};
            }})()"""
            result = await self.evaluate_script(script, page_id)
            if result.success:
                pos = result.data
                if isinstance(pos, dict) and "result" in pos:
                    pos = pos["result"].get("value", {})
                x = pos.get("x", x)
                y = pos.get("y", y)
        params = {
            "x": x, "y": y,
            "button": "left",
            "clickCount": 1,
        }
        r1 = await self._send_cdp(ws_url, "Input.dispatchMouseEvent",
                                   {**params, "type": "mousePressed"})
        r2 = await self._send_cdp(ws_url, "Input.dispatchMouseEvent",
                                   {**params, "type": "mouseReleased"})
        return CdpResult(success=r1.success and r2.success,
                         data={"x": x, "y": y})

    async def type_text(self, text: str, selector: str = "", page_id: str = "") -> CdpResult:
        target = page_id or self._state.active_page_id
        target_info = self._pages.get(target, {})
        ws_url = target_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return CdpResult(error="No page available")
        if selector:
            result = await self.evaluate_script(
                f"""(() => {{
                    const el = document.querySelector({json.dumps(selector)});
                    if (!el) throw new Error('Element not found');
                    el.focus();
                    el.value = '';
                    return true;
                }})()""", page_id)
            if not result.success:
                return result
        for char in text:
            await self._send_cdp(ws_url, "Input.dispatchKeyEvent", {
                "type": "keyDown", "text": char,
            })
            await self._send_cdp(ws_url, "Input.dispatchKeyEvent", {
                "type": "keyUp", "text": char,
            })
            await asyncio.sleep(0.01)
        return CdpResult(success=True, data={"chars": len(text)})

    async def get_network_log(self, page_id: str = "") -> CdpResult:
        target = page_id or self._state.active_page_id
        target_info = self._pages.get(target, {})
        ws_url = target_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return CdpResult(error="No page available")
        return await self._send_cdp(ws_url, "Network.getResponseBodyForInterception", {})

    def get_state(self) -> BrowserState:
        return self._state
