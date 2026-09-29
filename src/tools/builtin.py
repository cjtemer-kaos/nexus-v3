"""
Tools - Herramientas builtin para SuperNEXUS v2.0

Adaptado de Rowboat builtin-tools.
Workspace CRUD, executeCommand, parseFile, etc.
"""

import json
import logging
from pathlib import Path
from typing import Dict, Optional

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


class WorkspaceTools:
    """Herramientas de workspace (CRUD de archivos)"""

    def __init__(self, workspace_root: Optional[str] = None):
        if workspace_root is None:
            workspace_root = str(Path(__file__).parent.parent.parent / "data")
        self.root = Path(workspace_root)

    def read_file(self, path: str, offset: int = 1, limit: int = 2000) -> Dict:
        """Lee archivo con paginacion"""
        filepath = Path(path) if Path(path).is_absolute() else self.root / path
        if not filepath.exists():
            return {"error": f"File not found: {path}"}

        try:
            lines = filepath.read_text(encoding="utf-8").split("\n")
            start = max(0, offset - 1)
            end = start + limit
            page = lines[start:end]
            has_more = end < len(lines)

            prefixed = [f"{i + offset}: {line}" for i, line in enumerate(page)]
            footer = f"(Showing lines {offset}-{offset + len(page) - 1} of {len(lines)})"

            return {
                "path": path,
                "content": "\n".join(prefixed),
                "total_lines": len(lines),
                "has_more": has_more,
                "footer": footer,
            }
        except Exception as e:
            return {"error": str(e)}

    def write_file(self, path: str, data: str, mkdirp: bool = True) -> Dict:
        """Escribe archivo. Acepta path absoluto o relativo al workspace."""
        # Layer 3+4: Path detector + drive-root fallback
        warnings = self._validate_path(path)
        # Layer 4: if drive root, try fallback to <drive>\Temp\
        resolved = self._resolve_drive_root_intent(path)
        if resolved != path:
            warnings.append(
                f"PATH FALLBACK: original was drive root '{path}'; "
                f"writing to '{resolved}' instead (drive roots need admin on Windows)."
            )
        filepath = Path(resolved) if Path(resolved).is_absolute() else self.root / resolved
        if mkdirp:
            filepath.parent.mkdir(parents=True, exist_ok=True)

        try:
            filepath.write_text(data, encoding="utf-8")
            result = {"success": True, "path": str(filepath)}
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as e:
            return {"error": str(e)}

    def create_file(self, filename: str, location: str = "", content: str = "",
                    overwrite: bool = False) -> Dict:
        """Crea archivo con campos semánticos separados. Layer 2+4 fix.
        Auto-resuelve filename + location. location absoluta (C:\\, /home) → respeta.
        location drive-root (D:\\) → fallback a D:\\Temp. location relativa → workspace.
        location vacía → workspace/data/."""
        # Sanitize filename: reject if contains path separators
        if not filename or not filename.strip():
            return {"error": "filename is required"}
        if any(sep in filename for sep in ("/", "\\")):
            return {"error": f"filename must be basename only, no path separators: '{filename}'"}
        if filename in (".", "..") or filename.startswith("."):
            return {"error": f"invalid filename: '{filename}'"}

        # Resolve location
        if not location or not location.strip():
            base = self.root / "data"
        elif Path(location).is_absolute():
            # Layer 4: if drive root, fallback to <drive>\Temp
            base_str = self._resolve_drive_root_intent(location)
            if base_str != location and base_str != location.rstrip("\\"):
                # _resolve_drive_root_intent returned a new base dir for the fallback
                base = Path(base_str)
            else:
                base = Path(location)
        else:
            base = self.root / location

        # Path detector: warn if location seems weird
        warnings = self._validate_path(str(base))

        target = base / filename
        if target.exists() and not overwrite:
            return {
                "error": f"file already exists: {target}",
                "hint": "pass overwrite=true to replace",
                "path": str(target),
            }

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content or "", encoding="utf-8")
            result = {
                "success": True,
                "path": str(target),
                "filename": filename,
                "location": str(base),
                "size_bytes": target.stat().st_size,
            }
            if warnings:
                result["warnings"] = warnings
            return result
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _validate_path(path: str) -> list:
        """Layer 3: detector de paths sospechosos. Retorna lista de warnings."""
        warnings = []
        p = Path(path)

        # Case 1: literal natural-language instruction mistaken for path
        suspicious_words = ["crea el archivo", "create the file", "guarda el archivo",
                            "save the file", "en d:", "in d:", "haz un archivo",
                            "make a file", "el archivo"]
        lower = path.lower()
        for w in suspicious_words:
            if w in lower:
                warnings.append(
                    "path looks like natural-language instruction, not a file path. "
                    "Did you mean to extract filename+location separately? "
                    "Try create_file(filename, location, content)."
                )
                break

        # Case 2: relative path with spaces (common LLM mistake: "crea prueba.txt en d:")
        if not p.is_absolute() and " " in path:
            warnings.append(
                "path contains spaces and is relative — was this meant to be absolute? "
                "If user said 'en D:' or 'en el escritorio', use absolute path."
            )

        # Case 3: filename-only relative path that ends with common ext (looks like the LLM
        # extracted just the filename and forgot to combine with location)
        if not p.is_absolute() and not p.parts and "." in path:
            warnings.append(
                "path has no directory component — writing to workspace root. "
                "If user specified a location, use absolute path."
            )

        # Case 4: Windows drive root (Layer 4) — writing to D:\prueba.txt fails with
        # [Errno 22] on Windows because drive roots require admin perms.
        # Pattern: Path("D:\\").parts = ('D:\\',), len=1. With subdir: len=2+.
        if p.is_absolute():
            parts = p.parts
            # Just the drive root itself (D:\)
            if len(parts) == 1:
                warnings.append(
                    f"PATH REJECTED: '{p}' is a drive root. Windows blocks writes here "
                    f"([Errno 22] Invalid argument). Use a subdirectory like "
                    f"'D:\\\\Temp\\\\<file>' or 'D:\\\\Users\\\\<you>\\\\<file>'."
                )
            # Drive root + filename (D:\prueba.txt) — this is the bug case
            elif len(parts) == 2 and parts[1] != "\\" and not p.name.startswith("$"):
                # Only flag if it looks like a real filename (has extension, no spaces)
                if "." in p.name and " " not in p.name:
                    warnings.append(
                        f"PATH REJECTED: writing '{p}' (drive root + filename) is blocked "
                        f"on Windows. Use '{parts[0]}\\\\Temp\\\\{p.name}' or "
                        f"'{parts[0]}\\\\Users\\\\<you>\\\\{p.name}' instead."
                    )

        return warnings

    @staticmethod
    def _resolve_drive_root_intent(path: str) -> str:
        """Layer 4: si el path es drive root (D:\\file), auto-fallback a D:\\Temp\\file
        en lugar de fallar. Solo aplica si D:\\Temp existe o se puede crear."""
        p = Path(path)
        if not p.is_absolute():
            return path
        parts = p.parts
        # Drive root only: Path("D:\\").parts == ('D:\\',), len 1, no name
        if len(parts) == 1 or (len(parts) == 2 and not p.name and p.parts[1] == "\\"):
            drive = p  # already 'D:\\'
            candidate = drive / "Temp" / "_fallback.txt"
            try:
                candidate.parent.mkdir(parents=True, exist_ok=True)
                return str(drive / "Temp")
            except Exception:
                return path
        # Drive root + filename: Path("D:\\prueba.txt").parts == ('D:\\','prueba.txt')
        if len(parts) == 2 and parts[0].endswith("\\") and p.name and "." in p.name and " " not in p.name:
            drive = parts[0]  # 'D:\\'
            try:
                Path(drive + "Temp").mkdir(parents=True, exist_ok=True)
                return drive + "Temp\\" + p.name
            except Exception:
                return path
        return path

    def list_dir(self, path: str = "", recursive: bool = False, max_depth: int = 5) -> Dict:
        """Lista directorio — acepta paths absolutos o relativos al workspace"""
        if path and Path(path).is_absolute():
            dirpath = Path(path)
        else:
            dirpath = self.root / path if path else self.root
        if not dirpath.exists():
            return {"error": f"Directory not found: {path}"}

        exclude_dirs = {'.git', 'node_modules', '__pycache__', 'venv', '.venv', '.tox', 'dist', 'build', '.mypy_cache', '.pytest_cache'}
        entries = []

        if recursive:
            for p in dirpath.rglob("*"):
                rel_parts = p.relative_to(dirpath).parts
                if any(part in exclude_dirs for part in rel_parts):
                    continue
                if len(rel_parts) > max_depth:
                    continue
                entries.append({"path": str(p), "type": "dir" if p.is_dir() else "file"})
        else:
            for p in sorted(dirpath.iterdir()):
                if p.name in exclude_dirs:
                    continue
                entries.append({"path": p.name, "type": "dir" if p.is_dir() else "file"})

        return {"entries": entries, "count": len(entries)}


