"""Escritura atomica de archivos de estado.

El router adaptativo y la memoria jerarquica se guardan en JSON. La version
anterior hacia `os.write(fd, ...)` sobre un descriptor crudo y, si algo fallaba,
dejaba el temporal tirado en el directorio del brain. Dos problemas concretos:

- `os.write` puede escribir MENOS bytes de los pedidos (no es un `write` de
  archivo completo). Con un payload grande el archivo quedaba truncado y el
  lector recibia un JSON invalido.
- `except:` pelado atrapaba tambien KeyboardInterrupt/SystemExit y el `tmp`
  nunca se borraba: cada fallo dejaba basura acumulandose.

Ahora se escribe a un temporal en el mismo directorio, se hace fsync y recien
entonces `os.replace`: el lector ve el contenido viejo o el nuevo, nunca una
mezcla. Si falla, el temporal se borra y la excepcion sigue su curso.
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def _atomic_write_bytes(path: str | os.PathLike[str], payload: bytes) -> Path:
    destino = Path(path)
    destino.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=f".{destino.name}.", suffix=".tmp", dir=str(destino.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, destino)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return destino


def atomic_write_text(path: str | os.PathLike[str], text: str,
                      encoding: str = "utf-8") -> Path:
    """Escribe `text` completo o no toca el destino."""
    if not isinstance(text, str):
        raise TypeError(f"text debe ser str, no {type(text).__name__}")
    return _atomic_write_bytes(path, text.encode(encoding))


def atomic_write_json(path: str | os.PathLike[str], data: Any,
                      indent: int = 2) -> Path:
    """Serializa a JSON primero: si `data` no es serializable, el destino no se toca."""
    try:
        payload = json.dumps(data, ensure_ascii=False, indent=indent)
    except (TypeError, ValueError) as e:
        raise TypeError(f"no se pudo serializar a JSON para {path}: {e}") from e
    return _atomic_write_bytes(path, payload.encode("utf-8"))
