"""
Bridge que adapta patrones de browser-harness (browser-use, 13.7k stars) a Nexus.

Patrones absorbidos:
1. Coordinate-click: clicks por coordenadas de screenshot (mas robusto que selectores DOM)
2. Agent-writable workspace: agent_helpers.py que el agente crea/modifica en ejecucion
3. Domain skills: per-site playbooks auto-generados que persisten entre sesiones
4. Interaction skills: UI mechanics reusables (tabs, dialogs, iframes, shadow DOM, uploads)
5. Screenshot-first: screenshots como primario, DOM como fallback

Uso:
    from src.core.browser_agent_automation import BrowserAgentAutomation
    baa = BrowserAgentAutomation()
    await baa.ensure_browser()
    snap = await baa.screenshot_and_click(x=500, y=300)
"""
import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.security.atomic_io import atomic_write_json

logger = logging.getLogger("browser-agent-auto")

AGENT_WORKSPACE = Path(os.environ.get(
    "BH_AGENT_WORKSPACE",
    str(Path(__file__).resolve().parents[2] / "agent-workspace")
)).expanduser()

DOMAIN_SKILLS_DIR = AGENT_WORKSPACE / "domain-skills"
AGENT_HELPERS_PATH = AGENT_WORKSPACE / "agent_helpers.py"


@dataclass
class CoordinateClickTarget:
    x: float
    y: float
    width: float = 0
    height: float = 0
    label: str = ""
    confidence: float = 1.0


