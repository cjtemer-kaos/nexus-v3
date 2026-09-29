"""
Path Normalization - Cross-platform path handling.
Absorbed from openswarm pattern — handles /mnt/ paths on Windows.
"""

import os
from pathlib import Path


def normalize_path(path_str: str) -> str:
    """Normalize cross-platform paths, converting Linux /mnt/ paths to local relative on Windows."""
    if not path_str or not path_str.strip():
        return path_str

    raw = path_str.strip()

    if os.name != "nt":
        return raw

    if Path("/.dockerenv").exists():
        return raw

    if raw.startswith("/mnt/") or raw == "/mnt":
        parts = raw.split("/")
        if len(parts) >= 4:
            drive = parts[2]
            rest = "/".join(parts[3:])
            normalized = rest.replace("/", os.sep)
            return f"{drive}:{os.sep}{normalized}"
        elif len(parts) == 3:
            drive = parts[2]
            return f"{drive}:\\"

    return raw
