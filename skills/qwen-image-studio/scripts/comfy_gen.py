#!/usr/bin/env python
"""comfy_gen.py — Genera/edita imágenes con Qwen-Image en ComfyUI local.

Sin dependencias externas (solo stdlib). **Path-agnóstico**: no conoce la carpeta de ComfyUI;
se comunica SOLO por su API HTTP (sube la imagen con /upload/image y baja el resultado con /view).

Config por entorno:
  COMFYUI_URL   URL base de ComfyUI (default http://127.0.0.1:8188)

Modos:
  --mode edit  (default): Qwen-Image-Edit-2511 Q3_K_L. Requiere --image.
  --mode t2i            : Qwen-Image 2.1-Turbo GGUF (texto a imagen, 8 pasos / CFG 1).

Ejemplos:
  python comfy_gen.py --check
  python comfy_gen.py --mode edit --image foto.png --prompt "turn her into a gothic vampire, keep her exact face" --negative "fangs, monster, male face, deformed" --steps 20 --cfg 2.5 --prefix julie_vampire
  python comfy_gen.py --mode t2i --prompt "a red apple on a wooden table" --width 1024 --height 1024 --prefix apple

Salida: imprime las rutas de las imágenes generadas (las guarda en --save si se indica).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

HOST = (os.environ.get("COMFYUI_URL") or "http://127.0.0.1:8188").rstrip("/")

EDIT_UNET = "qwen-image-edit-2511-Q3_K_L.gguf"
EDIT_UNET_2511 = EDIT_UNET
T2I_UNET = "qwen-image-2.1-turbo-Q4_K_S.gguf"
CLIP_EDIT = "qwen_2.5_vl_7b_fp8_scaled.safetensors"
CLIP_T2I = "qwen3vl_8b_int8_convrot.safetensors"
VAE_EDIT = "qwen_image_vae.safetensors"
VAE_T2I = "qwen_image_2.1_vae_bf16.safetensors"

START_HINT = (
    "ComfyUI caído o inalcanzable en " + HOST + ".\n"
    "  Arrancá ComfyUI (run_nvidia_gpu.bat o: python main.py --listen 127.0.0.1 --port 8188)\n"
    "  y/o definí COMFYUI_URL (ej: COMFYUI_URL=http://127.0.0.1:8188)."
)


def _post(path: str, obj: dict, timeout: int = 30) -> dict:
    data = json.dumps(obj).encode("utf-8")
    req = urllib.request.Request(HOST + path, data=data,
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def _get(path: str, timeout: int = 15):
    with urllib.request.urlopen(HOST + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8") or "{}")


def check() -> int:
    try:
        s = _get("/system_stats", timeout=5)
    except Exception as e:
        print("DOWN:", e)
        print(START_HINT)
        return 1
    dev = (s.get("devices") or [{}])[0]
    print("OK comfyui", s.get("system", {}).get("comfyui_version"),
          "| gpu", dev.get("name"),
          "| vram_free", round(dev.get("vram_free", 0) / 1e9, 2), "GB")
    for node, key in (("UnetLoaderGGUF", "unet_name"), ("CLIPLoader", "clip_name"),
                      ("VAELoader", "vae_name"), ("LoraLoaderModelOnly", "lora_name")):
        try:
            o = _get(f"/object_info/{node}")
            opts = o[node]["input"]["required"][key][0]
            print(f"  {node}: {', '.join(opts)}")
        except Exception as e:
            print(f"  {node}: ERR {e}")
    try:
        _get("/object_info/TextEncodeQwenImageEditPlus")
        print("  TextEncodeQwenImageEditPlus: PRESENT")
    except Exception:
        print("  TextEncodeQwenImageEditPlus: MISSING")
    return 0


def _upload_image(image: str) -> str:
    """Sube la imagen a ComfyUI por /upload/image y devuelve el nombre (subfolder/name)."""
    p = Path(image).expanduser()
    if not p.is_file():
        raise SystemExit(f"imagen no encontrada: {p}")
    boundary = "----nexusboundary" + str(int(time.time()))
    body = b""
    body += f"--{boundary}\r\n".encode()
    body += f'Content-Disposition: form-data; name="image"; filename="{p.name}"\r\n'.encode()
    body += b"Content-Type: application/octet-stream\r\n\r\n"
    body += p.read_bytes() + b"\r\n"
    body += f"--{boundary}\r\n".encode()
    body += b'Content-Disposition: form-data; name="overwrite"\r\n\r\ntrue\r\n'
    body += f"--{boundary}--\r\n".encode()
    req = urllib.request.Request(HOST + "/upload/image", data=body,
                                 headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
                                 method="POST")
    with urllib.request.urlopen(req, timeout=120) as r:
        j = json.loads(r.read().decode("utf-8") or "{}")
    name = j.get("name") or p.name
    sub = j.get("subfolder") or ""
    return f"{sub}/{name}" if sub else name


def _save_output(img: dict, save_dir: str | None) -> str | None:
    """Descarga un resultado por /view y lo guarda en save_dir (o cwd)."""
    fn = img.get("filename")
    if not fn:
        return None
    q = urllib.parse.urlencode({"filename": fn, "subfolder": img.get("subfolder", ""),
                                "type": img.get("type", "output")})
    try:
        with urllib.request.urlopen(f"{HOST}/view?{q}", timeout=120) as r:
            data = r.read()
    except Exception as e:
        print("no se pudo descargar", fn, e)
        return None
    dest_dir = Path(save_dir).expanduser() if save_dir else Path.cwd()
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / fn
    dest.write_bytes(data)
    print("IMAGE:", dest)
    return str(dest)


def build_edit(a) -> dict:
    name = _upload_image(a.image)
    model = ["1", 0]
    wf = {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": a.edit_unet}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_EDIT, "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_EDIT}},
        "4": {"class_type": "LoadImage", "inputs": {"image": name}},
        "5": {"class_type": "ImageScaleToTotalPixels",
              "inputs": {"image": ["4", 0], "upscale_method": "lanczos",
                         "megapixels": a.megapixels, "resolution_steps": 32}},
        "6": {"class_type": "VAEEncode", "inputs": {"pixels": ["5", 0], "vae": ["3", 0]}},
        "7": {"class_type": "TextEncodeQwenImageEditPlus",
              "inputs": {"clip": ["2", 0], "prompt": a.prompt, "vae": ["3", 0], "image1": ["5", 0]}},
        "8": {"class_type": "TextEncodeQwenImageEditPlus",
              "inputs": {"clip": ["2", 0], "prompt": a.negative or "", "vae": ["3", 0], "image1": ["5", 0]}},
        "9": {"class_type": "KSampler",
              "inputs": {"model": model, "seed": a.seed, "steps": a.steps, "cfg": a.cfg,
                         "sampler_name": a.sampler, "scheduler": "simple",
                         "positive": ["7", 0], "negative": ["8", 0],
                         "latent_image": ["6", 0], "denoise": a.denoise}},
        "10": {"class_type": "VAEDecode", "inputs": {"samples": ["9", 0], "vae": ["3", 0]}},
        "11": {"class_type": "SaveImage", "inputs": {"images": ["10", 0], "filename_prefix": a.prefix}},
    }
    return wf


def build_t2i(a) -> dict:
    return {
        "1": {"class_type": "UnetLoaderGGUF", "inputs": {"unet_name": T2I_UNET}},
        "2": {"class_type": "CLIPLoader", "inputs": {"clip_name": CLIP_T2I, "type": "qwen_image"}},
        "3": {"class_type": "VAELoader", "inputs": {"vae_name": VAE_T2I}},
        "4": {"class_type": "TextEncodeQwenImage21",
              "inputs": {"clip": ["2", 0], "prompt": a.prompt,
                         "negative_prompt": a.negative or "", "resolution": max(a.width, a.height)}},
        "5": {"class_type": "EmptyLatentImage",
              "inputs": {"width": a.width, "height": a.height, "batch_size": 1}},
        "6": {"class_type": "KSampler",
              "inputs": {"model": ["1", 0], "seed": a.seed, "steps": a.steps, "cfg": a.cfg,
                         "sampler_name": a.sampler, "scheduler": "simple",
                         "positive": ["4", 0], "negative": ["4", 1],
                         "latent_image": ["5", 0], "denoise": 1.0}},
        "7": {"class_type": "VAEDecode", "inputs": {"samples": ["6", 0], "vae": ["3", 0]}},
        "8": {"class_type": "SaveImage", "inputs": {"images": ["7", 0], "filename_prefix": a.prefix}},
    }


def run(workflow: dict, timeout_s: int, save_dir: str | None) -> int:
    try:
        res = _post("/prompt", {"prompt": workflow, "client_id": "comfy_gen"})
    except urllib.error.URLError as e:
        print("ERROR conectando a ComfyUI:", e)
        print(START_HINT)
        return 1
    pid = res.get("prompt_id")
    if not pid:
        print("ERROR:", res)
        return 1
    print("prompt_id:", pid, "(generando...)")
    t0 = time.time()
    while time.time() - t0 < timeout_s:
        time.sleep(5)
        try:
            h = _get(f"/history/{pid}")
        except Exception:
            continue
        if pid in h:
            e = h[pid]
            st = e.get("status", {})
            if st.get("completed") or st.get("status_str") == "error":
                print("status:", st.get("status_str"), "elapsed:", round(time.time() - t0, 1), "s")
                if st.get("status_str") == "error":
                    print(json.dumps(st.get("messages"), ensure_ascii=False)[:800])
                    return 1
                for node in (e.get("outputs") or {}).values():
                    for img in node.get("images", []):
                        _save_output(img, save_dir)
                return 0
    print("TIMEOUT esperando resultado")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser(description="Qwen-Image / Qwen-Image-Edit via ComfyUI")
    ap.add_argument("--check", action="store_true", help="estado de ComfyUI y modelos")
    ap.add_argument("--mode", choices=["edit", "t2i"], default="edit")
    ap.add_argument("--image", help="ruta de la imagen a editar (modo edit)")
    ap.add_argument("--edit-unet", default=EDIT_UNET, help="UNET GGUF para --mode edit")
    ap.add_argument("--edit-2511", action="store_true", help="alias de compatibilidad; 2511 ya es el default")
    ap.add_argument("--prompt", default="")
    ap.add_argument("--negative", default="")
    ap.add_argument("--steps", type=int, default=None)
    ap.add_argument("--cfg", type=float, default=None)
    ap.add_argument("--sampler", default="euler")
    ap.add_argument("--denoise", type=float, default=1.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--megapixels", type=float, default=1.0)
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--prefix", default="nexus_out")
    ap.add_argument("--save", help="carpeta destino para los resultados")
    ap.add_argument("--timeout", type=int, default=1200)
    a = ap.parse_args()

    if a.check:
        return check()
    if a.edit_2511:
        a.edit_unet = EDIT_UNET_2511
    if a.steps is None:
        a.steps = 8 if a.mode == "t2i" else 20
    if a.cfg is None:
        a.cfg = 1.0 if a.mode == "t2i" else 2.5
    if not a.prompt:
        print("ERROR: --prompt requerido")
        return 2
    if a.seed == 0:
        a.seed = int(time.time()) % 2147483647
    if a.mode == "edit" and not a.image:
        print("ERROR: --mode edit requiere --image")
        return 2
    wf = build_edit(a) if a.mode == "edit" else build_t2i(a)
    return run(wf, a.timeout, a.save)


if __name__ == "__main__":
    sys.exit(main())
