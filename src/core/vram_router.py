import asyncio
import logging
import subprocess

logger = logging.getLogger("vram_router")

_NVIDIA_SMI_CACHE = {"free_gb": 0.0, "ts": 0.0}
_CACHE_TTL = 5.0


def _get_vram_free_gb() -> float:
    now = asyncio.get_event_loop().time() if hasattr(asyncio, "get_event_loop") else 0
    cached = _NVIDIA_SMI_CACHE
    if now - cached["ts"] < _CACHE_TTL:
        return cached["free_gb"]

    try:
        r = subprocess.run(
            [
                "nvidia-smi",
                "--query-gpu=memory.free",
                "--format=csv,noheader,nounits",
            ],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if r.returncode == 0:
            free_mib = float(r.stdout.strip().split("\n")[0])
            free_gb = free_mib / 1024
            cached["free_gb"] = free_gb
            cached["ts"] = asyncio.get_event_loop().time()
            return free_gb
    except Exception as e:
        logger.debug(f"nvidia-smi failed: {e}")

    return 0.0


def decide_num_gpu(
    model: str,
    models_on_gpu: tuple = ("qwen2.5-coder:7b",),
    vram_threshold_gb: float = 5.0,
) -> int:
    if model in ("nemotron-3-nano:4b", "qwen2.5:0.5b", "nomic-embed-text"):
        return 0

    if model == "qwen2.5-coder:7b":
        free_gb = _get_vram_free_gb()
        if free_gb >= vram_threshold_gb:
            logger.info(f"VRAM: {free_gb:.1f}GB free >= {vram_threshold_gb}GB, qwen2.5-coder -> GPU")
            return -1
        logger.info(f"VRAM: {free_gb:.1f}GB free < {vram_threshold_gb}GB, qwen2.5-coder -> CPU")
        return 0

    return -1
