"""Detección de hardware — sin hardcodear recursos (cada PC es distinta).

- detect_vram_gb(): VRAM total vía nvidia-smi; None si no hay GPU NVIDIA.
- detect_cuda(): True si hay GPU NVIDIA utilizable.
- auto_vram_budget(): VRAM total (o RAM/4 si solo CPU) para el router.
- auto_gpu_layers(): 99 con CUDA, 0 sin GPU.
"""
from __future__ import annotations

import shutil
import subprocess


def detect_vram_gb() -> float | None:
    """VRAM total en GB vía nvidia-smi. None si no hay NVIDIA."""
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=10,
        )
        if out.returncode != 0:
            return None
        vals = [float(x.strip()) for x in out.stdout.strip().splitlines() if x.strip()]
        if not vals:
            return None
        return round(max(vals) / 1024, 1)
    except Exception:
        return None


def detect_cuda() -> bool:
    return detect_vram_gb() is not None


def auto_vram_budget() -> float:
    """Presupuesto VRAM para el router: VRAM real, o RAM/4 si solo CPU."""
    vram = detect_vram_gb()
    if vram:
        return vram
    try:
        import psutil  # type: ignore
        ram_gb = psutil.virtual_memory().total / (1024 ** 3)
        return round(max(4.0, ram_gb / 4), 1)
    except Exception:
        return 8.0


def auto_gpu_layers() -> int:
    """99 con CUDA, 0 sin GPU (CPU puro)."""
    return 99 if detect_cuda() else 0
