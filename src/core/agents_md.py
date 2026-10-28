"""AGENTS.md jerárquico.

Lee el AGENTS.md del root del proyecto + los AGENTS.md de los directorios ancestros de
los paths relevantes, y los concatena para inyectar en el system prompt. Así el agente
recibe solo la guía aplicable a lo que va a tocar, sin cargar todo el árbol.
"""
from __future__ import annotations

from pathlib import Path


def _read(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""


def find_agents_md(paths, project_root, max_files: int = 8) -> list[tuple[str, str]]:
    """Devuelve [(relpath, contenido)] — root primero, luego ancestros (root→deep)."""
    root = Path(project_root).resolve()
    found: dict[str, str] = {}

    def add(md: Path) -> None:
        try:
            rel = str(md.relative_to(root)).replace("\\", "/")
        except Exception:
            return
        if rel in found:
            return
        content = _read(md)
        # Un AGENTS.md inexistente o vacío sólo aportaría un encabezado sin
        # reglas: se omite para no gastar presupuesto en nada.
        if content.strip():
            found[rel] = content

    add(root / "AGENTS.md")
    for p in paths or []:
        try:
            cur = Path(p).resolve()
        except Exception:
            continue
        cur = cur if cur.is_dir() else cur.parent
        chain = []
        while True:
            chain.append(cur)
            if cur == root or cur.parent == cur:
                break
            cur = cur.parent
        for d in reversed(chain):  # root → deep
            md = d / "AGENTS.md"
            if md.is_file():
                add(md)
    return sorted(found.items())[:max_files]


_HEADER = "\n\nGUIA DEL PROYECTO (AGENTS.md jerárquico; seguí estas reglas):\n"
_MIN_CHUNK = 200


def system_block(paths, project_root, max_chars: int = 12000) -> str:
    """Bloque para el system prompt ("" si no hay AGENTS.md).

    El presupuesto es total, pero **no descarta archivos en silencio**: si un
    AGENTS.md no entra completo se recorta y se marca `(recortado)`. Antes, en
    cuanto el del root crecía, el del subdirectorio desaparecía sin aviso y el
    agente perdía justo las reglas de la carpeta que iba a tocar.
    """
    items = find_agents_md(paths, project_root)
    if not items:
        return ""
    parts: list[str] = []
    total = len(_HEADER)
    for rel, content in items:
        chunk = f"## {rel}\n{content.strip()}\n"
        restante = max_chars - total
        if restante < _MIN_CHUNK:
            break
        if len(chunk) > restante:
            corte = chunk.rfind("\n", 0, restante - 1)
            chunk = chunk[:corte if corte > 0 else restante - 1] + "\n(recortado)\n"
        parts.append(chunk)
        total += len(chunk)
    if not parts:
        return ""
    return _HEADER + "\n".join(parts)
