"""
cookbook — Hardware scan + Ollama model recommendation + SSH remote detection.

Pattern (odysseus cookbook + llmfit): a new user shouldn't need to know
that qwen2.5-coder needs 5 GB VRAM. They paste their hardware, the
cookbook says "you can run X, Y, Z; don't try W."

Detection is best-effort and crossplatform:
  - CPU count       multiprocessing.cpu_count()
  - RAM             psutil.virtual_memory() if available; /proc/meminfo on Linux
  - Disk            shutil.disk_usage(home)
  - VRAM            nvidia-smi if present; AMD ROCm probe; else 0
  - SSH Remote      nvidia-smi via SSH for remote GPU detection

Recommendations are conservative — RAM headroom assumed for OS + browser,
VRAM headroom for KV cache. Real prod can override via env.
"""
from __future__ import annotations

import logging
import os
import platform
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# NVIDIA nvidia-smi path candidates
NVIDIA_PATH_CANDIDATES = [
    "nvidia-smi",
    "/usr/bin/nvidia-smi",
    "/usr/local/cuda/bin/nvidia-smi",
    "/usr/lib/wsl/lib/nvidia-smi",
]

# SSH config
SSH_PATH_OVERRIDE = os.environ.get("SSH_PATH_OVERRIDE", "")


@dataclass
class Hardware:
    os: str
    arch: str
    cpu_count: int
    ram_gb: float
    free_disk_gb: float
    vram_gb: float
    gpu_name: Optional[str] = None
    gpu_count: int = 0
    gpus: List[Dict] = field(default_factory=list)
    gpu_groups: List[Dict] = field(default_factory=list)
    homogeneous: bool = True
    backend: str = "unknown"
    unified_memory: bool = False
    remote_host: Optional[str] = None


@dataclass
class GPUInfo:
    """Informacion de un GPU individual."""
    index: int
    name: str
    vram_gb: float


def _detect_ram_gb() -> float:
    try:
        import psutil
        return round(psutil.virtual_memory().total / (1024 ** 3), 1)
    except Exception:
        pass
    if platform.system() == "Linux":
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        return round(kb / (1024 ** 2), 1)
        except Exception:
            pass
    return 0.0


def _run_cmd(cmd, timeout: int = 10) -> Optional[str]:
    """Ejecutar comando local o remoto."""
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout.strip()
    except Exception:
        pass
    return None


def _run_ssh_command(
    remote: str,
    ssh_port: Optional[str],
    remote_cmd: str,
    timeout: float = 15,
    connect_timeout: int = 5,
) -> subprocess.CompletedProcess:
    """Ejecutar comando via SSH."""
    argv = ["ssh", "-o", f"ConnectTimeout={connect_timeout}"]
    if SSH_PATH_OVERRIDE:
        argv = ["ssh", "-o", "StrictHostKeyChecking=no"]
    if ssh_port and ssh_port != "22":
        argv.extend(["-p", str(ssh_port)])
    argv.append(remote)
    argv.append(remote_cmd)
    return subprocess.run(argv, timeout=timeout, capture_output=True, text=True)


def _group_gpus(gpus: List[GPUInfo]) -> List[Dict]:
    """Agrupar GPUs identicas por (nombre, vram)."""
    groups = {}
    order = []
    for g in gpus:
        key = (g.name, round(g.vram_gb))
        if key not in groups:
            groups[key] = {
                "name": g.name,
                "vram_each": round(g.vram_gb, 1),
                "count": 0,
                "indices": [],
            }
            order.append(key)
        groups[key]["count"] += 1
        groups[key]["indices"].append(g.index)
    
    out = []
    for key in order:
        grp = groups[key]
        grp["vram_total"] = round(grp["vram_each"] * grp["count"], 1)
        out.append(grp)
    out.sort(key=lambda x: x["vram_total"], reverse=True)
    return out


