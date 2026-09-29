"""
Error Translation - AI-friendly error messages for browser operations.
Absorbed from agent-browser pattern — names cleaned.
"""

import logging
import re
from typing import Dict

logger = logging.getLogger(__name__)

ERROR_TRANSLATIONS: Dict[str, str] = {
    "strict mode violation": "Element matched multiple results — try a more specific selector.",
    "element is not visible": "Element exists but is not visible. Wait for it to render or scroll into view.",
    "element is not attached": "Element was removed from the DOM. The page may have navigated or re-rendered.",
    "element is disabled": "Element exists but is currently disabled or read-only.",
    "element is not editable": "Element is not an editable input field.",
    "no such element": "No element matches the given selector. Check the reference or wait for the element to appear.",
    "navigation timeout": "Page navigation took too long. The server may be slow or the URL may be invalid.",
    "session closed": "Browser session was closed. Reconnect or create a new session.",
    "connection refused": "Could not connect to browser. Ensure the browser is running.",
    "connection reset": "Connection to browser was lost. Reconnect.",
    "target closed": "Target tab or window was closed.",
    "frame detached": "Frame was detached from the page. The page may have navigated.",
    "net::err_name_not_resolved": "DNS resolution failed. Check the URL or network connection.",
    "net::err_connection_refused": "Server refused the connection. The service may be down.",
    "net::err_timed_out": "Connection timed out. The server may be unreachable.",
    "net::err_ssl_protocol_error": "SSL/TLS handshake failed. Certificate may be invalid.",
    "net::err_cert_date_invalid": "SSL certificate has expired or is not yet valid.",
    "net::err_cert_authority_invalid": "SSL certificate authority is not trusted.",
}

TRANSLATION_PATTERNS = [
    (re.compile(r"strict mode violation", re.IGNORECASE), "strict mode violation"),
    (re.compile(r"element is not visible", re.IGNORECASE), "element is not visible"),
    (re.compile(r"element is not attached", re.IGNORECASE), "element is not attached"),
    (re.compile(r"element is (?:currently )?disabled", re.IGNORECASE), "element is disabled"),
    (re.compile(r"element is not editable", re.IGNORECASE), "element is not editable"),
    (re.compile(r"no such (?:element|node)", re.IGNORECASE), "no such element"),
    (re.compile(r"navigation timeout", re.IGNORECASE), "navigation timeout"),
    (re.compile(r"session (?:is )?closed", re.IGNORECASE), "session closed"),
    (re.compile(r"connection refused", re.IGNORECASE), "connection refused"),
    (re.compile(r"connection reset", re.IGNORECASE), "connection reset"),
    (re.compile(r"target closed", re.IGNORECASE), "target closed"),
    (re.compile(r"frame detached", re.IGNORECASE), "frame detached"),
    (re.compile(r"err_name_not_resolved", re.IGNORECASE), "net::err_name_not_resolved"),
    (re.compile(r"err_connection_refused", re.IGNORECASE), "net::err_connection_refused"),
    (re.compile(r"err_timed_out", re.IGNORECASE), "net::err_timed_out"),
    (re.compile(r"err_ssl_protocol_error", re.IGNORECASE), "net::err_ssl_protocol_error"),
    (re.compile(r"err_cert_date_invalid", re.IGNORECASE), "net::err_cert_date_invalid"),
    (re.compile(r"err_cert_authority_invalid", re.IGNORECASE), "net::err_cert_authority_invalid"),
]


def translate_error(raw_error: str) -> str:
    """Translate raw browser error to AI-friendly message."""
    if not raw_error:
        return raw_error

    for pattern, key in TRANSLATION_PATTERNS:
        if pattern.search(raw_error):
            translated = ERROR_TRANSLATIONS.get(key)
            if translated:
                return f"{translated} (Original: {raw_error[:120]})"

    return raw_error


def translate_error_short(raw_error: str) -> str:
    """Translate without appending original error."""
    if not raw_error:
        return raw_error

    for pattern, key in TRANSLATION_PATTERNS:
        if pattern.search(raw_error):
            return ERROR_TRANSLATIONS.get(key, raw_error)

    return raw_error
