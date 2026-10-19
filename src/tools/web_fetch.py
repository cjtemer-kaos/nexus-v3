"""
Web Fetch - Fetch URL content and convert HTML to Markdown.

El punto de entrada REAL del harness es la tool `web_fetch` registrada en
`src/api/server.py` (valida SSRF en cada salto de redirect, no sigue redirects
automaticos y envuelve el contenido en un fence de "dato no confiable").

Este modulo provee:
  - `is_safe_url()`: chequeo SSRF (privadas/loopback/link-local/metadata).
  - `web_fetch()`: fetch async con httpx + markdownify (si estan instalados).
  - `HTTPFetcher`: fallback sincronico con urllib, con la MISMA validacion SSRF
    por salto (no usar urllib con redirects automaticos: un 302 puede saltar a
    una IP privada y saltear el chequeo).
"""

import logging
import re
import socket
import ipaddress
import urllib.error
import urllib.request
from typing import Dict
from urllib.parse import urljoin, urlparse

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

try:
    import markdownify
    MARKDOWNIFY_AVAILABLE = True
except ImportError:
    MARKDOWNIFY_AVAILABLE = False

logger = logging.getLogger(__name__)

USER_AGENT = "SuperNEXUS/2.0 (AI Assistant)"
MAX_CONTENT_LENGTH = 50000
DEFAULT_TIMEOUT = 120
MAX_REDIRECTS = 5

# DNS Blocklist - Dominios bloqueados por seguridad
DNS_BLOCKLIST = {
    # Metadata services (SSRF targets)
    "169.254.169.254",  # AWS/GCP/Azure metadata
    "metadata.google.internal",
    "100.100.100.200",  # Alibaba Cloud
    "metadata.oraclecloud.com",
    # Known malicious
    "localhost",
    "0.0.0.0",
}

# RFC 1918 + reserved ranges (aditional blocklist patterns)
BLOCKED_DOMAIN_PATTERNS = [
    ".local", ".internal", ".lan", ".home",
    ".metadata", ".compute.internal",
]


def _ip_is_blocked(ip) -> bool:
    """Privada/loopback/link-local/reservada/multicast/unspecified (incl. IPv4-mapped)."""
    if ip.version == 6 and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return (
        ip.is_private or ip.is_loopback or ip.is_link_local
        or ip.is_reserved or ip.is_multicast or ip.is_unspecified
    )


def is_safe_url(url: str) -> bool:
    """Check if the URL hostname resolves to a safe (non-private, non-loopback) IP address."""
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        if not hostname:
            return False

        # Check DNS blocklist
        if hostname.lower() in DNS_BLOCKLIST:
            return False

        # Check blocked domain patterns
        hostname_lower = hostname.lower()
        for pattern in BLOCKED_DOMAIN_PATTERNS:
            if hostname_lower.endswith(pattern):
                return False

        # Check if the hostname itself is a raw IP first
        try:
            ip = ipaddress.ip_address(hostname)
            return not _ip_is_blocked(ip)
        except ValueError:
            # Resolve DNS to check for private IPs
            try:
                addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            except socket.gaierror:
                return False  # DNS resolution failed — block for safety
            for info in addr_info:
                if _ip_is_blocked(ipaddress.ip_address(info[4][0])):
                    return False
        return True
    except Exception:
        return False


async def web_fetch(
    url: str,
    timeout: int = DEFAULT_TIMEOUT,
    max_length: int = MAX_CONTENT_LENGTH,
) -> Dict:
    """
    Fetch URL content and convert HTML to Markdown with SSRF protection.
    
    Returns:
        Dict with success, content, title, and metadata.
    """
    if not HTTPX_AVAILABLE:
        return {"error": "httpx not installed. Run: pip install httpx"}
    
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    
    try:
        async with httpx.AsyncClient(
            timeout=timeout,
            headers={"User-Agent": USER_AGENT},
        ) as client:
            current_url = url
            max_redirects = 5
            redirect_count = 0
            response = None
            
            while True:
                if not is_safe_url(current_url):
                    return {
                        "error": "Access to private/local addresses is restricted.",
                        "url": current_url
                    }
                
                res = await client.get(current_url, follow_redirects=False)
                
                if res.status_code in (301, 302, 303, 307, 308):
                    redirect_count += 1
                    if redirect_count > max_redirects:
                        return {"error": "Too many redirects", "url": current_url}
                    
                    location = res.headers.get("location")
                    if not location:
                        response = res
                        break
                    current_url = urljoin(current_url, location)
                    continue
                else:
                    response = res
                    break
            
            if response.status_code != 200:
                return {
                    "error": f"HTTP {response.status_code}",
                    "url": url,
                    "status_code": response.status_code,
                }
            
            content_type = response.headers.get("content-type", "")
            
            if "text/html" in content_type:
                html = response.text
                
                if MARKDOWNIFY_AVAILABLE:
                    md = markdownify.markdownify(
                        html,
                        heading_style="ATX",
                        strip=["script", "style", "nav", "footer"],
                    )
                    content = md[:max_length]
                    fmt = "markdown"
                else:
                    text = re.sub(r'<[^>]+>', ' ', html)
                    text = re.sub(r'\s+', ' ', text).strip()
                    content = text[:max_length]
                    fmt = "text"
                
                title_match = re.search(r'<title[^>]*>([^<]+)</title>', html, re.IGNORECASE)
                title = title_match.group(1).strip() if title_match else ""
                
                return {
                    "success": True,
                    "url": url,
                    "title": title,
                    "content": content,
                    "format": fmt,
                    "content_length": len(content),
                    "truncated": len(content) >= max_length,
                }
            
            elif "application/json" in content_type:
                return {
                    "success": True,
                    "url": url,
                    "content": response.text[:max_length],
                    "format": "json",
                    "content_length": len(response.text),
                }
            
            else:
                text = response.text[:max_length]
                return {
                    "success": True,
                    "url": url,
                    "content": text,
                    "format": "text",
                    "content_length": len(text),
                }
    
    except httpx.TimeoutException:
        return {"error": f"Request timed out after {timeout}s", "url": url}
    except httpx.ConnectError:
        return {"error": f"Could not connect to {url}", "url": url}
    except Exception as e:
        return {"error": str(e), "url": url}