def _detect_nvidia_remote(
    remote_host: str,
    ssh_port: Optional[str] = None,
) -> Optional[Dict]:
    """Detectar NVIDIA GPUs via SSH en maquina remota."""
    try:
        # Intentar nvidia-smi via SSH
        r = _run_ssh_command(
            remote_host,
            ssh_port,
            "nvidia-smi --query-gpu=memory.total,name --format=csv,noheader,nounits",
            timeout=15,
        )
        if r.returncode != 0 or not r.stdout.strip():
            # Intentar con bash login shell
            r = _run_ssh_command(
                remote_host,
                ssh_port,
                f"bash -lc '{SSH_PATH_OVERRIDE}nvidia-smi --query-gpu=memory.total,name --format=csv,noheader,nounits'",
                timeout=15,
            )
        
        if r.returncode != 0 or not r.stdout.strip():
            return None

        # Parsear output
        gpus = []
        for idx, line in enumerate(r.stdout.strip().split("\n")):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                try:
                    vram_mb = float(parts[0])
                    gpus.append(GPUInfo(index=idx, name=parts[1], vram_gb=vram_mb / 1024.0))
                except ValueError:
                    continue

        if not gpus:
            return None

        total_vram = sum(g.vram_gb for g in gpus)
        groups = _group_gpus(gpus)
        
        return {
            "gpu_name": gpus[0].name,
            "gpu_vram_gb": round(total_vram, 1),
            "gpu_count": len(gpus),
            "gpus": [{"index": g.index, "name": g.name, "vram_gb": g.vram_gb} for g in gpus],
            "gpu_groups": groups,
            "homogeneous": len(groups) <= 1,
            "backend": "cuda",
            "remote_host": remote_host,
        }
    except Exception as e:
        logger.warning(f"SSH GPU detection failed for {remote_host}: {e}")
        return None


def _detect_nvidia_local() -> Optional[Dict]:
    """Detectar NVIDIA GPUs localmente."""
    try:
        r = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.total,name", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            # Intentar con paths absolutos
            for p in NVIDIA_PATH_CANDIDATES:
                try:
                    r = subprocess.run(
                        [p, "--query-gpu=memory.total,name", "--format=csv,noheader,nounits"],
                        capture_output=True, text=True, timeout=5,
                    )
                    if r.returncode == 0 and r.stdout.strip():
                        break
                except Exception:
                    continue
            else:
                return None

        # Parsear output
        gpus = []
        for idx, line in enumerate(r.stdout.strip().split("\n")):
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                try:
                    vram_mb = float(parts[0])
                    gpus.append(GPUInfo(index=idx, name=parts[1], vram_gb=vram_mb / 1024.0))
                except ValueError:
                    continue

        if not gpus:
            return None

        total_vram = sum(g.vram_gb for g in gpus)
        groups = _group_gpus(gpus)
        
        return {
            "gpu_name": gpus[0].name,
            "gpu_vram_gb": round(total_vram, 1),
            "gpu_count": len(gpus),
            "gpus": [{"index": g.index, "name": g.name, "vram_gb": g.vram_gb} for g in gpus],
            "gpu_groups": groups,
            "homogeneous": len(groups) <= 1,
            "backend": "cuda",
        }
    except Exception as e:
        logger.warning(f"Local GPU detection failed: {e}")
        return None


def _detect_amd_local() -> Optional[Dict]:
    """Detectar AMD GPUs via ROCm."""
    try:
        r = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram"],
            capture_output=True, text=True, timeout=5,
        )
        if r.returncode == 0 and "VRAM Total" in r.stdout:
            for line in r.stdout.splitlines():
                if "Total" in line and "bytes" in line.lower():
                    n = "".join(c for c in line if c.isdigit())
                    if n:
                        return {
                            "gpu_name": "AMD ROCm GPU",
                            "gpu_vram_gb": round(int(n) / (1024 ** 3), 1),
                            "gpu_count": 1,
                            "backend": "rocm",
                        }
    except Exception:
        pass
    return None


