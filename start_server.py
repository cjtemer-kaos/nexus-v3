"""
Nexus v3 launcher — delegates to harness backend (src.api.server).

- `python start_server.py`       -> harness backend on :8765 (production SPA at ui/dist)
- `python start_server.py --dev` -> harness + Vite dev instructions
- `python start_server.py --port 9000` -> custom port
"""
import sys
from pathlib import Path

# Ensure project root on sys.path like src/api/server.py does
sys.path.insert(0, str(Path(__file__).parent))

if __name__ == "__main__":
    import argparse
    # If --dev flag, spawn both backend and vite via helper
    if "--dev" in sys.argv:
        import subprocess
        ui_dir = Path(__file__).parent / "ui"
        print("[nexus-v3] DEV: starting harness backend :8765 + Vite :5174")
        print("[nexus-v3] Hint: or use `npm run dev` (concurrently) or Electron `npm run dev`")
        # For simple --dev, just delegate to npm run dev in ui (user can run backend separately)
        # Real dev with both processes is via `npm run dev` at root (concurrently)
        subprocess.run(["npm", "run", "dev"], cwd=ui_dir, shell=True)
    else:
        # Production harness: FastAPI serving ui/dist
        from src.api.server import main
        # Forward args (e.g. --port)
        main(sys.argv[1:] if len(sys.argv) > 1 else None)
