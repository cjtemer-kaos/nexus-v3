"""
Smart Codebase Indexer — Codebase awareness engine for SuperNEXUS v2.

Provides Cursor-like codebase intelligence:
- Full-text search across symbols, docstrings, and file content
- Python AST parsing for precise symbol extraction
- Regex fallback for non-Python languages
- Incremental updates for live editing
- Call graph construction
- FTS5-backed search for fast retrieval

Storage: ~/.nexus/brain/codebase_index.db
Singleton: get_indexer()
"""

import ast
import hashlib
import logging
import os
import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("nexus-codebase-indexer")

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SymbolInfo:
    """Indexed symbol metadata."""
    name: str
    kind: str  # class | function | method | variable
    file_path: str
    line_start: int
    line_end: int
    docstring: str = ""

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "kind": self.kind,
            "file_path": self.file_path,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "docstring": self.docstring,
        }


@dataclass
class FileEntry:
    """Indexed file metadata."""
    path: str
    language: str
    size: int
    file_hash: str
    indexed_at: str


@dataclass
class ImportInfo:
    """Import statement info."""
    file_path: str
    module: str
    alias: str = ""


@dataclass
class DependencyInfo:
    """File-level dependency."""
    from_file: str
    to_file: str
    dep_type: str  # import | include | require | from


@dataclass
class SearchResult:
    """A single search result."""
    file_path: str
    line: int
    snippet: str
    match_type: str  # symbol | docstring | content
    score: float = 0.0

    def to_dict(self) -> dict:
        return {
            "file_path": self.file_path,
            "line": self.line,
            "snippet": self.snippet,
            "match_type": self.match_type,
            "score": self.score,
        }


# ---------------------------------------------------------------------------
# Language detection helpers
# ---------------------------------------------------------------------------

_LANG_EXTENSIONS: Dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".jsx": "javascript",
    ".tsx": "typescript",
    ".rs": "rust",
    ".go": "go",
    ".java": "java",
    ".c": "c",
    ".cpp": "cpp",
    ".h": "c",
    ".hpp": "cpp",
    ".rb": "ruby",
    ".php": "php",
    ".cs": "csharp",
    ".swift": "swift",
    ".kt": "kotlin",
    ".scala": "scala",
    ".sh": "bash",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".xml": "xml",
    ".html": "html",
    ".css": "css",
    ".sql": "sql",
    ".md": "markdown",
}

# Folders to skip during indexing
_SKIP_DIRS: Set[str] = {
    "__pycache__", ".git", ".hg", ".svn", "node_modules", ".venv", "venv",
    "env", ".env", ".tox", ".mypy_cache", ".pytest_cache", "dist", "build",
    ".eggs", "*.egg-info", ".idea", ".vscode", ".nexus",
}


def _detect_language(filepath: str) -> str:
    ext = Path(filepath).suffix.lower()
    return _LANG_EXTENSIONS.get(ext, "unknown")


