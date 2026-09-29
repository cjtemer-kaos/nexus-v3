from __future__ import annotations
import json
import logging
import shutil
import sys
import asyncio
from pathlib import Path
from typing import Dict, List, Optional, Any

logger = logging.getLogger(__name__)

CONNECTIONS_PATH = Path(__file__).parent.parent.parent / "data" / "connections" / "connections.json"
HIVE_AGENTS_PATH = Path(__file__).parent.parent.parent / "hive_agents.json"
AUTONOMOUS_DIR = Path(__file__).parent.parent / "agents"

CONNECTION_SCHEMA = {
    "name": str,
    "label": str,
    "description": str,
    "type": str,       # cli | api | mcp | autonomous_loop
    "protocol": str,   # subprocess | http | messageboard
    "tags": list,
    "enabled": bool,
}

HEALTH_CHECK_TYPES = {
    "http": lambda e, c: _check_http(e, c.get("expected")),
    "binary": lambda e, c: _check_binary(e),
    "shell": lambda e, c: _check_shell(e),
    "process": lambda e, c: _check_process(e),
}


def load_connections() -> Dict[str, Any]:
    if not CONNECTIONS_PATH.exists():
        return {"version": "1", "connections": []}
    try:
        return json.loads(CONNECTIONS_PATH.read_text(encoding="utf-8"))
    except Exception as e:
        logger.warning(f"Failed to load connections: {e}")
        return {"version": "1", "connections": []}


def save_connections(data: Dict[str, Any]) -> None:
    CONNECTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONNECTIONS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(CONNECTIONS_PATH)


def get_connection(name: str) -> Optional[Dict[str, Any]]:
    data = load_connections()
    for c in data.get("connections", []):
        if c["name"] == name:
            return c
    return None


def add_connection(entry: Dict[str, Any]) -> None:
    data = load_connections()
    existing = [c for c in data["connections"] if c["name"] == entry["name"]]
    if existing:
        for i, c in enumerate(data["connections"]):
            if c["name"] == entry["name"]:
                data["connections"][i] = entry
                break
    else:
        data["connections"].append(entry)
    save_connections(data)
    sync_to_hive_agents()


def remove_connection(name: str) -> bool:
    data = load_connections()
    before = len(data["connections"])
    data["connections"] = [c for c in data["connections"] if c["name"] != name]
    if len(data["connections"]) < before:
        save_connections(data)
        sync_to_hive_agents()
        return True
    return False


