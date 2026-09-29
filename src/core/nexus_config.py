"""
nexus_config.py — Configuración centralizada de red para SuperNEXUS.

Single source of truth para el puerto y URL del servidor NEXUS.
Todos los módulos deben importar desde aquí en lugar de hardcodear.
"""

import os

_DEFAULT_PORT = 9000
_port: int = _DEFAULT_PORT

def get_port() -> int:
    """Retorna el puerto actual. Prioridad: set_port() > NEXUS_PORT env > 9000."""
    global _port
    if _port != _DEFAULT_PORT:
        return _port
    return int(os.environ.get("NEXUS_PORT", str(_DEFAULT_PORT)))

def set_port(port: int):
    """Fija el puerto en tiempo de ejecución (lo llama server.py al arrancar)."""
    global _port
    _port = port

def get_host() -> str:
    """Retorna el host por defecto para URLs locales."""
    return os.environ.get("NEXUS_HOST", "localhost")

def get_nexus_url(host: str | None = None) -> str:
    """Retorna la URL base del servidor NEXUS.
    
    Args:
        host: Host a usar (default: get_host())
    Returns:
        URL string como "http://localhost:9000"
    """
    h = host or get_host()
    return f"http://{h}:{get_port()}"

def get_ws_url(host: str | None = None) -> str:
    """Retorna la URL WebSocket del servidor NEXUS."""
    h = host or get_host()
    return f"ws://{h}:{get_port()}"