class ExecuteTools:
    """Herramientas de ejecucion"""

    def __init__(self):
        from src.tools.persistent_shell import PersistentShell
        self.shell = PersistentShell.get_instance()

    async def execute_command(self, command: str, cwd: Optional[str] = None, timeout: int = 60) -> Dict:
        """Ejecuta comando shell usando persistent shell"""
        try:
            timeout_ms = timeout * 1000
            stdout, stderr, exit_code, interrupted = await self.shell.exec(command, timeout_ms)
            return {
                "success": exit_code == 0 and not interrupted,
                "stdout": stdout[:5000],
                "stderr": stderr[:5000],
                "returncode": exit_code,
                "interrupted": interrupted,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}


class ParseTools:
    """Herramientas de parseo de archivos"""

    def parse_file(self, path: str) -> Dict:
        """Parsea archivo segun extension"""
        filepath = Path(path)
        if not filepath.exists():
            return {"error": f"File not found: {path}"}

        if filepath.stat().st_size > 5 * 1024 * 1024:
            return {"error": f"File too large (>5MB): {path}"}

        ext = filepath.suffix.lower()
        try:
            content = filepath.read_text(encoding="utf-8")

            if ext in (".json",):
                return {"success": True, "format": "json", "data": json.loads(content)}
            elif ext in (".md", ".txt", ".py", ".js", ".ts", ".html", ".css", ".yaml", ".yml", ".toml", ".env", ".sh", ".dockerfile", ".go", ".rs", ".java", ".c", ".cpp", ".h", ".rb"):
                return {"success": True, "format": "text", "content": content[:5000]}
            else:
                return {"success": True, "format": "unknown", "size": filepath.stat().st_size}
        except Exception as e:
            return {"error": str(e)}
