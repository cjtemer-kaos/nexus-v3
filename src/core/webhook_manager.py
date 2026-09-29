"""
Webhook Manager - SuperNEXUS v2
HTTP POST outgoing webhooks con HMAC-SHA256 signing, SSRF protection.
Absorbed from odysseus/src/webhook_manager.py — names cleaned.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

ALLOWED_EVENTS = frozenset({
    "session.created",
    "chat.completed",
    "chat.message",
    "webhook.test",
    "task.completed",
    "email.received",
})

_PRIVATE_NETWORKS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
]

DATA_DIR = Path(__import__("os").environ.get("NEXUS_DATA", Path.home() / ".nexus")) / "webhooks"
DATA_DIR.mkdir(parents=True, exist_ok=True)
WEBHOOKS_FILE = DATA_DIR / "webhooks.json"


def _utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _ip_is_private(addr) -> bool:
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return (
        addr.is_private or addr.is_loopback or addr.is_link_local
        or addr.is_reserved or addr.is_multicast or addr.is_unspecified
    )


def _resolve_hostname_ips(hostname: str) -> list:
    import socket
    try:
        infos = socket.getaddrinfo(hostname, None)
    except Exception:
        return []
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return out


def _is_private_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").strip()
        if not hostname:
            return True
        h_lower = hostname.lower()
        if h_lower in ("localhost", "0.0.0.0", "metadata.google.internal", "metadata"):
            return True
        if h_lower.endswith((".local", ".internal", ".lan", ".intranet", ".localhost")):
            return True
        try:
            return _ip_is_private(ipaddress.ip_address(hostname))
        except ValueError:
            pass
        addrs = _resolve_hostname_ips(hostname)
        if not addrs:
            return True
        return any(_ip_is_private(a) for a in addrs)
    except ValueError:
        return True


def validate_webhook_url(url: str) -> str:
    url = url.strip()
    if len(url) > 2048:
        raise ValueError("URL too long (max 2048)")
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("URL must use http or https")
    if not parsed.hostname:
        raise ValueError("URL must have a hostname")
    if _is_private_url(url):
        raise ValueError("URL must not point to private/internal addresses")
    return url


def sanitize_error(error: str, max_len: int = 200) -> str:
    cleaned = re.sub(r'\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}(:\d+)?', '[redacted]', error)
    cleaned = re.sub(r'https?://[^\s/]+', '[redacted-url]', cleaned)
    return cleaned[:max_len]


class Webhook:
    def __init__(self, data: Dict):
        self.id: str = data.get("id", "")
        self.url: str = data.get("url", "")
        self.events: str = data.get("events", "webhook.test")
        self.secret: str = data.get("secret", "")
        self.is_active: bool = data.get("is_active", True)
        self.last_triggered_at: Optional[float] = data.get("last_triggered_at")
        self.last_status_code: Optional[int] = data.get("last_status_code")
        self.last_error: Optional[str] = data.get("last_error")
        self.created_at: float = data.get("created_at", time.time())

    def to_dict(self) -> Dict:
        return {
            "id": self.id, "url": self.url, "events": self.events,
            "is_active": self.is_active, "last_triggered_at": self.last_triggered_at,
            "last_status_code": self.last_status_code, "last_error": self.last_error,
            "created_at": self.created_at,
        }


class WebhookManager:
    """Gestor de outgoing webhooks con HMAC-SHA256 signing y SSRF protection."""

    def __init__(self):
        self.webhooks: Dict[str, Webhook] = {}
        self._load()

    def _load(self):
        try:
            if WEBHOOKS_FILE.exists():
                data = json.loads(WEBHOOKS_FILE.read_text(encoding="utf-8"))
                for w in data.get("webhooks", []):
                    wh = Webhook(w)
                    self.webhooks[wh.id] = wh
        except Exception as e:
            logger.error(f"Error loading webhooks: {e}")

    def _save(self):
        try:
            WEBHOOKS_FILE.write_text(json.dumps({
                "webhooks": [w.to_dict() for w in self.webhooks.values()]
            }, indent=2, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            logger.error(f"Error saving webhooks: {e}")

    def add(self, url: str, events: str = "webhook.test", secret: str = "") -> Webhook:
        import uuid
        validate_webhook_url(url)
        wh = Webhook({
            "id": str(uuid.uuid4())[:8],
            "url": url, "events": events, "secret": secret,
            "created_at": time.time(),
        })
        self.webhooks[wh.id] = wh
        self._save()
        return wh

    def remove(self, webhook_id: str) -> bool:
        if webhook_id in self.webhooks:
            del self.webhooks[webhook_id]
            self._save()
            return True
        return False

    def list_webhooks(self) -> List[Dict]:
        return [w.to_dict() for w in self.webhooks.values()]

    def fire_and_forget(self, event: str, payload: dict):
        if event not in ALLOWED_EVENTS:
            return
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.fire(event, payload))
        except RuntimeError:
            pass

    async def fire(self, event: str, payload: dict):
        if event not in ALLOWED_EVENTS:
            return
        matching = [w for w in self.webhooks.values()
                    if w.is_active and event in w.events.split(",")]
        for wh in matching:
            asyncio.create_task(self._deliver(wh, event, payload))

    async def _deliver(self, wh: Webhook, event: str, payload: dict):
        try:
            validate_webhook_url(wh.url)
        except ValueError as e:
            logger.warning(f"Webhook {wh.id} invalid URL: {e}")
            return

        try:
            import httpx
        except ImportError:
            logger.error("httpx not installed")
            return

        body = json.dumps({"event": event, "timestamp": _utcnow().isoformat(), "data": payload})
        headers = {
            "Content-Type": "application/json",
            "X-Event": event,
            "User-Agent": "SuperNEXUS-Webhook/1.0",
        }
        if wh.secret:
            sig = hmac.new(wh.secret.encode(), body.encode(), hashlib.sha256).hexdigest()
            headers["X-Signature"] = sig

        try:
            async with httpx.AsyncClient(timeout=10, follow_redirects=False) as client:
                resp = await client.post(wh.url, content=body, headers=headers)
                wh.last_triggered_at = time.time()
                wh.last_status_code = resp.status_code
                wh.last_error = None
                self._save()
        except Exception as e:
            logger.warning(f"Webhook {wh.id} delivery failed")
            wh.last_triggered_at = time.time()
            wh.last_status_code = None
            wh.last_error = sanitize_error(str(e))
            self._save()

    async def deliver_test(self, webhook_id: str):
        wh = self.webhooks.get(webhook_id)
        if wh:
            await self._deliver(wh, "webhook.test", {"message": "Test ping"})

    def get_status(self) -> Dict:
        return {
            "total": len(self.webhooks),
            "active": sum(1 for w in self.webhooks.values() if w.is_active),
        }