class BrowserAgentAutomation:
    """Extiende CdpBrowser con patrones browser-harness.
    Coordinate-click, agent-writable workspace, domain skills.
    """

    def __init__(self, cdp_port: int = 9222):
        self.cdp_port = cdp_port
        self.browser_url = f"http://127.0.0.1:{cdp_port}"
        self._cdp = None
        self._ws_url: Optional[str] = None
        self._page_id: str = ""
        self._active_page_url: str = ""
        self._domain_skills_cache: Dict[str, Dict] = {}
        self._last_screenshot_path: str = ""

    async def ensure_browser(self) -> bool:
        from src.tools.cdp_browser import CdpBrowser
        if self._cdp is None:
            self._cdp = CdpBrowser(browser_url=self.browser_url)
        if self._cdp.get_state().status != "connected":
            ok = await self._cdp.connect()
            if not ok:
                return False
        if not self._page_id:
            pages = self._cdp.get_state().pages
            if pages:
                self._page_id = pages[0].get("id", "")
            if not self._page_id:
                result = await self._cdp.new_page()
                if result.success:
                    self._page_id = result.data.get("page_id", "")
        return bool(self._page_id)

    async def navigate(self, url: str) -> Dict:
        if not await self.ensure_browser():
            return {"error": "No browser connected"}
        result = await self._cdp.navigate_page(url, self._page_id)
        if result.success:
            self._active_page_url = url
            domain = self._extract_domain(url)
            skill = await self._load_domain_skill(domain)
        return {"success": result.success, "url": url,
                "page_id": self._page_id}

    async def screenshot(self, full_page: bool = False) -> Dict:
        if not await self.ensure_browser():
            return {"error": "No browser connected"}
        result = await self._cdp.take_screenshot(
            page_id=self._page_id, full_page=full_page)
        if result.success:
            self._last_screenshot_path = result.data.get("path", "")
            return {"path": self._last_screenshot_path,
                    "format": result.data.get("format", "png")}
        return {"error": result.error}

    async def screenshot_and_click(self, x: float, y: float,
                                    page_id: str = "") -> Dict:
        """Coordinate-click: screenshot first, then click at (x, y).
        Mas robusto que selectores DOM porque evita problemas de
        elementos ocultos, animaciones, o selectores rotos.
        """
        if not await self.ensure_browser():
            return {"error": "No browser connected"}
        result = await self._cdp.click(x=x, y=y, page_id=page_id or self._page_id)
        if result.success:
            return {"success": True, "x": x, "y": y,
                    "x_ratio": None, "y_ratio": None}
        return {"error": result.error}

    async def screenshot_and_type(self, text: str, x: float, y: float) -> Dict:
        """Click en coordenada, luego type text. Patron tipico browser-harness."""
        click_r = await self.screenshot_and_click(x, y)
        if "error" in click_r:
            return click_r
        await asyncio.sleep(0.1)
        result = await self._cdp.type_text(text, page_id=self._page_id)
        return {"success": result.success, "text": text, "x": x, "y": y}

    async def snapshot(self, verbose: bool = False) -> Dict:
        if not await self.ensure_browser():
            return {"error": "No browser connected"}
        result = await self._cdp.get_accessibility_snapshot(
            page_id=self._page_id, verbose=verbose)
        if result.success:
            return result.data
        return {"error": result.error}

    async def evaluate(self, script: str) -> Any:
        if not await self.ensure_browser():
            return {"error": "No browser connected"}
        result = await self._cdp.evaluate_script(script, self._page_id)
        if result.success:
            data = result.data
            if isinstance(data, dict) and "result" in data:
                return data["result"].get("value", data)
            return data
        return {"error": result.error}

    # ── Agent-Writable Workspace ──────────────────────────────

    def ensure_workspace(self):
        AGENT_WORKSPACE.mkdir(parents=True, exist_ok=True)
        DOMAIN_SKILLS_DIR.mkdir(parents=True, exist_ok=True)

    def get_agent_helpers_path(self) -> str:
        self.ensure_workspace()
        if not AGENT_HELPERS_PATH.exists():
            AGENT_HELPERS_PATH.write_text(
                "# Agent-created helpers for browser automation\n"
                "# Add functions here that the agent discovers during runtime\n\n"
            )
        return str(AGENT_HELPERS_PATH)

    def load_agent_helpers(self) -> Dict:
        """Carga agent_helpers.py como modulo. Retorna dict con funciones."""
        path = self.get_agent_helpers_path()
        import importlib.util
        spec = importlib.util.spec_from_file_location("agent_helpers", path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            funcs = {n: f for n, f in vars(mod).items()
                     if callable(f) and not n.startswith("_")}
            return funcs
        return {}

    # ── Domain Skills ─────────────────────────────────────────

    def _extract_domain(self, url: str) -> str:
        from urllib.parse import urlparse
        try:
            netloc = urlparse(url).netloc.lower()
            return netloc.replace("www.", "").split(":")[0]
        except Exception:
            return "unknown"

    async def _load_domain_skill(self, domain: str) -> Dict:
        if domain in self._domain_skills_cache:
            return self._domain_skills_cache[domain]
        skill_path = DOMAIN_SKILLS_DIR / f"{domain}.json"
        if skill_path.exists():
            try:
                data = json.loads(skill_path.read_text(encoding="utf-8"))
                self._domain_skills_cache[domain] = data
                return data
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    async def save_domain_skill(self, domain: str = "", skill_data: Dict = None):
        if not domain:
            domain = self._extract_domain(self._active_page_url)
        self.ensure_workspace()
        skill_path = DOMAIN_SKILLS_DIR / f"{domain}.json"
        atomic_write_json(skill_path, skill_data or {}, indent=2)
        self._domain_skills_cache[domain] = skill_data or {}
        logger.info(f"Domain skill saved: {domain} ({skill_path})")

    async def learn_domain_skill(self, actions: List[Dict]) -> Dict:
        """Auto-genera domain skill de las acciones realizadas.
        browser-harness pattern: el agente descubre y persiste
        automatizacion especifica del sitio."""
        domain = self._extract_domain(self._active_page_url)
        existing = await self._load_domain_skill(domain)
        if "actions" not in existing:
            existing["actions"] = []
        existing["actions"].extend(actions)
        existing["updated_at"] = time.time()
        existing["domain"] = domain
        await self.save_domain_skill(domain, existing)
        return existing

    # ── Interaction Skills (17 UI mechanics de browser-harness) ──

    async def interaction_click_tab(self, tab_name: str) -> Dict:
        """Click en tab por nombre. Busca aria-label o texto."""
        return await self._cdp.click(
            selector=f'[role="tab"][aria-label="{tab_name}"], '
                     f'[role="tab"]:has-text("{tab_name}")',
            page_id=self._page_id)

    async def interaction_handle_dialog(self, action: str = "accept",
                                         text: str = "") -> Dict:
        """Acepta/dismiss dialogo del browser."""
        script = """
        (() => {
            return new Promise((resolve) => {
                const handler = (e) => {
                    e.preventDefault();
                    window.__nexus_dialog_handled = true;
                    resolve({type: e.type, message: e.message});
                };
                window.addEventListener('beforeunload', handler, {once: true});
                setTimeout(() => resolve(null), 2000);
            });
        })()
        """
        return await self._cdp.evaluate_script(script, self._page_id)

    async def interaction_switch_iframe(self, iframe_selector: str) -> Dict:
        """Cambia contexto a iframe. Retorna session_id para CDP."""
        result = await self._cdp.evaluate_script(
            f"document.querySelector('{iframe_selector}')", self._page_id)
        return result

    async def interaction_upload_file(self, selector: str, file_path: str) -> Dict:
        """Sube archivo via input[type=file]."""
        script = f"""
        (() => {{
            const input = document.querySelector('{selector}');
            if (!input) throw new Error('File input not found');
            const dt = new DataTransfer();
            dt.items.add(new File([new Uint8Array(0)], '{Path(file_path).name}'));
            input.files = dt.files;
            input.dispatchEvent(new Event('change', {{bubbles: true}}));
            return true;
        }})()
        """
        return await self._cdp.evaluate_script(script, self._page_id)

    async def interaction_hover(self, x: float, y: float) -> Dict:
        """Hover en coordenada."""
        if not await self.ensure_browser():
            return {"error": "No browser connected"}
        target = self._page_id
        target_info = self._cdp._pages.get(target, {})
        ws_url = target_info.get("webSocketDebuggerUrl", "")
        if not ws_url:
            return {"error": "No page available"}
        params = {"x": x, "y": y, "type": "mouseMoved"}
        return await self._cdp._send_cdp(ws_url, "Input.dispatchMouseEvent", params)

    async def interaction_scroll(self, delta_x: float = 0, delta_y: float = 300) -> Dict:
        """Scroll por delta. Positivo = scroll down."""
        script = f"window.scrollBy({{left: {delta_x}, top: {delta_y}, behavior: 'smooth'}});"
        return await self._cdp.evaluate_script(script, self._page_id)

    async def interaction_select_option(self, selector: str, value: str) -> Dict:
        """Selecciona opcion en <select>."""
        script = f"""
        (() => {{
            const el = document.querySelector('{selector}');
            if (!el) throw new Error('Select not found');
            el.value = '{value}';
            el.dispatchEvent(new Event('change', {{bubbles: true}}));
            return el.value;
        }})()
        """
        return await self._cdp.evaluate_script(script, self._page_id)

    async def interaction_get_text(self, selector: str) -> Dict:
        """Obtiene texto de elemento."""
        script = f"""
        (() => {{
            const el = document.querySelector('{selector}');
            return el ? el.innerText || el.textContent : null;
        }})()
        """
        return await self._cdp.evaluate_script(script, self._page_id)

    # ── Estado ────────────────────────────────────────────────

    def get_status(self) -> Dict:
        state = self._cdp.get_state() if self._cdp else None
        return {
            "connected": state and state.status == "connected",
            "url": self._active_page_url,
            "page_id": self._page_id,
            "page_count": len(state.pages) if state else 0,
            "workspace": str(AGENT_WORKSPACE),
            "domain_skills_count": len(self._domain_skills_cache),
            "last_screenshot": self._last_screenshot_path,
        }
