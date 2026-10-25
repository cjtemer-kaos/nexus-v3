"""
Code Graph — Knowledge graph queries for code intelligence.

Provides:
- build_graph: Build/rebuild the code knowledge graph from source
- query_graph: Search nodes by label/name
- get_neighbors: Explore node relationships
- get_god_nodes: Top connected entities
- find_surprising: Cross-community edge discovery
- find_cycles: Circular import detection
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# Ensure project root is on sys.path for imports from src.codegraph.*
import sys as _sys
_proj_root_str = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _proj_root_str not in _sys.path:
    _sys.path.insert(0, _proj_root_str)

# Project root (src/tools/codegraph_tool.py -> src -> project root)
_PROJECT_ROOT = Path(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
CODEGRAPH_DIR = Path(os.environ.get("CODEGRAPH_DIR", str(_PROJECT_ROOT / ".codegraph")))
DEFAULT_GRAPH_PATH = CODEGRAPH_DIR / "graph.json"


def _get_graph():
    """Load the graph from JSON if available."""
    if not DEFAULT_GRAPH_PATH.exists():
        return None
    try:
        import networkx as nx
        data = json.loads(DEFAULT_GRAPH_PATH.read_text(encoding="utf-8"))
        G = nx.node_link_graph(data, directed=data.get("metadata", {}).get("is_directed", False))
        return G
    except Exception as e:
        logger.warning(f"Failed to load graph: {e}")
        return None


async def build_graph(
    source_dir: str = "src",
    output_dir: str = ".codegraph",
    force: bool = False,
) -> dict:
    """Build code knowledge graph from source files.

    Uses simple AST extraction for Python files, discovers
    functions, classes, imports, and inheritance relationships.

    Args:
        source_dir: Directory to scan (default: src)
        output_dir: Output directory for graph artifacts (default: .codegraph)
        force: Rebuild even if graph exists (default: False)

    Returns:
        Dict with node_count, edge_count, community_count, paths
    """
    # Make source_dir absolute relative to project root
    if not os.path.isabs(source_dir):
        source_dir = os.path.join(_proj_root_str, source_dir)
    output_dir = os.path.join(_proj_root_str, output_dir)
    from src.codegraph.builder import build
    from src.codegraph.community import cluster
    from src.codegraph.export import to_json, to_html

    if not force and Path(output_dir, "graph.json").exists():
        G = _get_graph()
        if G is not None:
            return {
                "status": "already_exists",
                "node_count": G.number_of_nodes(),
                "edge_count": G.number_of_edges(),
                "graph_path": str(Path(output_dir, "graph.json")),
                "html_path": str(Path(output_dir, "graph.html")),
            }

    source_dir = source_dir.rstrip("/\\")
    if not os.path.isdir(source_dir):
        return {"error": f"Directory not found: {source_dir}"}


    extractions = []
    file_count = 0
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_"))]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                ext = _extract_python(fpath)
                if ext:
                    extractions.append(ext)
                    file_count += 1
            except Exception as e:
                logger.warning(f"Failed to extract {fpath}: {e}")

    if not extractions:
        return {"error": "No Python files found or extracted"}

    G = build(extractions, directed=True, dedup=True)
    communities = cluster(G, resolution=1.2)
    node_comm = {n: cid for cid, nodes in communities.items() for n in nodes}
    import networkx as nx
    nx.set_node_attributes(G, node_comm, "community")

    os.makedirs(output_dir, exist_ok=True)
    to_json(G, os.path.join(output_dir, "graph.json"))
    to_html(G, os.path.join(output_dir, "graph.html"), title=f"Code Graph: {source_dir}")

    return {
        "status": "built",
        "node_count": G.number_of_nodes(),
        "edge_count": G.number_of_edges(),
        "community_count": len(communities),
        "file_count": file_count,
        "graph_path": str(Path(output_dir, "graph.json")),
        "html_path": str(Path(output_dir, "graph.html")),
    }


async def query_graph(query: str, top_k: int = 10) -> dict:
    """Search the code graph for nodes matching a query.

    Uses TF-IDF scoring across node labels. Returns ranked matches.

    Args:
        query: Search term (e.g. "rag engine", "memory", "agent loop")
        top_k: Maximum results (default: 10)

    Returns:
        Dict with results list (id, label, source_file, score)
    """
    G = _get_graph()
    if G is None:
        return {"error": "No hay grafo de codigo. Requiere el paquete src/codegraph + networkx (no presentes en nexus-v3)."}

    from src.codegraph.query import GraphQuery
    q = GraphQuery(G)
    results = q.query(query, top_k=top_k)
    return {"query": query, "count": len(results), "results": results}


async def get_neighbors(
    node_id: str,
    relation: str = "",
    max_depth: int = 1,
    max_nodes: int = 50,
) -> dict:
    """Explore neighbors of a node in the code graph.

    Args:
        node_id: Node ID (e.g. "core/rag_engine.py::RAGEngine")
        relation: Filter by relation type (calls, imports, inherits, etc.)
        max_depth: Traversal depth (default: 1)
        max_nodes: Max neighbors to return (default: 50)

    Returns:
        Dict with neighbors list and metadata
    """
    G = _get_graph()
    if G is None:
        return {"error": "No hay grafo de codigo. Requiere el paquete src/codegraph + networkx (no presentes en nexus-v3)."}
    if node_id not in G:
        return {"error": f"Node not found: {node_id}"}

    from src.codegraph.query import GraphQuery
    q = GraphQuery(G)
    rel = relation if relation else None
    neighbors = q.neighbors(node_id, relation=rel, max_depth=max_depth, max_nodes=max_nodes)

    node_data = G.nodes[node_id]
    return {
        "node_id": node_id,
        "label": node_data.get("label", node_id),
        "source_file": node_data.get("source_file", ""),
        "degree": G.degree(node_id),
        "neighbor_count": len(neighbors),
        "neighbors": neighbors,
    }


async def get_god_nodes(top_n: int = 10) -> dict:
    """Find the most connected entities in the code graph.

    These are the core abstractions with highest degree centrality.
    File-level hubs and concept nodes are excluded.

    Args:
        top_n: Number of top nodes (default: 10)

    Returns:
        Dict with ranked god nodes list
    """
    G = _get_graph()
    if G is None:
        return {"error": "No hay grafo de codigo. Requiere el paquete src/codegraph + networkx (no presentes en nexus-v3)."}

    from src.codegraph.analyze import god_nodes
    nodes = god_nodes(G, top_n=top_n)
    return {"count": len(nodes), "nodes": nodes}


async def find_surprising(top_n: int = 5) -> dict:
    """Find surprising cross-community connections in the code graph.

    Reveals non-obvious relationships between structurally distant
    parts of the codebase (cross-module calls, cross-language refs).

    Args:
        top_n: Max results (default: 5)

    Returns:
        Dict with surprising connections list
    """
    G = _get_graph()
    if G is None:
        return {"error": "No hay grafo de codigo. Requiere el paquete src/codegraph + networkx (no presentes en nexus-v3)."}

    node_comm: dict[str, int] = {}
    for n, d in G.nodes(data=True):
        c = d.get("community")
        if c is not None:
            node_comm[n] = c
    communities: dict[int, list[str]] = {}
    for n, c in node_comm.items():
        communities.setdefault(c, []).append(n)

    from src.codegraph.analyze import surprising_connections
    surprises = surprising_connections(G, communities, top_n=top_n)
    return {"count": len(surprises), "connections": surprises}


async def find_cycles(max_length: int = 5, top_n: int = 20) -> dict:
    """Detect circular import dependencies.

    Collapses symbol-level nodes to file level, builds a directed
    import graph, then finds simple cycles.

    Args:
        max_length: Max cycle length to report (default: 5)
        top_n: Max cycles to return (default: 20)

    Returns:
        Dict with cycles list
    """
    G = _get_graph()
    if G is None:
        return {"error": "No hay grafo de codigo. Requiere el paquete src/codegraph + networkx (no presentes en nexus-v3)."}

    from src.codegraph.analyze import find_import_cycles
    cycles = find_import_cycles(G, max_cycle_length=max_length, top_n=top_n)
    return {"count": len(cycles), "cycles": cycles}


def _extract_python(filepath: str) -> dict | None:
    """Minimal AST extraction for a single Python file."""
    import ast
    import hashlib

    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    relpath = os.path.relpath(filepath).replace("\\", "/")
    tree = ast.parse(content)
    nodes = []
    edges = []

    body_hash = hashlib.sha256(content.encode()).hexdigest()[:16]
    nodes.append({
        "id": relpath,
        "label": os.path.basename(filepath),
        "file_type": "code",
        "source_file": relpath,
        "body_hash": body_hash,
    })

    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            nid = f"{relpath}::{node.name}"
            nodes.append({
                "id": nid, "label": f"{node.name}()",
                "file_type": "code", "source_file": relpath,
                "signature": f"def {node.name}",
                "is_async": isinstance(node, ast.AsyncFunctionDef),
            })
            edges.append({
                "source": relpath, "target": nid,
                "relation": "contains", "confidence": "EXTRACTED",
            })
            for d in node.decorator_list:
                if isinstance(d, ast.Name):
                    edges.append({
                        "source": nid, "target": d.id,
                        "relation": "references", "confidence": "EXTRACTED",
                    })
        elif isinstance(node, ast.ClassDef):
            nid = f"{relpath}::{node.name}"
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            nodes.append({
                "id": nid, "label": node.name,
                "file_type": "code", "source_file": relpath,
                "bases": bases,
            })
            edges.append({
                "source": relpath, "target": nid,
                "relation": "contains", "confidence": "EXTRACTED",
            })
            for base in bases:
                edges.append({
                    "source": nid, "target": base,
                    "relation": "inherits", "confidence": "EXTRACTED",
                })
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    mid = f"{nid}::{item.name}"
                    nodes.append({
                        "id": mid, "label": f"{item.name}()",
                        "file_type": "code", "source_file": relpath,
                    })
                    edges.append({
                        "source": nid, "target": mid,
                        "relation": "contains", "confidence": "EXTRACTED",
                    })
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module.split(".")[0])

    for imp in sorted(imports):
        if imp and imp != os.path.basename(relpath).replace(".py", ""):
            edges.append({
                "source": relpath, "target": f"{imp}.py",
                "relation": "imports_from", "confidence": "EXTRACTED",
            })

    return {"nodes": nodes, "edges": edges}