def scan_hardware(
    remote_host: Optional[str] = None,
    ssh_port: Optional[str] = None,
) -> Hardware:
    """
    Detectar hardware local o remoto via SSH.
    
    Args:
        remote_host: Host SSH para detectar GPU remota
        ssh_port: Puerto SSH (default 22)
    """
    ram = _detect_ram_gb()
    
    # Detectar GPU
    gpu_info = None
    if remote_host:
        gpu_info = _detect_nvidia_remote(remote_host, ssh_port)
    else:
        gpu_info = _detect_nvidia_local()
        if not gpu_info:
            gpu_info = _detect_amd_local()
    
    vram = gpu_info.get("gpu_vram_gb", 0) if gpu_info else 0
    gpu_name = gpu_info.get("gpu_name") if gpu_info else None
    gpu_count = gpu_info.get("gpu_count", 0) if gpu_info else 0
    gpus = gpu_info.get("gpus", []) if gpu_info else []
    gpu_groups = gpu_info.get("gpu_groups", []) if gpu_info else []
    homogeneous = gpu_info.get("homogeneous", True) if gpu_info else True
    backend = gpu_info.get("backend", "unknown") if gpu_info else "unknown"
    unified_memory = gpu_info.get("unified_memory", False) if gpu_info else False
    
    try:
        disk_free = round(shutil.disk_usage(Path.home()).free / (1024 ** 3), 1)
    except Exception:
        disk_free = 0.0
    
    return Hardware(
        os=platform.system(),
        arch=platform.machine(),
        cpu_count=os.cpu_count() or 1,
        ram_gb=ram,
        free_disk_gb=disk_free,
        vram_gb=vram,
        gpu_name=gpu_name,
        gpu_count=gpu_count,
        gpus=gpus,
        gpu_groups=gpu_groups,
        homogeneous=homogeneous,
        backend=backend,
        unified_memory=unified_memory,
        remote_host=remote_host,
    )


# Curated model catalog with approximate footprints.
# (size_gb is on-disk; vram_gb is rough loaded footprint at Q4_K_M).
_MODEL_CATALOG: List[Dict] = [
    # Tiny (CPU OK)
    {"name": "qwen2.5:0.5b",        "size_gb": 0.4, "vram_gb": 0.6,  "use": "draft/speculative", "tier": "tiny"},
    {"name": "qwen2.5:1.5b",        "size_gb": 1.0, "vram_gb": 1.5,  "use": "tasks rápidas",     "tier": "tiny"},
    # Small (any modern GPU)
    {"name": "nemotron-3-nano:4b",  "size_gb": 2.4, "vram_gb": 3.0,  "use": "clasificación/rápido", "tier": "small"},
    {"name": "qwen2.5-coder:7b",       "size_gb": 3.0, "vram_gb": 4.0,  "use": "general/meta/judge",  "tier": "small"},
    # Medium (6 GB VRAM target)
    {"name": "qwen2.5-coder:7b",    "size_gb": 4.7, "vram_gb": 5.5,  "use": "coding/math",         "tier": "medium"},
    {"name": "qwen2.5vl:7b",        "size_gb": 4.7, "vram_gb": 5.5,  "use": "visión",              "tier": "medium"},
    {"name": "deepseek-r1:8b",      "size_gb": 5.0, "vram_gb": 6.0,  "use": "reasoning",           "tier": "medium"},
    {"name": "maternion/lfm2.5:latest", "size_gb": 5.0, "vram_gb": 4.0, "use": "MoE 8.5B active 1B (research/security)", "tier": "medium"},
    # Large (12+ GB VRAM)
    {"name": "qwen2.5:14b",         "size_gb": 9.0, "vram_gb": 11.0, "use": "high quality general", "tier": "large"},
    {"name": "qwen2.5-coder:14b",   "size_gb": 9.0, "vram_gb": 11.0, "use": "high quality coding",  "tier": "large"},
]


