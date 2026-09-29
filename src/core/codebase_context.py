"""
codebase_context — acceso al codebase completo para todas las gemas.
Envuelve repomix con --compress Tree-sitter + cache LRU + grep contextual.

Uso:
  from core.codebase_context import CodebaseContext
  ctx = CodebaseContext(project_root="/path/to/supernexus-v2")
  dump = await ctx.get_context(scope="src/core")  # full dump comprimido
  results = await ctx.query_context("auth system")  # grep + contexto
"""
import asyncio
import hashlib
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False


class LRUCache:
    """Simple LRU cache with TTL."""

    def __init__(self, maxsize: int = 8, ttl: int = 300):
        self._data: Dict[str, Tuple[float, str]] = {}
        self._order: List[str] = []
        self._maxsize = maxsize
        self._ttl = ttl

    def get(self, key: str) -> Optional[str]:
        now = time.time()
        entry = self._data.get(key)
        if not entry:
            return None
        ts, val = entry
        if now - ts > self._ttl:
            self._evict(key)
            return None
        # Move to end (most recently used)
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        return val

    def set(self, key: str, value: str):
        now = time.time()
        self._data[key] = (now, value)
        if key in self._order:
            self._order.remove(key)
        self._order.append(key)
        while len(self._order) > self._maxsize:
            oldest = self._order.pop(0)
            self._evict(oldest)

    def _evict(self, key: str):
        self._data.pop(key, None)
        if key in self._order:
            self._order.remove(key)

    def clear(self):
        self._data.clear()
        self._order.clear()


