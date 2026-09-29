"""Gestor llama.cpp autocontenido — sin paths hardcodeados.

Todo se resuelve contra NEXUS_ROOT (variable de entorno o raíz del proyecto):
- Binario: <root>/vendor/llama.cpp/llama-server.exe
- Modelos: rutas relativas de config/models.yaml
- Al arrancar el backend: levanta director (:8080) + embeddings (:8001).
- Swap bajo demanda en :8080 para el resto de chat models.
- Al apagar: mata solo los procesos que lanzó este manager.
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
import urllib.request
import json
from pathlib import Path


def project_root() -> Path:
    env = os.environ.get("NEXUS_ROOT", "")
    if env:
        return Path(env).expanduser().resolve()
    # src/providers/llama_manager.py -> parents[2] = raíz del proyecto
    return Path(__file__).parents[2].resolve()


def load_models_catalog() -> dict:
    try:
        import yaml  # type: ignore
        cfg_path = project_root() / "config" / "models.yaml"
        with open(cfg_path, encoding="utf-8") as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}


def server_binary() -> Path:
    root = project_root()
    cat = load_models_catalog()
    vendor = cat.get("vendor", {}) if isinstance(cat, dict) else {}
    rel = vendor.get("server_exe", "llama-server.exe")
    vdir = vendor.get("dir", "./vendor/llama.cpp")
    return (root / vdir / rel).resolve()


def model_entry(model_id: str) -> dict | None:
    cat = load_models_catalog()
    models = cat.get("models", {}) if isinstance(cat, dict) else {}
    return models.get(model_id)


def model_path(model_id: str) -> Path | None:
    e = model_entry(model_id)
    if not e:
        return None
    p = Path(e["path"])
    if not p.is_absolute():
        p = (project_root() / p).resolve()
    return p


def _port_open(port: int) -> bool:
    import socket
    s = socket.socket()
    s.settimeout(0.5)
    try:
        s.connect(("127.0.0.1", port))
        s.close()
        return True
    except OSError:
        return False


def _model_loaded(port: int) -> str:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/v1/models", timeout=3) as r:
            data = json.load(r)
        items = data.get("data") or []
        if items:
            return items[0].get("id", "")
    except Exception:
        pass
    return ""


# Procesos lanzados por este manager: (proc, puerto) para matarlos por puerto al apagar/swap.
_launched: list[tuple[subprocess.Popen, int]] = []

# Último uso por puerto (para liberación automática por idle)
_last_use: dict[int, float] = {}
_watcher_started: bool = False
_swap_lock = threading.Lock()
_swap_locks: dict[int, threading.Lock] = {}


def touch(port: int) -> None:
    """Marca actividad en un puerto (resetea el contador de idle)."""
    _last_use[port] = time.time()


def _idle_timeout() -> int:
    cat = load_models_catalog()
    try:
        return int(cat.get("swap", {}).get("idle_timeout_s", 30))
    except Exception:
        return 30


def _resident_for_port(port: int) -> str | None:
    """Modelo residente configurado para un puerto (o None)."""
    cat = load_models_catalog()
    servers = cat.get("servers", {}) if isinstance(cat, dict) else {}
    models = cat.get("models", {}) if isinstance(cat, dict) else {}
    for mid, e in models.items():
        if not isinstance(e, dict):
            continue
        sport = None
        srv = e.get("server", "chat")
        if isinstance(servers.get(srv), dict):
            sport = int(servers[srv].get("port", 0))
        if sport == port and e.get("resident"):
            return mid
    # fallback: default_model del servidor
    for name, s in servers.items():
        if isinstance(s, dict) and int(s.get("port", 0)) == port:
            return s.get("default_model")
    return None


def _idle_watcher_loop() -> None:
    """Hilo fondo: si un puerto chat lleva idle>timeout con modelo no-residente,
    vuelve al residente (libera VRAM del modelo pesado)."""
    while True:
        try:
            time.sleep(10)
            timeout = _idle_timeout()
            now = time.time()
            cat = load_models_catalog()
            servers = cat.get("servers", {}) if isinstance(cat, dict) else {}
            for name, s in servers.items():
                if not isinstance(s, dict):
                    continue
                port = int(s.get("port", 0))
                if not port or name == "embed":
                    continue  # embeddings siempre residente (liviano)
                last = _last_use.get(port, now)
                if now - last < timeout:
                    continue
                resident = _resident_for_port(port)
                if not resident:
                    continue
                cur = _model_loaded(port)
                want = model_path(resident)
                if cur and want and Path(cur).resolve() == want.resolve():
                    continue  # ya está el residente
                if not _port_open(port):
                    continue
                ensure_model(resident, port, timeout=180)
                touch(port)
        except Exception:
            time.sleep(10)


def start_idle_watcher() -> None:
    """Inicia el hilo de liberación por idle (una sola vez)."""
    global _watcher_started
    if _watcher_started:
        return
    _watcher_started = True
    import threading
    t = threading.Thread(target=_idle_watcher_loop, daemon=True, name="llama-idle")
    t.start()


def _build_cmd(model_id: str, port: int) -> list[str] | None:
    cat = load_models_catalog()
    e = model_entry(model_id)
    if not e:
        return None
    mp = model_path(model_id)
    if not mp or not mp.exists():
        return None
    swap = cat.get("swap", {}) if isinstance(cat, dict) else {}
    ctx = str(e.get("ctx", cat.get("servers", {}).get("chat", {}).get("ctx_default", 32768)))
    _ngl = swap.get("n_gpu_layers", "auto")
    if isinstance(_ngl, str) and _ngl.lower() == "auto":
        try:
            from src.core.hardware import auto_gpu_layers
            _ngl = auto_gpu_layers()
        except Exception:
            _ngl = 99
    cmd = [
        str(server_binary()), "-m", str(mp),
        "--host", "127.0.0.1", "--port", str(port),
        "--ctx-size", ctx,
        "--n-gpu-layers", str(_ngl),
    ]
    mmproj = e.get("mmproj")
    if mmproj:
        mmp = Path(mmproj)
        if not mmp.is_absolute():
            mmp = (project_root() / mmp).resolve()
        if mmp.exists():
            cmd += ["--mmproj", str(mmp)]
    if e.get("embedding"):
        # Modelos de embeddings: flag dedicada, sin parallel/flash (rompen el modo embedding)
        cmd += ["--embedding"]
    else:
        for a in swap.get("extra_args", []) or []:
            cmd.append(str(a))
    return cmd


def ensure_model(model_id: str, port: int, timeout: int = 120) -> bool:
    """Asegura que `model_id` esté cargado en `port`. True si listo. Serializa swaps por puerto."""
    # Lock por puerto para evitar double-spawn concurrente
    plock = _swap_locks.get(port)
    if plock is None:
        with _swap_lock:
            plock = _swap_locks.get(port)
            if plock is None:
                plock = threading.Lock()
                _swap_locks[port] = plock
    with plock:
        want = model_path(model_id)
        if not want or not want.exists():
            return False
        cur = _model_loaded(port)
        if cur and Path(cur).resolve() == want.resolve():
            touch(port)
            return True
        # matar nuestros procesos de ESE puerto + cualquier otro proceso que lo ocupe
        # (el swap puede venir de otro proceso Python; el puerto manda)
        stop_managed(port=port)
        _kill_port_holder(port)
        time.sleep(2)
        cmd = _build_cmd(model_id, port)
        if not cmd or not server_binary().exists():
            return False
        log_dir = project_root() / "data" / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        logf = open(log_dir / f"llama-{port}.log", "a", encoding="utf-8")
        creation = 0x08000000 if sys.platform == "win32" else 0
        proc = subprocess.Popen(cmd, creationflags=creation, stdout=logf, stderr=subprocess.STDOUT, cwd=str(project_root()))
        _launched.append((proc, port))
        for _ in range(timeout):
            time.sleep(1)
            cur2 = _model_loaded(port)
            if cur2 and Path(cur2).resolve() == want.resolve():
                touch(port)
                return True
            if proc.poll() is not None:
                return False
        return False


def ensure_residents(timeout_each: int = 180) -> dict[str, bool]:
    """Levanta director (:8080) + embeddings (:8001) según models.yaml."""
    cat = load_models_catalog()
    servers = cat.get("servers", {}) if isinstance(cat, dict) else {}
    chat_port = int(servers.get("chat", {}).get("port", 8080))
    embed_port = int(servers.get("embed", {}).get("port", 8001))
    out: dict[str, bool] = {}
    # embeddings primero (liviano)
    out["nomic-embed-text"] = ensure_model("nomic-embed-text", embed_port, timeout=timeout_each)
    out["nexus-director-v6"] = ensure_model("nexus-director-v6", chat_port, timeout=timeout_each)
    return out


def _kill_port_holder(port: int) -> None:
    """Mata el proceso que escucha en `port` (sea nuestro o no). Necesario para swap entre procesos."""
    try:
        import psutil  # type: ignore
    except Exception:
        return
    for conn in psutil.net_connections(kind="tcp"):
        try:
            if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port and conn.pid:
                if conn.pid == os.getpid():
                    continue
                psutil.Process(conn.pid).terminate()
        except Exception:
            continue
    # espera a que libere
    for _ in range(20):
        time.sleep(0.5)
        busy = False
        try:
            for conn in psutil.net_connections(kind="tcp"):
                if conn.status == "LISTEN" and conn.laddr and conn.laddr.port == port:
                    busy = True
                    break
        except Exception:
            break
        if not busy:
            break


def stop_managed(port: int | None = None) -> None:
    """Mata solo los procesos lanzados por este manager (opcionalmente solo un puerto)."""
    targets = [t for t in _launched if port is None or t[1] == port]
    for p, _ in targets:
        try:
            p.terminate()
        except Exception:
            pass
    # espera corta + kill
    end = time.time() + 5
    while time.time() < end and any(p.poll() is None for p, _ in targets):
        time.sleep(0.5)
    for p, _ in targets:
        try:
            if p.poll() is None:
                p.kill()
        except Exception:
            pass
    for t in targets:
        if t in _launched:
            _launched.remove(t)


def status() -> dict:
    cat = load_models_catalog()
    servers = cat.get("servers", {}) if isinstance(cat, dict) else {}
    out: dict = {
        "vendor_binary": str(server_binary()),
        "vendor_exists": server_binary().exists(),
        "root": str(project_root()),
        "servers": {},
    }
    for name, s in servers.items():
        port = int(s.get("port", 0))
        out["servers"][name] = {
            "port": port,
            "listening": _port_open(port),
            "model": _model_loaded(port),
        }
    return out
