# vendor/ — binarios externos (no versionados)

Esta carpeta queda vacía a propósito. Los binarios son grandes, no se trackean en git
(`.gitignore`) y se restauran desde un backup o se descargan de nuevo.

## llama.cpp (retirado 2026-10-19)

- **Estado**: retirado del proyecto. `llama.cpp` se descartó con la migración a Ollama
  (2026-09-28); `config/config.yaml` solo declara el provider `ollama` y
  `config/models.yaml` quedó como referencia.
- **Qué se movió**: `vendor/llama.cpp/` (556 MB, binarios Windows + `ggml-cuda.dll` de 513 MB)
  a `D:\ias\backups\nexus-v3\vendor-llama.cpp-20261019\` (fuera del repo, para no engordar
  el instalador de Electron).
- **Cómo restaurar** (solo si se re-habilita llama.cpp):

  ```powershell
  Move-Item D:\ias\backups\nexus-v3\vendor-llama.cpp-20261019 vendor\llama.cpp
  ```

  Después hay que declarar el provider en `config/config.yaml`:

  ```yaml
  providers:
    llamacpp:
      autostart: true
  ```

  Sin ese bloque, `src/providers/llama_manager.py` no arranca nada y el harness sigue
  funcionando con Ollama (comportamiento verificado en `tests/test_llama_manager.py`).

## Regenerar desde cero

`llama_manager.py` espera `<root>/vendor/llama.cpp/llama-server.exe` y sus DLLs
(`llama.dll`, `llama-common.dll`, `ggml.dll`, `ggml-base.dll`, `ggml-cuda.dll`, `libomp.dll`).
Se obtienen de un release oficial de llama.cpp para Windows (build CUDA o CPU).

## Higiene

`python scripts/hygiene.py` reporta si la carpeta existe, cuánto ocupa y qué binarios
requeridos faltan (nunca borra nada por su cuenta).