def sync_to_hive_agents() -> None:
    """Sync connections.json -> hive_agents.json so the Hive Hub can dispatch."""
    conns = load_connections()
    agents = {}
    for c in conns.get("connections", []):
        if not c.get("enabled", True):
            continue
        name = c["name"]
        ctype = c.get("type", "cli")
        if ctype == "cli":
            entry = {
                "type": "cli",
                "transport": "subprocess",
                "binary": c.get("binary", ""),
                "args": c.get("args", ["{task}"]),
                "cwd": c.get("cwd"),
                "timeout_s": c.get("timeout_s", 120),
                "description": c.get("description", ""),
                "tags": c.get("tags", []),
                "enabled": True,
            }
            if c.get("stdin_json"):
                entry["stdin_json"] = True
            if c.get("env"):
                entry["env"] = c["env"]
            if c.get("response_parser"):
                entry["post_parse"] = c["response_parser"]
            agents[name] = entry
        elif ctype == "api":
            agents[name] = {
                "type": "cli",
                "transport": "subprocess",
                "binary": "curl.exe",
                "args": _build_curl_args(c),
                "timeout_s": c.get("timeout_s", 120),
                "description": c.get("description", ""),
                "tags": c.get("tags", []),
                "enabled": True,
            }
            if c.get("response_parser"):
                agents[name]["post_parse"] = c["response_parser"]
        elif ctype == "mcp":
            agents[name] = {
                "type": "mcp",
                "transport": c.get("protocol", "streamable_http"),
                "url": c.get("url", ""),
                "headers": c.get("headers", {}),
                "default_tool": c.get("default_tool", "system.run"),
                "timeout_s": c.get("timeout_s", 120),
                "description": c.get("description", ""),
                "tags": c.get("tags", []),
                "enabled": True,
            }

    hive = {
        "version": "0.1.0",
        "schema": "hive_agents/v1",
        "description": "Auto-synced from data/connections/connections.json",
        "defaults": {
            "timeout_s": 120,
            "env_passthrough": ["PATH", "SYSTEMROOT", "USERPROFILE", "TEMP", "TMP"],
            "encoding": "utf-8",
            "psk_env": "HIVE_PSK",
        },
        "agents": agents,
    }
    HIVE_AGENTS_PATH.write_text(json.dumps(hive, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info(f"Synced {len(agents)} agents to hive_agents.json")


def _build_curl_args(conn: Dict[str, Any]) -> List[str]:
    args = ["-s", "-m", str(conn.get("timeout_s", 110))]
    method = conn.get("method", "POST").upper()
    if method == "POST":
        args.extend(["-X", "POST"])
    args.extend([conn["url"]])
    for k, v in conn.get("headers", {}).items():
        args.extend(["-H", f"{k}: {v}"])
    body = conn.get("body_template", "{}")
    if "{task}" in body:
        body = body.replace("{task}", "{task}")
    args.extend(["-d", body])
    return args


async def check_health(conn: Dict[str, Any]) -> Dict[str, Any]:
    hc = conn.get("health_check", {})
    htype = hc.get("type", "")
    endpoint = hc.get("endpoint", "")
    if not htype or not endpoint:
        return {"status": "unknown", "reason": "no health check configured"}

    fn = HEALTH_CHECK_TYPES.get(htype)
    if not fn:
        return {"status": "unknown", "reason": f"unknown check type: {htype}"}

    try:
        ok, detail = await fn(endpoint, hc)
        return {"status": "online" if ok else "offline", "detail": detail}
    except Exception as e:
        return {"status": "error", "detail": str(e)}


async def check_all_connections() -> List[Dict[str, Any]]:
    data = load_connections()
    results = []
    for c in data.get("connections", []):
        status = await check_health(c)
        results.append({"name": c["name"], "label": c.get("label", c["name"]), **status})
    return results


async def _check_http(endpoint: str, config: dict) -> tuple:
    try:
        import aiohttp
        async with aiohttp.ClientSession() as s:
            async with s.get(endpoint, timeout=5) as r:
                if r.status == 200:
                    expected = config.get("expected")
                    if expected:
                        body = await r.text()
                        return expected in body, f"HTTP {r.status}"
                    return True, f"HTTP {r.status}"
                return False, f"HTTP {r.status}"
    except ImportError:
        import urllib.request
        try:
            with urllib.request.urlopen(endpoint, timeout=5) as r:
                return r.status == 200, f"HTTP {r.status}"
        except Exception as e:
            return False, str(e)
    except Exception as e:
        return False, str(e)


async def _check_binary(endpoint: str, config: dict = None) -> tuple:
    resolved = shutil.which(endpoint) or Path(endpoint)
    return resolved is not None and (Path(resolved).exists() if isinstance(resolved, Path) else True), str(resolved)


async def _check_shell(endpoint: str, config: dict = None) -> tuple:
    proc = await asyncio.create_subprocess_shell(
        endpoint,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        rc = await asyncio.wait_for(proc.wait(), timeout=5)
        return rc == 0, f"exit code {rc}"
    except asyncio.TimeoutError:
        proc.kill()
        return False, "timeout"


async def _check_process(endpoint: str, config: dict = None) -> tuple:
    name = Path(endpoint).stem.lower() if endpoint else ""
    if sys.platform == "win32":
        proc = await asyncio.create_subprocess_exec(
            "tasklist", "/FI", f"IMAGENAME eq {name}.exe",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        stdout, _ = await proc.communicate()
        return name in stdout.decode().lower(), f"tasklist checked {name}"
    return False, "process check only on Windows"


class ConnectionManager:
    """Manages connection lifecycle: config, sync, health, auto-start loops."""

    def __init__(self):
        self._processes: Dict[str, asyncio.subprocess.Process] = {}
        self._tasks: Dict[str, asyncio.Task] = {}

    async def start_autonomous_loops(self, loop: asyncio.AbstractEventLoop = None) -> int:
        """Start all enabled autonomous_loop connections."""
        data = load_connections()
        started = 0
        for c in data.get("connections", []):
            if c.get("type") != "autonomous_loop":
                continue
            if not c.get("enabled", True):
                continue
            if not c.get("auto_start", False):
                continue
            name = c["name"]
            if name in self._processes:
                continue
            script = c.get("script", "")
            script_path = AUTONOMOUS_DIR / script if not Path(script).is_absolute() else Path(script)
            if not script_path.exists():
                logger.warning(f"auto-start {name}: script not found {script_path}")
                continue
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, str(script_path),
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                self._processes[name] = proc
                started += 1
                logger.info(f"auto-started autonomous loop: {name} (pid={proc.pid})")
            except Exception as e:
                logger.error(f"auto-start {name} failed: {e}")
        return started

    async def stop_autonomous_loop(self, name: str) -> bool:
        proc = self._processes.pop(name, None)
        if proc:
            try:
                proc.kill()
                await proc.wait()
                logger.info(f"stopped autonomous loop: {name}")
            except Exception:
                pass
            return True
        return False

    def get_running_loops(self) -> List[str]:
        alive = []
        for name, proc in list(self._processes.items()):
            if proc.returncode is None:
                alive.append(name)
            else:
                self._processes.pop(name, None)
        return alive

    async def stop_all(self):
        for name in list(self._processes.keys()):
            await self.stop_autonomous_loop(name)
