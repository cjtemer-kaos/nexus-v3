---
name: qwen-image-studio
description: Genera, edita y anima imagenes y VIDEO 100% local en PC1 (ComfyUI, RTX 3060). Cubre imagen (Qwen-Image 2.1-Turbo / Edit 2509/2511), video (Wan 2.2 TI2V-5B, Wan 2.1 VACE, Wan 2.1 Fun 1.3B), edicion por instruccion (Bernini-R), audio sincronizado (MMAudio) e interpolacion (RIFE). Usala cuando pidan crear/transformar/animar imagenes o videos, editar video, poner audio a un clip, "cyberpunk/vampira/payaso", cambiar fondo, I2V, t2v, lipsync, o cuando mencionen ComfyUI, Qwen-Image, Wan, Bernini, MMAudio, img2img o video local.
---

# Qwen Image & Video Studio (ComfyUI local, PC1)

Generacion/edicion de **imagen y video** 100% local con **ComfyUI portable**.
Sin APIs de pago, sin subir material a la nube.

## Entorno (PC1)

- ComfyUI portable: `D:\ComfyUI-2.1\ComfyUI_windows_portable`
- URL API: `http://127.0.0.1:8188`
- GPU: RTX 3060 12 GB (Ampere sm_86) · Ryzen 7 5700X3D · 31 GB RAM
- Python embebido: `D:\ComfyUI-2.1\ComfyUI_windows_portable\python_embeded\python.exe`
- ffmpeg (WinGet): `C:\Users\cjtr\AppData\Local\Microsoft\WinGet\Packages\Gyan.FFmpeg_*\ffmpeg-8.1-full_build\bin\ffmpeg.exe`
- Inputs multimedia: `ComfyUI\input\` · Salidas: `ComfyUI\output\`

### Arranque / estado
```powershell
# cwd: D:\ComfyUI-2.1\ComfyUI_windows_portable
.\python_embeded\python.exe -s ComfyUI\main.py --windows-standalone-build --listen 127.0.0.1 --port 8188
# o: .\run_nvidia_gpu.bat
python <skill>\scripts\comfy_gen.py --check     # estado + modelos
```

## Capa 1 — IMAGEN (script helper)

`scripts/comfy_gen.py` (solo stdlib) — copia la imagen a `ComfyUI\input` automaticamente.

```powershell
python comfy_gen.py --check
# Editar conservando identidad (Qwen-Image-Edit 2511 Q3_K_L; default)
python comfy_gen.py --mode edit --image foto.png --prompt "..." --negative "fangs, monster, male face, deformed" --steps 20 --cfg 2.5 --prefix out --save C:\Users\cjtr\Desktop
# Alias viejo (opcional): --edit-2511 ya es el default
python comfy_gen.py --mode edit --edit-2511 --image foto.png --prompt "..." --steps 20 --cfg 2.5 --prefix out
# Texto a imagen (Qwen-Image 2.1-Turbo, 8 pasos / CFG 1 por defecto)
python comfy_gen.py --mode t2i --prompt "a red apple on a wooden table" --width 1024 --height 1024
```

**Edit 2511:** `qwen-image-edit-2511-Q3_K_L.gguf` (10,58 GB) es el **default de edición** (`EDIT_UNET`). Usa el mismo CLIP `qwen_2.5_vl_7b_fp8_scaled` y VAE `qwen_image_vae`. La card oficial sugiere 40 pasos / true_cfg 4.0; medido en la 3060: **20 pasos / CFG 2.5 a 1 MP = 8:05**.

**Quant en 12 GB:** Q3_K_L (10,58 GB) es el equilibrio para la 3060. Q4_K_M (13,24 GB) carga, pero deja 3,4 GB en RAM y baja a ~2:19 por paso a 1 MP (~46 min los 20 pasos); se descartó.

**T2I Turbo:** `T2I_UNET=qwen-image-2.1-turbo-Q4_K_S.gguf`. Usa **8 pasos y CFG 1.0**; no le pases 20/2.5 (es el schedule del 2.1 base, no del Turbo). El checkpoint trae sus sigmas; con `KSampler/simple` es una aproximación, para el schedule exacto usar `ManualSigmas` + `SamplerCustomAdvanced`.

**REGLA DE ORO identidad:** para mantener el rostro usa `--steps 20 --cfg 2.5`, prompt *"preserve the EXACT same woman..."* + negative. Base ~8 min (2511 Q3_K_L, medido 8:05).

**REGLA DE ENCUADRE (crítico, aprendido 2026-10-02 con Pocahontas):** Qwen-Image-Edit pierde la cara si le pedís escena amplia. Para conservar identidad: **plano cerrado** (head and shoulders, *"FACE LARGE and centered, do NOT zoom out, NO full body"*). Negative obligatorio: `full body, wide shot, landscape, different person`. Pedir "standing in a vast forest / full body" = aleja la cámara y diluye el rostro.
Contraste real: vampira/payasa (retrato cerrado sobre `julie.png`) = identidad OK; Pocahontas cuerpo entero = perdía la cara; Pocahontas en **close-up** = identidad OK.

## Capa 2 — VIDEO (Wan, nativo)

Modelos instalados (ComfyUI\models):
| Uso | Archivo | Dir |
|---|---|---|
| Generar t2v/i2v 720p | `wan2.2_ti2v_5B_fp16.safetensors` (+ GGUF Q5_K_M) | diffusion_models |
| Editar (ref/inpaint/FLF2V) | `wan2.1_vace_1.3B_fp16.safetensors` | diffusion_models |
| Control camara/pose/inpaint | `wan2.1_fun_control/inp/camera_v1.1_1.3B_bf16` | diffusion_models |
| Edicion por instruccion | `wan2.1_bernini_1.3B_fp16.safetensors` | diffusion_models |
| Encoder | `umt5_xxl_fp8_e4m3fn_scaled.safetensors` | text_encoders |
| VAE | `wan2.2_vae` (TI2V) · `wan_2.1_vae` (VACE/Fun/1.3B) | vae |
| LTX rapido | `ltx-video-2b-v0.9.5.safetensors` | checkpoints |

Fórmula duración: **frames = segundos × fps** (Wan entrena a ~16 fps). 5 s = 81 frames.
Formato de frames: **4n+1** (81, 121, 161...). `SaveVideo(format=mp4, codec=h264)` = MP4 directo.

### T2V (Wan2.2 TI2V-5B)
`UNETLoader(wan2.2_ti2v_5B_fp16) + CLIPLoader(umt5, type=wan) + VAELoader(wan2.2_vae) + ModelSamplingSD3(shift8) + 2×CLIPTextEncode + Wan22ImageToVideoLatent(w,h,length) + KSampler(uni_pc/simple, cfg5) + VAEDecode + CreateVideo(fps) + SaveVideo(mp4/h264)`

### I2V (animar una foto) — añade a lo anterior
`LoadImage → ImageScale → Wan22ImageToVideoLatent(start_image=...)`. **Tip:** ~5-8 s de animacion funciona mejor con prompts de movimiento sutil ("blinks, camera slowly pushes in").

### Editar video / reference / FLF2V → **WanVaceToVideo** (VACE 1.3B, 480p)
### Control camara/pose → **WanCameraEmbedding + WanCameraImageToVideo** (Fun 1.3B)
### Edicion por instruccion (quitar watermark/subtitulos, style transfer) → **Bernini-R 1.3B** (nodos `BerniniConditioning`, `UnetLoaderGGUF`)

## Capa 3 — AUDIO + POST (local)

- **CosyVoice (CLONACIÓN de voz, la mejor opción)**: `D:\ComfyUI-2.1\system\voice\CosyVoice` (modelo `CosyVoice-300M-Instruct`). Clona desde un audio de referencia corto (~6-15 s). Launcher: `system\voice\cosyvoice.bat "texto" [ref.wav] [out.wav]`. Script: `cosyvoice_clone.py --ref ref.wav --text "..." --out out.wav` (venv `system\voice\VibeVoice\.venv`). Así se pone **la voz de julie** (referencia en `samples\julie_ref\`). ~17 s por frase, 22 kHz.
- **VibeVoice TTS** (Microsoft, `D:\ComfyUI-2.1\system\voice\VibeVoice`): texto→voz local en CUDA (~1.1x tiempo real). Launcher: `system\voice\vibevoice.bat "texto" [voz]`. Voces embebidas (`en-Emma_woman`, `sp-spk1_man`...). Pesos en `system\voice\_hf_cache`. Arg clave: `--txt_path` **absoluto**. (No clona voz arbitraria; para clonar usar CosyVoice.)
- **MMAudio** (nodos `MMAudio*`): audio sincronizado desde video+prompt. Modelos en `models/mmaudio/`. 🚨 `MMAudioFeatureUtilsLoader` requiere `precision='fp16'` y `mode='44k'`; VHS_VideoCombine no engancha audio → usar `SaveAudio` + ffmpeg mux.

- **MMAudio** (nodos `MMAudio*`): audio sincronizado desde video+prompt. Modelos en `models/mmaudio/` (large_44k_v2_fp16 + vae_44k_fp16 + synchformer_fp16 + CLIP DFN5B fp16). El vocoder BigVGAN se **auto-descarga** a `models/mmaudio/nvidia/bigvgan_v2_44khz_128band_512x`. 🚨 `MMAudioFeatureUtilsLoader` requiere pasar `precision='fp16'` y `mode='44k'` explicitos.
  Flujo: `VHS_LoadVideo → MMAudioModelLoader + MMAudioFeatureUtilsLoader + MMAudioVoCoderLoader → MMAudioSampler(duration=...) → SaveAudio` (flac). Luego unir a video con ffmpeg (`-c:v copy -c:a aac`).
- **RIFE/VFI** (`RIFEInterpolation`, `FrameInterpolationModelLoader`): interpolar 16→24/30 fps.
- **ComfyUI-VideoHelperSuite** (`VHS_LoadVideo`, `VHS_VideoCombine`, `VHS_LoadAudio`...): cargar/guardar/combinar video+audio.
- **WanVACE-Prep** (`WanVACEPrep/Extend/Inpaint/Outpaint`): transiciones, extender, unir clips.

## Performance medido (RTX 3060, 12 GB)
| Job | Res | Tiempo |
|---|---|---|
| Imagen t2i Turbo (8 pasos, CFG 1) | 512×512 | ~15 s |
| Imagen t2i Turbo (8 pasos, CFG 1) | 1024×1024 | ~55 s |
| Imagen edit 2511 Q3_K_L (20 pasos) | 1 MP | ~8 min (485 s) |
| Video T2V/I2V (5-8 s) | 832×480 | ~4-8 min |
| Video I2V | 1280×704 | ~14 min (evitar salvo final) |
| Fun Camera zoom | 832×480 | ~6-7 min |
| MMAudio audio (3 s) | — | ~2 min (1ª vez descarga BigVGAN) |

## Descargar modelos nuevos
`hf download <repo> --include "<archivo>" --local-dir <tmp>` → mover a `models\<carpeta>` → reiniciar ComfyUI.
Fuentes: `Comfy-Org/Qwen-Image_ComfyUI`, `Comfy-Org/Qwen-Image-Edit_ComfyUI`, `Comfy-Org/Wan_2.*_ComfyUI_repackaged`, `QuantStack/*-GGUF`, `Kijai/MMAudio_safetensors`, `Lightricks/LTX-Video`.

## Alternativa: Fooocus (SDXL, fotorealismo sin censura)
`D:\ias\proyectos\Fooocus` — motor SDXL con `juggernautXL_v8Rundiffusion` (6.62 GB), presets (`realistic.json`, `pony_v6.json`, `anime.json`), venv propio. UI en `:7865`. Bueno para fotorealismo SDXL rapido; el sistema Qwen/Wan es superior en edicion/identidad/instruccion.

## Escena primero + angulos + catalogo de modelos (adoptado de ArtCraft, 2026-10-07)

**REGLA: escena primero (scene-first), no "promptear y rezar".** Antes de generar, armar la composicion y sacar una guia:
1. **Bloquear escena**: pose/camara/capas en 3D (rigs Blender fight3 / blocky) o 2D (capas + mascaras).
2. **Render guia**: depth/pose/mask + la camara EXACTA (el encuadre se decide en 3D, no en el prompt).
3. **Generar**: `qwen-image-edit-2509` (identidad) o Wan I2V usando la guia.
4. **Componer**: bg-removal -> capas/props; inpaint donde haga falta.
5. **Post**: RIFE (fps) + MMAudio (audio) + ffmpeg.

- **Angulos (clave para animacion)**: generar el MISMO sujeto desde varios angulos a partir de 1 imagen -> storyboard multi-plano consistente. Cloud: `qwen_edit_2511_angles` / `flux_2_lora_angles`. Local: Qwen-Image-Edit 2509 con instruccion de camara + (ControlNet/pose).
- **Multi-funcion**: el mismo script hace t2i si NO hay `--image`, y edit/i2i si la hay (patron ArtCraft `image_media_tokens` opcional).
- **BG removal / inpaint**: local (rembg/BiRefNet + nodos inpaint) o cloud (remove_background / flux inpaint) para reusar sujetos como capas/props.
- **Catalogo de modelos y params**: `D:\ComfyUI-2.1\system\video\models_catalog.json` — consultarlo ANTES de elegir modelo; trae `routing` por tarea (identidad, i2v, angulos, bg-removal, 3D...).
- **Tecnica "web sin API key"** (de ArtCraft `lib/`): `browser_emulation` + `cloudflare_mitigation` (headers Firefox WS/HTTP1.1 + cookie store + browser profile) para llamar proveedores web emulando navegador — usar solo con sesion propia.

## Troubleshooting
- **ComfyUI caido** → arrancar (arriba).
- **OOM** → bajar a 832×480 o `--megapixels 0.7`; cerrar otros usos de GPU.
- **Video corto** → subir `length` (fps 16) o mas frames.
- **MMAudio sin stream audio** → usar `SaveAudio` y unir con ffmpeg (VHS_VideoCombine no siempre engancha el audio).
- **Modelos nuevos no aparecen** → reiniciar ComfyUI.
- **Cara no reconocible** → ver REGLA DE ORO arriba.