async def install_model(model: str) -> Dict:
    """Stream-pull `model` via Ollama. Returns final {ok, model, bytes_total,
    status} or {ok:false, error}. Best-effort; tolerates ollama down."""
    # Validate model is in our catalog (defense vs arbitrary pulls)
    known = {m["name"] for m in _MODEL_CATALOG}
    if model not in known:
        return {"ok": False, "error": f"unknown model: {model}",
                "known": sorted(known)}
    try:
        import httpx
    except ImportError:
        return {"ok": False, "error": "httpx not installed"}
    url = "http://localhost:11434/api/pull"
    bytes_total = 0
    last_status = ""
    try:
        async with httpx.AsyncClient(timeout=600.0) as client:
            async with client.stream("POST", url, json={"name": model, "stream": True}) as r:
                if r.status_code >= 400:
                    return {"ok": False, "error": f"ollama HTTP {r.status_code}"}
                async for line in r.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        import json as _json
                        d = _json.loads(line)
                        last_status = d.get("status", "")
                        if "total" in d:
                            bytes_total = max(bytes_total, int(d.get("total", 0)))
                    except Exception:
                        continue
        # Emit completion event
        try:
            from src.observability.event_stream import emit, EventType
            emit(EventType.SYSTEM_BOOT_READY,  # closest semantic
                 data={"event": "model_pulled", "model": model,
                       "bytes_total": bytes_total, "status": last_status},
                 source="cookbook")
        except Exception:
            pass
        return {"ok": True, "model": model, "bytes_total": bytes_total,
                "status": last_status or "success"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def recommend(hw: Hardware, *, vram_headroom_gb: float = 1.0) -> Dict:
    """Return {recommended: [...], can_run: [...], too_big: [...]} based on hw."""
    available_vram = max(0.0, hw.vram_gb - vram_headroom_gb)
    can_run, too_big = [], []
    for m in _MODEL_CATALOG:
        if m["size_gb"] > hw.free_disk_gb:
            too_big.append({**m, "reason": f"disk: need {m['size_gb']}GB, have {hw.free_disk_gb}"})
            continue
        if m["vram_gb"] > available_vram and hw.vram_gb > 0:
            too_big.append({**m, "reason": f"vram: need {m['vram_gb']}GB, have {available_vram}"})
            continue
        # CPU-only ok for tiny + small only
        if hw.vram_gb == 0 and m["tier"] not in ("tiny", "small"):
            too_big.append({**m, "reason": "no GPU detected; medium+ models too slow on CPU"})
            continue
        can_run.append(m)

    # Recommended subset: 1 per tier, biggest in each that fits.
    recommended = []
    for tier in ("tiny", "small", "medium", "large"):
        tier_models = [m for m in can_run if m["tier"] == tier]
        if tier_models:
            recommended.append(max(tier_models, key=lambda x: x["size_gb"]))
    
    # Build GPU groups info
    gpu_groups_info = []
    for grp in hw.gpu_groups:
        gpu_groups_info.append({
            "name": grp.get("name", "unknown"),
            "count": grp.get("count", 1),
            "vram_each": grp.get("vram_each", 0),
            "vram_total": grp.get("vram_total", 0),
            "indices": grp.get("indices", []),
        })
    
    return {
        "recommended": recommended,
        "can_run": can_run,
        "too_big": too_big,
        "hardware": {
            "os": hw.os, "arch": hw.arch, "cpu_count": hw.cpu_count,
            "ram_gb": hw.ram_gb, "free_disk_gb": hw.free_disk_gb,
            "vram_gb": hw.vram_gb, "gpu_name": hw.gpu_name,
            "gpu_count": hw.gpu_count, "backend": hw.backend,
            "homogeneous": hw.homogeneous, "unified_memory": hw.unified_memory,
            "gpu_groups": gpu_groups_info,
            "remote_host": hw.remote_host,
        },
        "rationale": (
            f"Detected {hw.vram_gb}GB VRAM ({hw.gpu_name or 'no GPU'}), "
            f"{hw.ram_gb}GB RAM, {hw.free_disk_gb}GB free disk. "
            f"Headroom: {vram_headroom_gb}GB VRAM reserved for KV cache."
            + (f" Remote: {hw.remote_host}" if hw.remote_host else "")
        ),
    }