class CodebaseContext:
    """
    Acceso al codebase via repomix.
    - Genera dump comprimido una vez (Tree-sitter structural compression)
    - Cache LRU por scope
    - Query via grep sobre el dump cacheado
    """

    def __init__(
        self,
        project_root: Optional[str] = None,
        repomix_timeout: int = 60,
        cache_ttl: int = 600,
    ):
        if project_root:
            self._project_root = Path(project_root).resolve()
        else:
            # Auto-detect: buscar el project root desde __file__
            self._project_root = Path(__file__).resolve().parents[2]
        self._repomix_timeout = repomix_timeout
        self._cache = LRUCache(maxsize=8, ttl=cache_ttl)
        self._repomix_available = self._check_repomix()
        self._project_hash = hashlib.md5(str(self._project_root).encode()).hexdigest()[:8]
        self._dump_path = Path.home() / ".nexus" / "codebase_dumps" / f"dump_{self._project_hash}.md"

    def _check_repomix(self) -> bool:
        """Check if repomix command is available via shell."""
        try:
            if os.name == "nt":
                r = subprocess.run(
                    "where repomix.cmd",
                    capture_output=True, text=True, timeout=5, shell=True,
                )
            else:
                r = subprocess.run(
                    ["which", "repomix"],
                    capture_output=True, text=True, timeout=5,
                )
            return r.returncode == 0
        except Exception:
            return False

    def _repomix_cmd(self) -> str:
        """Devuelve el comando repomix correcto segun plataforma."""
        if os.name == "nt":
            # Windows: repomix.cmd es un batch que ejecuta node
            return "repomix.cmd"
        return "repomix"

    async def _run_repomix(self, scope: str = "") -> Optional[str]:
        """Ejecuta repomix --compress y devuelve el dump."""
        if not self._repomix_available:
            logger.warning("repomix not found — install with: npm install -g repomix")
            return None

        scope_key = scope.replace("\\", "/").replace("..", "")
        cache_key = f"repomix:{self._project_hash}:{scope_key}"

        cached = self._cache.get(cache_key)
        if cached:
            return cached

        repomix = self._repomix_cmd()

        # Build repomix command
        cmd = [repomix, "--compress", "--style", "markdown"]

        # Use --include for scope
        if scope:
            parts = [s.strip() for s in scope.split(",") if s.strip()]
            if parts:
                cmd.append("--include")
                cmd.extend(parts)

        cmd.extend(["--output", str(self._dump_path)])
        cmd.append(str(self._project_root))

        try:
            # Use shell on Windows for .cmd files
            if os.name == "nt":
                # Windows: quote paths with spaces, use double quotes for .cmd
                quoted = []
                for c in cmd:
                    if " " in c:
                        quoted.append(f'"{c}"')
                    else:
                        quoted.append(c)
                shell_cmd = " ".join(quoted)
                proc = await asyncio.create_subprocess_shell(
                    shell_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self._repomix_timeout
                )
            except asyncio.TimeoutError:
                proc.kill()
                logger.error(f"repomix timed out ({self._repomix_timeout}s) for scope={scope}")
                return None
        except Exception as e:
            logger.error(f"repomix failed for scope={scope}: {e}")
            return None

        if proc.returncode != 0:
            logger.warning(f"repomix returned {proc.returncode}: {stderr.decode()[:200]}")
            # Dump may still exist even with non-zero returncode
            if not self._dump_path.exists():
                return None

        if not self._dump_path.exists():
            logger.error(f"repomix didn't create dump at {self._dump_path}")
            if stderr:
                logger.error(f"stderr: {stderr.decode()[:300]}")
            return None

        content = self._dump_path.read_text(encoding="utf-8", errors="replace")
        self._cache.set(cache_key, content)
        logger.info(f"repomix dump generated ({len(content)} chars, scope={scope or 'full'})")
        return content

    async def get_context(
        self,
        scope: str = "",
        format: str = "markdown",
        max_chars: int = 0,
    ) -> str:
        """
        Devuelve dump completo del codebase (comprimido con Tree-sitter).

        Args:
            scope: paths a incluir, ej "src/core" o "src/core,src/brain"
                   vacío = todo el proyecto
            format: "markdown" (default) — repomix solo soporta markdown por ahora
            max_chars: truncar a N chars (0 = sin truncar)
        """
        content = await self._run_repomix(scope)
        if not content:
            return "Codebase context unavailable (repomix not installed)"
        if max_chars and len(content) > max_chars:
            content = content[:max_chars] + "\n\n... [TRUNCATED]"
        return content

    async def query_context(
        self,
        query: str,
        scope: str = "",
        max_results: int = 5,
        context_lines: int = 10,
    ) -> str:
        """
        Busca codigo relevante a un query en el codebase comprimido.
        Usa el dump cacheado y hace grep sobre el.

        Args:
            query: texto a buscar (keyword o regex simple)
            scope: paths a incluir en el dump
            max_results: maximo de secciones a devolver
            context_lines: lineas de contexto alrededor de cada match

        Returns:
            Markdown con las secciones relevantes del codebase
        """
        content = await self._run_repomix(scope)
        if not content:
            return "Codebase query unavailable (repomix not installed)"

        # Split into sections by file headers
        sections = re.split(r'(^## File: .+$)', content, flags=re.MULTILINE)
        if len(sections) < 2:
            return content[:2000]  # No se pudo dividir, devolver raw

        # Pair headers with content
        pairs: List[Tuple[str, str]] = []
        current_header = ""
        for s in sections:
            s = s.strip()
            if s.startswith("## File:"):
                current_header = s
            elif current_header and s:
                pairs.append((current_header, s))
                current_header = ""
            elif not current_header and s:
                # Content before first header — skip
                pass

        # Score each section by query relevance
        scored: List[Tuple[float, str, str]] = []
        qwords = set(query.lower().split())
        for header, body in pairs:
            lower = body.lower()
            # Count exact match
            exact_count = lower.count(query.lower())
            if exact_count == 0:
                # Check word overlap
                words = set(lower.split())
                overlap = len(qwords & words)
                if overlap < 1:
                    continue
                score = overlap / max(len(qwords), 1)
            else:
                score = exact_count * 2 + 5  # boost for exact matches

            # Boost for filename match
            if query.lower() in header.lower():
                score += 10

            scored.append((score, header, body))

        # Sort by relevance
        scored.sort(key=lambda x: -x[0])

        if not scored:
            return f"No codebase results for query: {query}"

        # Build result
        result_lines = [f"# Codebase context for: {query}", ""]
        count = 0
        for score, header, body in scored:
            if count >= max_results:
                break
            result_lines.append(header)
            # Extract context around matches
            if context_lines and query:
                body_lines = body.split("\n")
                matched_indices = set()
                for i, bl in enumerate(body_lines):
                    if query.lower() in bl.lower():
                        start = max(0, i - context_lines)
                        end = min(len(body_lines), i + context_lines + 1)
                        for j in range(start, end):
                            matched_indices.add(j)
                selected = [body_lines[i] for i in sorted(matched_indices)]
                if selected:
                    result_lines.extend(selected)
                else:
                    result_lines.append(body[:500])
            else:
                result_lines.append(body[:500])
            result_lines.append("")
            count += 1

        return "\n".join(result_lines)

    async def close(self):
        self._cache.clear()


# Singleton instance for reuse
_instance: Optional[CodebaseContext] = None


def get_instance(project_root: Optional[str] = None) -> CodebaseContext:
    global _instance
    if _instance is None:
        _instance = CodebaseContext(project_root=project_root)
    return _instance
