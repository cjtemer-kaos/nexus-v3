"""
ofp — OpenFang Protocol lite: peer-to-peer JSON-RPC with HMAC-SHA256.

Pattern (openfang openfang-wire): TCP between peers, every frame carries
a nonce + HMAC over (nonce + payload), verified in constant time. No TLS
here — assumed to ride on top of WireGuard / Tailscale (NEXUS already
uses Tailscale per CLAUDE.md). The HMAC defends against passive
tampering even if the WG tunnel is somehow misconfigured.

Frame format (single line JSON, newline-delimited):
    {"nonce":"<hex16>","ts":<unix>,"payload":{...},"mac":"<hex64>"}

Auth model:
    Both peers share a pre-distributed PSK (env var NEXUS_OFP_PSK or file).
    HMAC-SHA256(PSK, nonce + str(ts) + canonical_json(payload)) → mac.
    nonces tracked in a 5-min rolling window to defeat replay.

Public API:
    PeerNode(host, port, psk).start()       async server, prints replies
    PeerClient(host, port, psk).call(payload) -> dict | raises

PeerRegistry holds known peers + last-seen and what they reported.
"""
from __future__ import annotations

import asyncio
import hmac
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Optional

logger = logging.getLogger(__name__)


REPLAY_WINDOW_S = 300
TIME_SKEW_S = 60


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _mac(psk: bytes, nonce: str, ts: int, payload: dict) -> str:
    msg = f"{nonce}|{ts}|{_canonical(payload)}".encode("utf-8")
    return hmac.new(psk, msg, hashlib.sha256).hexdigest()


def _verify_mac(psk: bytes, nonce: str, ts: int, payload: dict, mac: str) -> bool:
    expected = _mac(psk, nonce, ts, payload)
    return hmac.compare_digest(expected, mac)


def _now_ok(ts: int) -> bool:
    diff = abs(time.time() - ts)
    return diff <= TIME_SKEW_S + REPLAY_WINDOW_S


def load_psk() -> bytes:
    """Read PSK from env or file. Raises if neither set."""
    raw = os.environ.get("NEXUS_OFP_PSK")
    if raw:
        return raw.encode("utf-8") if not raw.startswith("0x") else bytes.fromhex(raw[2:])
    from pathlib import Path
    f = Path.home() / ".nexus" / "ofp_psk"
    if f.exists():
        return f.read_bytes().strip()
    raise RuntimeError("NEXUS_OFP_PSK unset and ~/.nexus/ofp_psk missing")


def generate_psk() -> bytes:
    """Return a fresh 32-byte PSK suitable for both endpoints."""
    return secrets.token_bytes(32)


@dataclass
class PeerInfo:
    addr: str
    last_seen: float = 0.0
    last_payload: Dict[str, Any] = field(default_factory=dict)


class PeerRegistry:
    def __init__(self):
        self._peers: Dict[str, PeerInfo] = {}
        self._seen_nonces: Dict[str, float] = {}  # nonce -> ts

    def note(self, addr: str, payload: dict):
        self._peers[addr] = PeerInfo(addr=addr, last_seen=time.time(), last_payload=payload)

    def is_replay(self, nonce: str) -> bool:
        # GC stale
        now = time.time()
        if len(self._seen_nonces) > 5000:
            cutoff = now - REPLAY_WINDOW_S
            self._seen_nonces = {n: t for n, t in self._seen_nonces.items() if t > cutoff}
        if nonce in self._seen_nonces:
            return True
        self._seen_nonces[nonce] = now
        return False

    def snapshot(self) -> dict:
        return {addr: {"last_seen": p.last_seen, "last_payload": p.last_payload}
                for addr, p in self._peers.items()}


registry = PeerRegistry()


class OFPError(Exception):
    pass


class PeerNode:
    """Async TCP server. Calls `handler(peer_addr, payload)` per frame and
    sends the reply back as a frame on the same connection."""

    def __init__(self, host: str, port: int, psk: bytes,
                 handler: Optional[Callable[[str, dict], dict]] = None):
        self.host, self.port, self.psk = host, port, psk
        self.handler = handler or (lambda addr, p: {"echo": p})
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self):
        self._server = await asyncio.start_server(self._on_conn, self.host, self.port)
        logger.info(f"OFP node listening on {self.host}:{self.port}")

    async def stop(self):
        if self._server:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def _on_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        peer = writer.get_extra_info("peername")
        addr = f"{peer[0]}:{peer[1]}" if peer else "?"
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                try:
                    frame = json.loads(line.decode("utf-8"))
                    nonce = frame["nonce"]; ts = int(frame["ts"])
                    payload = frame["payload"]; mac = frame["mac"]
                except Exception as e:
                    await self._send_err(writer, f"bad frame: {e}"); continue
                if not _now_ok(ts):
                    await self._send_err(writer, "stale timestamp"); continue
                if registry.is_replay(nonce):
                    await self._send_err(writer, "replay nonce"); continue
                if not _verify_mac(self.psk, nonce, ts, payload, mac):
                    await self._send_err(writer, "bad mac"); continue
                registry.note(addr, payload)
                try:
                    reply = self.handler(addr, payload)
                    await self._send(writer, reply)
                except Exception as e:
                    await self._send_err(writer, f"handler error: {e}")
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _send(self, writer: asyncio.StreamWriter, payload: dict):
        nonce = secrets.token_hex(8)
        ts = int(time.time())
        mac = _mac(self.psk, nonce, ts, payload)
        line = json.dumps({"nonce":nonce,"ts":ts,"payload":payload,"mac":mac}) + "\n"
        writer.write(line.encode("utf-8"))
        await writer.drain()

    async def _send_err(self, writer, msg: str):
        await self._send(writer, {"error": msg})


class PeerClient:
    def __init__(self, host: str, port: int, psk: bytes, timeout: float = 10.0):
        self.host, self.port, self.psk, self.timeout = host, port, psk, timeout

    async def call(self, payload: dict) -> dict:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port), timeout=self.timeout)
        try:
            nonce = secrets.token_hex(8); ts = int(time.time())
            mac = _mac(self.psk, nonce, ts, payload)
            line = json.dumps({"nonce":nonce,"ts":ts,"payload":payload,"mac":mac}) + "\n"
            writer.write(line.encode("utf-8")); await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=self.timeout)
            frame = json.loads(raw.decode("utf-8"))
            # Server's reply is also signed — verify
            if not _verify_mac(self.psk, frame["nonce"], int(frame["ts"]),
                               frame["payload"], frame["mac"]):
                raise OFPError("server reply bad mac")
            return frame["payload"]
        finally:
            writer.close()
            try: await writer.wait_closed()
            except Exception: pass
