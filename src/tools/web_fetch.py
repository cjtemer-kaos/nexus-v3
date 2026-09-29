"""
Web Fetch - Fetch URL content and convert HTML to Markdown.
Based on opencode/internal/llm/tools/fetch.go pattern.

Uses httpx for HTTP requests and markdownify for HTML→MD conversion.
"""

import logging
from typing import Dict

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

import re

try:
    import markdownify
    MARKDOWNIFY_AVAILABLE = True
except ImportError:
    MARKDOWNIFY_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)

USER_AGENT = "SuperNEXUS/2.0 (AI Assistant)"
MAX_CONTENT_LENGTH = 50000
DEFAULT_TIMEOUT = 120

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


import socket
import ipaddress
from urllib.parse import urlparse, urljoin

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
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
                return False
        except ValueError:
            # Resolve DNS to check for private IPs
            import socket
            try:
                addr_info = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
            except socket.gaierror:
                return False  # DNS resolution failed — block for safety
            for info in addr_info:
                ip_str = info[4][0]
                ip = ipaddress.ip_address(ip_str)
                if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved:
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
