"""
Security Utilities - SuperNEXUS v2
Absorbed from Odysseus: url_safety, secret_storage, settings_scrub
"""

import ipaddress
import logging
import os
import socket
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from urllib.parse import urlparse

logger = logging.getLogger(__name__)


# ==================== URL SAFETY (SSRF Hardening) ====================

ALLOWED_SCHEMES = ("http", "https")


def _default_resolver(host: str) -> List[str]:
    return [info[4][0] for info in socket.getaddrinfo(host, None)]


def _classify_ip(ip: ipaddress._BaseAddress, *, block_private: bool = False) -> Optional[str]:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    if ip.is_link_local:
        return f"link-local blocked (SSRF risk): {ip}"
    if ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return f"disallowed address: {ip}"
    if block_private and (ip.is_private or ip.is_loopback):
        return f"private/loopback blocked: {ip}"
    return None


def check_outbound_url(
    url: str,
    *,
    block_private: bool = False,
    resolver: Optional[Callable[[str], List[str]]] = None,
) -> Tuple[bool, str]:
    """Validar URL outbound antes de hacer request. Retorna (ok, reason)."""
    if not url or not url.strip():
        return False, "URL is required"
    try:
        parsed = urlparse(url.strip())
    except Exception as e:
        return False, f"unparseable URL: {e}"

    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        return False, f"scheme must be http/https, got '{parsed.scheme or '(none)'}'"
    host = parsed.hostname
    if not host:
        return False, "URL has no host"

    resolve = resolver or _default_resolver
    try:
        raw_ips = resolve(host)
    except Exception as e:
        return False, f"host does not resolve: {e}"
    if not raw_ips:
        return False, "host does not resolve"

    for raw in raw_ips:
        try:
            ip = ipaddress.ip_address(raw.split("%")[0])
        except ValueError:
            continue
        reason = _classify_ip(ip, block_private=block_private)
        if reason:
            return False, reason
    return True, "ok"


# ==================== SECRET STORAGE (Fernet Encryption) ====================

_PREFIX = "enc:"
_fernet = None


def _get_fernet():
    global _fernet
    if _fernet is not None:
        return _fernet
    try:
        from cryptography.fernet import Fernet
    except ImportError:
        logger.warning("cryptography no instalado — secret_storage deshabilitado")
        return None

    key_path = Path(os.environ.get("NEXUS_DATA", Path.home() / ".nexus")) / ".app_key"
    if key_path.exists():
        key = key_path.read_bytes()
    else:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key = Fernet.generate_key()
        key_path.write_bytes(key)
        try:
            os.chmod(key_path, 0o600)
        except Exception:
            pass
        logger.info(f"Generated app key: {key_path}")

    _fernet = Fernet(key)
    return _fernet


def encrypt(plaintext: str) -> str:
    """Encriptar string. Idempotente: valores ya encriptados pasan sin cambio."""
    if not plaintext:
        return plaintext or ""
    if plaintext.startswith(_PREFIX):
        return plaintext
    f = _get_fernet()
    if f is None:
        return plaintext
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return _PREFIX + token


def decrypt(value: str) -> str:
    """Desencriptar valor con prefijo enc:. Plaintext pasa sin cambio."""
    if not value:
        return value or ""
    if not value.startswith(_PREFIX):
        return value
    f = _get_fernet()
    if f is None:
        return value
    try:
        return f.decrypt(value[len(_PREFIX):].encode("ascii")).decode("utf-8")
    except Exception:
        logger.error("Decrypt failed — wrong key or corrupt token")
        return ""


def is_encrypted(value: str) -> bool:
    return bool(value) and value.startswith(_PREFIX)


# ==================== SETTINGS SCRUB (Secret Masking) ====================

_SECRET_KEY_PATTERNS = (
    "_api_key", "_apikey", "_password", "_passwd", "_pass", "_pwd",
    "_secret", "_client_secret", "_token", "_access_token", "_refresh_token",
    "_credential", "_credentials", "_key",
)
_SECRET_KEY_ALLOW = ("google_pse_cx",)


def is_secret_key(name: str) -> bool:
    n = (name or "").lower()
    if n in _SECRET_KEY_ALLOW:
        return False
    return any(n.endswith(p) or n == p.lstrip("_") for p in _SECRET_KEY_PATTERNS)


def _scrub_value(key, value):
    if isinstance(value, dict):
        return {
            k: ("" if (is_secret_key(k) and isinstance(v, str) and v)
                else _scrub_value(k, v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_scrub_value(key, item) for item in value]
    if is_secret_key(key) and isinstance(value, str) and value:
        return ""
    return value


def scrub_settings(settings: dict) -> dict:
    """Retorna copia de settings con secretos enmascarados (deep recursive)."""
    if not isinstance(settings, dict):
        return {}
    return {k: _scrub_value(k, v) for k, v in (settings or {}).items()}
