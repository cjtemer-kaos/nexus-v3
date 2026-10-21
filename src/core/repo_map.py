"""Repo-map compacto: simbolos + imports + PageRank con presupuesto.

tree-sitter es opcional (extra ``repomap``). Si no esta instalado, se usa un
fallback regex estable. El mapa se ordena por PageRank del grafo de imports:
los archivos de los que todos dependen aparecen primero, y el texto se corta
por presupuesto de tokens (chars/4) para no inundar el contexto.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_SKIP_DIRS = {
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv",
    ".venv-ft314", "dist", "build", ".next", ".nuxt", "target", "vendor",
    "data", "brain", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
_EXTS = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs",
    ".go", ".rs", ".java", ".kt", ".kts", ".c", ".h", ".cc", ".cpp",
    ".hpp", ".cs", ".rb", ".php", ".swift", ".scala",
}

_PY_SYM = re.compile(r"^\s*(?:async\s+)?(?:class|def)\s+([A-Za-z_]\w*)", re.M)
_JS_SYM = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:class|function|const|let|var)\s+"
    r"([A-Za-z_$][\w$]*)", re.M)
_GO_SYM = re.compile(r"^\s*(?:func|type)\s+(?:\([^)]*\)\s*)?([A-Za-z_]\w*)", re.M)
_RS_SYM = re.compile(
    r"^\s*(?:pub\s+)?(?:async\s+)?(?:fn|struct|enum|trait|impl|mod)\s+"
    r"([A-Za-z_]\w*)", re.M)

_PY_IMPORT = re.compile(
    r"^\s*(?:from\s+([\w\.]+)\s+import|import\s+([\w\.]+))", re.M)
_JS_IMPORT = re.compile(
    r"""(?:from\s+|require\(\s*)['"]([^'"]+)['"]""", re.M)
_GO_IMPORT = re.compile(r'^\s*"([^"]+)"', re.M)


@dataclass
class FileInfo:
    path: str
    symbols: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)
    rank: float = 1.0

    def to_dict(self) -> dict:
        return {"path": self.path, "symbols": self.symbols[:12],
                "imports": self.imports[:12], "rank": round(self.rank, 4)}


def _iter_files(root: Path, max_files: int) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if len(out) >= max_files:
                return out
            if Path(name).suffix.lower() in _EXTS:
                out.append(Path(dirpath) / name)
    return out


# Gramaticas individuales (paquetes con wheels abi3). El extra ``repomap``
# instala las que tenemos; las demas degradan a regex sin romper el mapa.
_TS_GRAMMARS: dict[str, tuple[str, str]] = {
    ".py": ("tree_sitter_python", "language"),
    ".pyw": ("tree_sitter_python", "language"),
    ".js": ("tree_sitter_javascript", "language"),
    ".jsx": ("tree_sitter_javascript", "language"),
    ".mjs": ("tree_sitter_javascript", "language"),
    ".cjs": ("tree_sitter_javascript", "language"),
    ".ts": ("tree_sitter_typescript", "language_typescript"),
    ".tsx": ("tree_sitter_typescript", "language_tsx"),
    ".go": ("tree_sitter_go", "language"),
    ".rs": ("tree_sitter_rust", "language"),
}

# Nodos que declaran un simbolo con nombre. Se resuelve por campo ``name`` y,
# si la gramatica no lo expone, por el primer identificador hijo.
_TS_NAME_NODES = frozenset({
    "function_definition", "class_definition",
    "function_declaration", "generator_function_declaration",
    "class_declaration", "method_definition", "variable_declarator",
    "interface_declaration", "type_alias_declaration", "enum_declaration",
    "abstract_class_declaration",
    "function_item", "struct_item", "enum_item", "trait_item", "impl_item",
    "mod_item", "type_item", "const_item", "static_item", "macro_definition",
    "method_declaration", "type_spec",
})
_TS_NAME_CHILD_TYPES = frozenset({
    "identifier", "field_identifier", "property_identifier",
    "type_identifier", "constant",
})

_TS_PARSERS: dict[str, Any] = {}
_TS_FAILED: set[str] = set()


def _tree_sitter_disabled() -> bool:
    return os.environ.get("NEXUS_REPOMAP", "").strip().lower() in (
        "regex", "off", "0", "false", "no")


def _tree_sitter_parser(suffix: str):
    """Parser cacheado por sufijo. None si falta la gramatica o no carga."""
    if _tree_sitter_disabled():
        return None
    if suffix in _TS_PARSERS:
        return _TS_PARSERS[suffix]
    if suffix in _TS_FAILED:
        return None
    spec = _TS_GRAMMARS.get(suffix)
    if not spec:
        return None
    module_name, attr = spec
    try:
        import importlib

        from tree_sitter import Language, Parser

        mod = importlib.import_module(module_name)
        raw = getattr(mod, attr)()
        language = raw if isinstance(raw, Language) else Language(raw)
        parser = Parser(language)
    except Exception:
        _TS_FAILED.add(suffix)
        return None
    _TS_PARSERS[suffix] = parser
    return parser


def _tree_sitter_available() -> bool:
    if _tree_sitter_disabled():
        return False
    try:
        import tree_sitter  # noqa: F401
    except Exception:
        return False
    return any(_tree_sitter_parser(suffix) is not None for suffix in _TS_GRAMMARS)


def _ts_node_name(node, source: bytes) -> str:
    try:
        name_node = node.child_by_field_name("name")
    except Exception:
        name_node = None
    if name_node is not None:
        return source[name_node.start_byte:name_node.end_byte].decode(
            "utf-8", "replace")
    for child in node.children:
        if child.type in _TS_NAME_CHILD_TYPES:
            return source[child.start_byte:child.end_byte].decode(
                "utf-8", "replace")
    return ""


def _tree_sitter_symbols(text: str, suffix: str) -> list[str] | None:
    """Intenta tree-sitter; devuelve None si no hay parser disponible."""
    parser = _tree_sitter_parser(suffix)
    if parser is None:
        return None
    source = text.encode("utf-8", "replace")
    try:
        tree = parser.parse(source)
        names: list[str] = []

        def walk(node) -> None:
            if node.type in _TS_NAME_NODES:
                name = _ts_node_name(node, source)
                if name:
                    names.append(name)
            for child in node.children:
                walk(child)

        walk(tree.root_node)
        return names
    except Exception:
        return None


def _symbols(text: str, suffix: str) -> list[str]:
    ts = _tree_sitter_symbols(text, suffix)
    if ts is not None:
        return _uniq(ts)
    if suffix == ".py":
        return _uniq(_PY_SYM.findall(text))
    if suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        return _uniq(_JS_SYM.findall(text))
    if suffix == ".go":
        return _uniq(_GO_SYM.findall(text))
    if suffix == ".rs":
        return _uniq(_RS_SYM.findall(text))
    return _uniq(_JS_SYM.findall(text) or _GO_SYM.findall(text))


def _imports(text: str, suffix: str) -> list[str]:
    if suffix == ".py":
        vals = [a or b for a, b in _PY_IMPORT.findall(text)]
    elif suffix in (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs"):
        vals = _JS_IMPORT.findall(text)
    elif suffix == ".go":
        vals = _GO_IMPORT.findall(text)
    else:
        vals = _JS_IMPORT.findall(text)
    return _uniq(vals)


def _uniq(vals: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in vals:
        v = str(v or "").strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _resolve_import(src: Path, imp: str, by_stem: dict[str, list[str]]) -> list[str]:
    """Resuelve imports locales de forma heuristica (sin ejecutar nada)."""
    if imp.startswith("."):
        dots = len(imp) - len(imp.lstrip("."))
        base = src.parent
        for _ in range(max(0, dots - 1)):
            base = base.parent
        mod = imp.lstrip(".").replace(".", "/")
        base = (base / mod) if mod else base
        cands = [base.with_suffix(s) for s in _EXTS]
        cands += [base / ("__init__" + s) for s in (".py",)]
        cands += [base / ("index" + s) for s in (".ts", ".tsx", ".js", ".jsx")]
        return [str(c) for c in cands if c.exists()]
    key = imp.replace("\\", "/").split("/")[-1]
    key = key.split(".")[0]
    return [p for p in by_stem.get(key, []) if p != str(src)][:8]


def _pagerank(nodes: list[str], edges: dict[str, set[str]],
              iterations: int = 24, damping: float = 0.85) -> dict[str, float]:
    n = max(1, len(nodes))
    ranks = {x: 1.0 / n for x in nodes}
    for _ in range(iterations):
        new = {x: (1.0 - damping) / n for x in nodes}
        for src, deps in edges.items():
            if not deps:
                continue
            share = ranks.get(src, 0.0) * damping / len(deps)
            for dst in deps:
                if dst in new:
                    new[dst] += share
        ranks = new
    return ranks


def build_repo_map(
    root: str = ".",
    *,
    budget_tokens: int = 2000,
    max_files: int = 300,
) -> dict:
    root_p = Path(root).resolve()
    if not root_p.exists() or not root_p.is_dir():
        raise ValueError(f"repo_map: no es un directorio: {root}")
    budget_tokens = max(128, min(20000, int(budget_tokens or 2000)))
    max_files = max(1, min(2000, int(max_files or 300)))
    files = _iter_files(root_p, max_files)
    infos: dict[str, FileInfo] = {}
    by_stem: dict[str, list[str]] = {}
    for p in files:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:400_000]
        except Exception:
            continue
        suffix = p.suffix.lower()
        rel = str(p.relative_to(root_p)).replace("\\", "/")
        infos[rel] = FileInfo(path=rel, symbols=_symbols(text, suffix),
                              imports=_imports(text, suffix))
        by_stem.setdefault(p.stem, []).append(str(p.resolve()))

    edges: dict[str, set[str]] = {rel: set() for rel in infos}
    for rel, info in infos.items():
        src = (root_p / rel).resolve()
        for imp in info.imports:
            for resolved in _resolve_import(src, imp, by_stem):
                try:
                    dst_rel = str(Path(resolved).resolve().relative_to(root_p)).replace("\\", "/")
                except Exception:
                    continue
                if dst_rel in infos and dst_rel != rel:
                    edges[rel].add(dst_rel)

    ranks = _pagerank(list(infos), edges)
    for rel, rank in ranks.items():
        infos[rel].rank = rank
    ordered = sorted(infos.values(), key=lambda x: (-x.rank, x.path))

    lines: list[str] = []
    used = 0
    included: list[dict] = []
    for info in ordered:
        symbols = ", ".join(info.symbols[:10])
        entry = f"{info.path} (rank={info.rank:.3f})"
        if symbols:
            entry += f": {symbols}"
        entry_tokens = max(1, len(entry) // 4)
        if used + entry_tokens > budget_tokens and lines:
            break
        lines.append(entry)
        used += entry_tokens
        included.append(info.to_dict())
    text = "\n".join(lines)
    return {
        "root": str(root_p),
        "files": included,
        "text": text,
        "stats": {
            "files_scanned": len(infos),
            "files_included": len(included),
            "edges": sum(len(v) for v in edges.values()),
            "budget_tokens": budget_tokens,
            "used_tokens": used,
            "truncated": len(included) < len(infos),
            "tree_sitter": _tree_sitter_available(),
        },
    }