def _file_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _assign_targets(node: ast.AST) -> List[str]:
    """Extract assignment target names from ast.Assign / ast.AnnAssign."""
    targets: List[str] = []
    if isinstance(node, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                targets.append(t.id)
            elif isinstance(t, (ast.Tuple, ast.List)):
                for elt in t.elts:
                    if isinstance(elt, ast.Name):
                        targets.append(elt.id)
    elif isinstance(node, ast.AnnAssign) and node.target:
        if isinstance(node.target, ast.Name):
            targets.append(node.target.id)
    return targets


# ---------------------------------------------------------------------------
# Python AST parser
# ---------------------------------------------------------------------------

class _PythonParser:
    """Extract symbols, imports, and call graphs from Python source via AST."""

    @staticmethod
    def parse(
        source: str, filepath: str
    ) -> Tuple[List[SymbolInfo], List[ImportInfo], Dict[str, List[str]]]:
        symbols: List[SymbolInfo] = []
        imports: List[ImportInfo] = []
        call_graph: Dict[str, List[str]] = {}

        try:
            tree = ast.parse(source, filename=filepath)
        except SyntaxError as exc:
            logger.warning("AST parse failed for %s: %s", filepath, exc)
            return symbols, imports, call_graph

        lines = source.splitlines()

        def _docstring(node: ast.AST) -> str:
            return ast.get_docstring(node) or ""

        def _span(node: ast.stmt) -> Tuple[int, int]:
            start = getattr(node, "lineno", 0)
            end = getattr(node, "end_lineno", start)
            return start, end

        def _class_methods(node: ast.ClassDef) -> None:
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    s, e = _span(item)
                    kind = "method"
                    symbols.append(SymbolInfo(
                        name=item.name, kind=kind, file_path=filepath,
                        line_start=s, line_end=e,
                        docstring=_docstring(item),
                    ))
                    _extract_calls(item, f"{node.name}.{item.name}")

        def _extract_calls(node: ast.AST, caller: str) -> None:
            callees: List[str] = []
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name):
                        callees.append(child.func.id)
                    elif isinstance(child.func, ast.Attribute):
                        callees.append(child.func.attr)
            if callees:
                call_graph[caller] = callees

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                s, e = _span(node)
                symbols.append(SymbolInfo(
                    name=node.name, kind="class", file_path=filepath,
                    line_start=s, line_end=e, docstring=_docstring(node),
                ))
                _class_methods(node)
                _extract_calls(node, node.name)
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                s, e = _span(node)
                symbols.append(SymbolInfo(
                    name=node.name, kind="function", file_path=filepath,
                    line_start=s, line_end=e, docstring=_docstring(node),
                ))
                _extract_calls(node, node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                names = _assign_targets(node)
                for n in names:
                    s, e = _span(node)
                    symbols.append(SymbolInfo(
                        name=n, kind="variable", file_path=filepath,
                        line_start=s, line_end=e,
                    ))
            # Imports
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.append(ImportInfo(
                        file_path=filepath,
                        module=alias.name,
                        alias=alias.asname or "",
                    ))
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                for alias in node.names:
                    imports.append(ImportInfo(
                        file_path=filepath,
                        module=f"{mod}.{alias.name}" if mod else alias.name,
                        alias=alias.asname or "",
                    ))

        return symbols, imports, call_graph


# ---------------------------------------------------------------------------
# Regex fallback parser (for non-Python languages)
# ---------------------------------------------------------------------------

class _RegexParser:
    """Best-effort symbol extraction for non-Python files using regex."""

    # Patterns for common languages
    _PATTERNS: List[Tuple[str, re.Pattern]] = [
        ("function", re.compile(
            r"(?:function|def|fn|func)\s+([A-Za-z_]\w*)", re.MULTILINE
        )),
        ("class", re.compile(
            r"(?:class|struct|interface|enum)\s+([A-Za-z_]\w*)", re.MULTILINE
        )),
        ("variable", re.compile(
            r"(?:let|const|var|val)\s+([A-Za-z_]\w*)", re.MULTILINE
        )),
    ]

    _IMPORT_PATTERNS: List[re.Pattern] = [
        re.compile(r"import\s+[\"']([^\"']+)[\"']"),
        re.compile(r"from\s+([^\s]+)\s+import"),
        re.compile(r"require\s*\(\s*[\"']([^\"']+)[\"']\s*\)"),
        re.compile(r"include\s*[\"']([^\"']+)[\"']"),
        re.compile(r"#include\s*[<\"]([^>\"]+)[>\"]"),
    ]

    @classmethod
    def parse(
        cls, source: str, filepath: str
    ) -> Tuple[List[SymbolInfo], List[ImportInfo], Dict[str, List[str]]]:
        symbols: List[SymbolInfo] = []
        imports: List[ImportInfo] = []
        call_graph: Dict[str, List[str]] = {}
        lines = source.splitlines()

        for kind, pat in cls._PATTERNS:
            for m in pat.finditer(source):
                line_no = source[: m.start()].count("\n") + 1
                # Grab surrounding context for a snippet
                start = max(0, line_no - 1)
                end = min(len(lines), line_no + 2)
                snippet = "\n".join(lines[start:end]).strip()
                symbols.append(SymbolInfo(
                    name=m.group(1), kind=kind, file_path=filepath,
                    line_start=line_no, line_end=line_no, docstring=snippet,
                ))

        for pat in cls._IMPORT_PATTERNS:
            for m in pat.finditer(source):
                imports.append(ImportInfo(
                    file_path=filepath, module=m.group(1),
                ))

        return symbols, imports, call_graph


# ---------------------------------------------------------------------------
# Main indexer
# ---------------------------------------------------------------------------

class SmartCodebaseIndexer:
    """
    Codebase indexer with FTS5 search, AST parsing, and incremental updates.

    Stores file metadata, symbols, imports, and dependencies in SQLite.
    Python files are parsed with ``ast`` for precise results; other languages
    fall back to regex heuristics.
    """

    def __init__(self, db_path: Optional[str] = None):
        if db_path is None:
            home = Path.home()
            db_path = str(home / ".nexus" / "brain" / "codebase_index.db")

        self._db_path = db_path
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._init_schema()
        logger.info("SmartCodebaseIndexer ready — db=%s", db_path)

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _init_schema(self) -> None:
        cur = self._conn.cursor()
        cur.executescript("""
            CREATE TABLE IF NOT EXISTS files (
                path        TEXT PRIMARY KEY,
                language    TEXT NOT NULL DEFAULT 'unknown',
                size        INTEGER NOT NULL DEFAULT 0,
                hash        TEXT NOT NULL DEFAULT '',
                indexed_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS symbols (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                kind        TEXT NOT NULL DEFAULT 'variable',
                file_path   TEXT NOT NULL,
                line_start  INTEGER NOT NULL DEFAULT 0,
                line_end    INTEGER NOT NULL DEFAULT 0,
                docstring   TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name);
            CREATE INDEX IF NOT EXISTS idx_symbols_file ON symbols(file_path);

            CREATE TABLE IF NOT EXISTS imports (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path   TEXT NOT NULL,
                module      TEXT NOT NULL,
                alias       TEXT NOT NULL DEFAULT '',
                FOREIGN KEY (file_path) REFERENCES files(path) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS dependencies (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                from_file   TEXT NOT NULL,
                to_file     TEXT NOT NULL,
                dep_type    TEXT NOT NULL DEFAULT 'import',
                FOREIGN KEY (from_file) REFERENCES files(path) ON DELETE CASCADE,
                FOREIGN KEY (to_file)   REFERENCES files(path) ON DELETE CASCADE
            );

            -- FTS5 virtual table for code search
            CREATE VIRTUAL TABLE IF NOT EXISTS code_search USING fts5(
                file_path,
                symbol_name,
                docstring,
                content_snippet,
                tokenize='porter unicode61'
            );
        """)
        self._conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index_directory(self, path: str, recursive: bool = True) -> int:
        """
        Index all source files under *path*.

        Returns the number of files indexed.
        """
        count = 0
        root = Path(path)
        if not root.is_dir():
            logger.warning("Not a directory: %s", path)
            return 0

        patterns = [f"*{ext}" for ext in _LANG_EXTENSIONS]
        files_to_index = []
        for pattern in patterns:
            if recursive:
                files_to_index.extend(root.rglob(pattern))
            else:
                files_to_index.extend(root.glob(pattern))

        for fpath in files_to_index:
            if any(part in _SKIP_DIRS or part.startswith(".") for part in fpath.parts):
                continue
            try:
                self.incremental_update(str(fpath))
                count += 1
            except Exception as exc:
                logger.debug("Skip %s: %s", fpath, exc)

        logger.info("Indexed %d files from %s", count, path)
        return count

    def search_code(
        self, query: str, language: Optional[str] = None, limit: int = 10
    ) -> List[SearchResult]:
        """
        Full-text search across indexed symbols, docstrings, and content.

        Uses FTS5 with BM25 ranking.  Splits CamelCase and multi-word
        queries so the porter/unicode61 tokenizer can match individual
        tokens.
        """
        # Split query into individual searchable tokens
        # "SmartCodebaseIndexer" -> ["Smart", "Codebase", "Indexer"]
        # "get_indexer foo bar" -> ["get", "indexer", "foo", "bar"]
        tokens = re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?=[A-Z]|$)|\w+", query)
        if not tokens:
            return []

        # Build FTS5 OR query so any token match counts
        fts_expr = " OR ".join(tokens)

        sql = """
            SELECT file_path, symbol_name, docstring, content_snippet,
                   rank
            FROM code_search
            WHERE code_search MATCH ?
        """
        params: list = [fts_expr]

        if language:
            sql += " AND file_path IN (SELECT path FROM files WHERE language = ?)"
            params.append(language)

        sql += " ORDER BY rank LIMIT ?"
        params.append(limit)

        results: List[SearchResult] = []
        try:
            with self._lock:
                for row in self._conn.execute(sql, params):
                    file_path, sym_name, doc, snippet, _rank = row
                    # Determine match type
                    mt = "content"
                    if sym_name and query.lower() in sym_name.lower():
                        mt = "symbol"
                    elif doc and query.lower() in doc.lower():
                        mt = "docstring"
                    results.append(SearchResult(
                        file_path=file_path,
                        line=0,
                        snippet=snippet or doc or sym_name or "",
                        match_type=mt,
                        score=abs(_rank) if _rank else 0.0,
                    ))
        except sqlite3.OperationalError as exc:
            logger.warning("FTS5 search error: %s", exc)

        return results

    def get_file_context(
        self, filepath: str, line_range: Optional[Tuple[int, int]] = None
    ) -> str:
        """
        Return file content (optionally within *line_range*).

        Falls back to disk read if file is not cached.
        """
        # Try reading from the file directly
        try:
            with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except FileNotFoundError:
            return ""

        if line_range:
            start, end = line_range
            start = max(0, start - 1)  # 1-indexed → 0-indexed
            end = min(len(lines), end)
            return "".join(lines[start:end])

        return "".join(lines)

    def get_symbol_info(self, name: str) -> List[SymbolInfo]:
        """Look up symbols by exact name."""
        rows = self._conn.execute(
            "SELECT name, kind, file_path, line_start, line_end, docstring "
            "FROM symbols WHERE name = ?",
            (name,),
        ).fetchall()
        return [
            SymbolInfo(name=r[0], kind=r[1], file_path=r[2],
                       line_start=r[3], line_end=r[4], docstring=r[5])
            for r in rows
        ]

    def get_imports(self, filepath: str) -> List[ImportInfo]:
        """Return all imports for a given file."""
        rows = self._conn.execute(
            "SELECT file_path, module, alias FROM imports WHERE file_path = ?",
            (filepath,),
        ).fetchall()
        return [ImportInfo(file_path=r[0], module=r[1], alias=r[2]) for r in rows]

    def get_dependencies(self, filepath: str) -> List[DependencyInfo]:
        """Return files that *filepath* depends on (outgoing deps)."""
        rows = self._conn.execute(
            "SELECT from_file, to_file, dep_type FROM dependencies "
            "WHERE from_file = ?",
            (filepath,),
        ).fetchall()
        return [
            DependencyInfo(from_file=r[0], to_file=r[1], dep_type=r[2])
            for r in rows
        ]

    def get_call_graph(self, function_name: str) -> Dict[str, List[str]]:
        """
        Return the call graph rooted at *function_name*.

        Searches the symbol table and reconstructs a shallow graph
        (direct callees only — deep traversal left to the consumer).
        """
        # Find all functions in all indexed files and build a merged graph
        graph: Dict[str, List[str]] = {}
        rows = self._conn.execute(
            "SELECT DISTINCT file_path FROM symbols WHERE kind IN "
            "('function', 'method')"
        ).fetchall()
        for (fp,) in rows:
            content = self.get_file_context(fp)
            if not content:
                continue
            lang = _detect_language(fp)
            if lang == "python":
                _, _, cg = _PythonParser.parse(content, fp)
            else:
                _, _, cg = _RegexParser.parse(content, fp)
            graph.update(cg)

        # Return subgraph reachable from function_name
        if function_name in graph:
            return {function_name: graph[function_name]}
        # Search partial match
        for k, v in graph.items():
            if function_name in k or function_name in v:
                return {k: v}
        return {}

    def incremental_update(self, filepath: str, content: Optional[str] = None) -> bool:
        """
        Re-index a single file (e.g. after an edit).

        If *content* is not provided the file is read from disk.
        Returns True if the file was actually updated (hash changed).
        """
        if content is None:
            try:
                with open(filepath, "r", encoding="utf-8", errors="replace") as fh:
                    content = fh.read()
            except FileNotFoundError:
                self._remove_file(filepath)
                return False

        raw = content.encode("utf-8")
        new_hash = _file_hash(raw)

        # Check if already up-to-date
        with self._lock:
            row = self._conn.execute(
                "SELECT hash FROM files WHERE path = ?", (filepath,)
            ).fetchone()
            if row and row[0] == new_hash:
                return False  # no change

        lang = _detect_language(filepath)
        now = datetime.utcnow().isoformat()

        with self._lock:
            # Upsert file entry
            self._conn.execute(
                "INSERT OR REPLACE INTO files (path, language, size, hash, indexed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (filepath, lang, len(raw), new_hash, now),
            )
            # Clear old data for this file
            self._conn.execute("DELETE FROM symbols WHERE file_path = ?", (filepath,))
            self._conn.execute("DELETE FROM imports WHERE file_path = ?", (filepath,))
            self._conn.execute(
                "DELETE FROM dependencies WHERE from_file = ? OR to_file = ?",
                (filepath, filepath),
            )
            # Remove old FTS rows
            self._conn.execute(
                "DELETE FROM code_search WHERE file_path = ?", (filepath,)
            )

            # Parse
            if lang == "python":
                symbols, imports, call_graph = _PythonParser.parse(content, filepath)
            else:
                symbols, imports, call_graph = _RegexParser.parse(content, filepath)

            # Insert symbols
            for sym in symbols:
                self._conn.execute(
                    "INSERT INTO symbols (name, kind, file_path, line_start, line_end, docstring) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (sym.name, sym.kind, filepath, sym.line_start, sym.line_end, sym.docstring),
                )
                # FTS row
                snippet = content[max(0, (sym.line_start - 1) * 80):
                                  sym.line_end * 80].replace("\0", " ")[:500]
                self._conn.execute(
                    "INSERT INTO code_search (file_path, symbol_name, docstring, content_snippet) "
                    "VALUES (?, ?, ?, ?)",
                    (filepath, sym.name, sym.docstring, snippet),
                )

            # Insert imports
            for imp in imports:
                self._conn.execute(
                    "INSERT INTO imports (file_path, module, alias) VALUES (?, ?, ?)",
                    (imp.file_path, imp.module, imp.alias),
                )

            # Build dependency edges (module → file lookup)
            for imp in imports:
                target = self._resolve_module(imp.module)
                if target and target != filepath:
                    self._conn.execute(
                        "INSERT INTO dependencies (from_file, to_file, dep_type) "
                        "VALUES (?, ?, 'import')",
                        (filepath, target),
                    )

            self._conn.commit()

        logger.debug("Indexed %s (%d symbols)", filepath, len(symbols))
        return True

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _remove_file(self, filepath: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM files WHERE path = ?", (filepath,))
            self._conn.execute("DELETE FROM symbols WHERE file_path = ?", (filepath,))
            self._conn.execute("DELETE FROM imports WHERE file_path = ?", (filepath,))
            self._conn.execute(
                "DELETE FROM dependencies WHERE from_file = ? OR to_file = ?",
                (filepath, filepath),
            )
            self._conn.execute("DELETE FROM code_search WHERE file_path = ?", (filepath,))
            self._conn.commit()

    def _resolve_module(self, module: str) -> Optional[str]:
        """Best-effort resolution of a module name to a file path."""
        # Try exact match first
        row = self._conn.execute(
            "SELECT path FROM files WHERE path LIKE ?",
            (f"%{module.replace('.', os.sep)}%",),
        ).fetchone()
        if row:
            return row[0]
        # Try as a file path
        parts = module.split(".")
        candidate = os.sep.join(parts)
        for ext in (".py", ".js", ".ts", ".rs", ".go", ".java"):
            candidate_path = candidate + ext
            row = self._conn.execute(
                "SELECT path FROM files WHERE path LIKE ?",
                (f"%{candidate_path}",),
            ).fetchone()
            if row:
                return row[0]
        return None

    def get_stats(self) -> Dict[str, Any]:
        """Return indexing statistics."""
        with self._lock:
            file_count = self._conn.execute("SELECT COUNT(*) FROM files").fetchone()[0]
            sym_count = self._conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
            imp_count = self._conn.execute("SELECT COUNT(*) FROM imports").fetchone()[0]
            dep_count = self._conn.execute(
                "SELECT COUNT(*) FROM dependencies"
            ).fetchone()[0]
            lang_rows = self._conn.execute(
                "SELECT language, COUNT(*) FROM files GROUP BY language"
            ).fetchall()
        return {
            "files": file_count,
            "symbols": sym_count,
            "imports": imp_count,
            "dependencies": dep_count,
            "languages": {r[0]: r[1] for r in lang_rows},
        }

    def close(self) -> None:
        """Close the database connection."""
        self._conn.close()


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_instance: Optional[SmartCodebaseIndexer] = None
_instance_lock = threading.Lock()


def get_indexer(db_path: Optional[str] = None) -> SmartCodebaseIndexer:
    """Return the global SmartCodebaseIndexer singleton."""
    global _instance
    if _instance is None:
        with _instance_lock:
            if _instance is None:
                _instance = SmartCodebaseIndexer(db_path=db_path)
    return _instance