def _html_to_text(html: str) -> str:
    """HTML → texto plano legible (sin script/style y sin escapar el markup)."""
    html = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?is)<!--.*?-->", " ", html)
    html = re.sub(r"(?i)<br\s*/?>", "\n", html)
    html = re.sub(r"(?i)</(p|div|li|tr|h[1-6]|section|article|header|footer|table)>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    for entity, char in (("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                         ("&#39;", "'"), ("&apos;", "'"), ("&nbsp;", " ")):
        text = text.replace(entity, char)
    text = re.sub(r"[ \t\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Anula el redirect automatico de urllib: lo hacemos a mano para validar SSRF."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class HTTPFetcher:
    """Fallback sincronico (urllib) con la misma politica SSRF que `web_fetch`.

    Nunca sigue redirects automaticamente: cada `Location` se valida con
    `is_safe_url()` antes del siguiente salto, asi un 302 hacia 127.0.0.1 no
    puede saltearse el chequeo.
    """

    def __init__(self, url: str, timeout: int = DEFAULT_TIMEOUT, max_length: int = MAX_CONTENT_LENGTH):
        self.url = url
        self.timeout = timeout
        self.max_length = max_length
        self._opener = urllib.request.build_opener(_NoRedirectHandler)
        self._opener.addheaders = [("User-Agent", USER_AGENT)]

    def fetch(self) -> Dict:
        """Fetch URL and return text. Nunca lanza: devuelve dict con `error`."""
        current_url = self.url
        if not current_url.startswith(("http://", "https://")):
            current_url = "https://" + current_url
        for _hop in range(MAX_REDIRECTS + 1):
            if not is_safe_url(current_url):
                return {"error": "Acceso a direcciones privadas/locales restringido.", "url": current_url}
            req = urllib.request.Request(current_url, headers={"User-Agent": USER_AGENT})
            try:
                with self._opener.open(req, timeout=self.timeout) as response:
                    raw = response.read(self.max_length * 8)
                    charset = response.headers.get_content_charset() or "utf-8"
                    text = raw.decode(charset, errors="replace")
                    content_type = (response.headers.get("Content-Type") or "").lower()
                    title = ""
                    if "text/html" in content_type:
                        title_match = re.search(r"<title[^>]*>([^<]+)</title>", text, re.IGNORECASE)
                        title = title_match.group(1).strip() if title_match else ""
                        content, fmt = _html_to_text(text), "text"
                    elif "application/json" in content_type:
                        content, fmt = text, "json"
                    else:
                        content, fmt = text, "text"
                    content = content[: self.max_length]
                    return {
                        "success": True,
                        "url": current_url,
                        "title": title,
                        "content": content,
                        "format": fmt,
                        "content_length": len(content),
                        "truncated": len(text) > self.max_length,
                    }
            except urllib.error.HTTPError as e:
                location = e.headers.get("Location") if e.headers else None
                if e.code in (301, 302, 303, 307, 308) and location:
                    current_url = urljoin(current_url, location)
                    continue
                return {"error": f"HTTP {e.code}", "url": current_url, "status_code": e.code}
            except urllib.error.URLError as e:
                return {"error": f"URL error: {e.reason}", "url": current_url}
            except Exception as e:
                return {"error": str(e), "url": current_url}
        return {"error": "Too many redirects", "url": current_url}

