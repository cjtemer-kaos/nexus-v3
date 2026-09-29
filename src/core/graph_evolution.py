"""
F6: Graph Evolution (Self-Healing)

Rewrite graph topology on failure, retry with new structure.
Tracks evolution history and quality metrics.
"""

import logging
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger("nexus-evolution")


class GraphQuality(Enum):
    CLEAN = "clean"
    DEGRADED = "degraded"
    FAILED = "failed"


@dataclass
class EvolutionEvent:
    id: str
    timestamp: str
    trigger: str
    changes: Dict
    quality_before: str
    quality_after: str


@dataclass
class TaskGraph:
    id: str
    nodes: Dict[str, Dict] = field(default_factory=dict)
    edges: List[Dict] = field(default_factory=list)
    quality: GraphQuality = GraphQuality.CLEAN
    evolution_count: int = 0
    max_evolutions: int = 3
    history: List[EvolutionEvent] = field(default_factory=list)


class GraphEvolution:
    """Self-healing task graph that evolves on failure"""

    def __init__(self):
        self._graphs: Dict[str, TaskGraph] = {}
        self._rewrite_rules = self._default_rules()

    def _default_rules(self) -> List[Dict]:
        return [
            {"name": "add_fallback", "trigger": "node_failure", "action": "add_fallback_node"},
            {"name": "simplify_path", "trigger": "repeated_failure", "action": "remove_complex_nodes"},
            {"name": "parallelize", "trigger": "sequential_bottleneck", "action": "split_to_parallel"},
            {"name": "add_retry", "trigger": "transient_failure", "action": "add_retry_step"},
            {"name": "bypass", "trigger": "non_critical_failure", "action": "bypass_failed_node"},
        ]

    def create_graph(self, graph_id: str, nodes: Dict, edges: List[Dict]) -> TaskGraph:
        graph = TaskGraph(id=graph_id, nodes=nodes, edges=edges)
        self._graphs[graph_id] = graph
        return graph

    def add_node(self, node_id: str, title: str, graph_id: str = "default"):
        if graph_id not in self._graphs:
            self._graphs[graph_id] = TaskGraph(id=graph_id)
        self._graphs[graph_id].nodes[node_id] = {
            "title": title, "description": title, "status": "pending",
        }

    def add_edge(self, from_node: str, to_node: str, graph_id: str = "default"):
        if graph_id not in self._graphs:
            self._graphs[graph_id] = TaskGraph(id=graph_id)
        self._graphs[graph_id].edges.append({"from": from_node, "to": to_node})

    def get_node(self, node_id: str, graph_id: str = "default") -> Optional[Dict]:
        graph = self._graphs.get(graph_id)
        return graph.nodes.get(node_id) if graph else None

    def get_node_count(self, graph_id: str = "default") -> int:
        graph = self._graphs.get(graph_id)
        return len(graph.nodes) if graph else 0

    def get_failure_count(self, node_id: str, graph_id: str = "default") -> int:
        graph = self._graphs.get(graph_id)
        if not graph:
            return 0
        node = graph.nodes.get(node_id, {})
        return node.get("failure_count", 0)

    def get_healing_suggestion(self, node_id: str, graph_id: str = "default") -> Optional[str]:
        graph = self._graphs.get(graph_id)
        if not graph:
            return None
        node = graph.nodes.get(node_id, {})
        if node.get("status") == "failed":
            return f"Node {node_id} failed. Try: add retry, simplify, or use fallback."
        if node.get("failure_count", 0) >= 2:
            return f"Node {node_id} failing repeatedly. Consider simplifying or bypassing."
        return None

    def rewrite_node(self, node_id: str, new_title: str, graph_id: str = "default"):
        graph = self._graphs.get(graph_id)
        if graph and node_id in graph.nodes:
            graph.nodes[node_id]["title"] = new_title
            graph.nodes[node_id]["description"] = new_title

    def record_failure(self, graph_id: str, node_id: str, error: str) -> Optional[TaskGraph]:
        graph = self._graphs.get(graph_id)
        if not graph:
            return None

        if node_id in graph.nodes:
            graph.nodes[node_id]["status"] = "failed"
            graph.nodes[node_id]["error"] = error

        # Check if evolution is needed
        if graph.evolution_count >= graph.max_evolutions:
            graph.quality = GraphQuality.FAILED
            logger.warning(f"Graph {graph_id}: max evolutions reached, marking as FAILED")
            return graph

        # Apply rewrite rules
        evolved = self._apply_rewrite_rules(graph, node_id, error)
        if evolved:
            graph.evolution_count += 1
            graph.quality = GraphQuality.DEGRADED if graph.evolution_count < graph.max_evolutions else GraphQuality.FAILED
            logger.info(f"Graph {graph_id} evolved (generation {graph.evolution_count})")

        return graph

    def _apply_rewrite_rules(self, graph: TaskGraph, failed_node: str, error: str) -> bool:
        """Apply the first matching rewrite rule"""
        failed_node_data = graph.nodes.get(failed_node, {})
        error_lower = error.lower()

        # Rule: transient failure -> add retry
        if any(kw in error_lower for kw in ["timeout", "connection", "temporary", "rate"]):
            self._add_retry(graph, failed_node)
            return True

        # Rule: non-critical failure -> bypass
        if failed_node_data.get("critical", True) is False:
            self._bypass_node(graph, failed_node)
            return True

        # Rule: repeated failure -> simplify
        if failed_node_data.get("failure_count", 0) >= 2:
            self._simplify_path(graph, failed_node)
            return True

        # Default: add fallback
        self._add_fallback(graph, failed_node)
        return True

    def _add_retry(self, graph: TaskGraph, node_id: str):
        node = graph.nodes[node_id]
        if "retries" not in node:
            node["retries"] = 0
            node["max_retries"] = 3
        node["retries"] = node.get("retries", 0) + 1
        if node["retries"] >= node["max_retries"]:
            node["status"] = "failed"
            node["error"] = f"Max retries ({node['max_retries']}) exceeded"
        else:
            node["status"] = "retry"
        self._add_event(graph, "transient_failure", {"action": "add_retry", "node": node_id, "retries": node["retries"]})

    def _bypass_node(self, graph: TaskGraph, node_id: str):
        graph.nodes[node_id]["status"] = "bypassed"
        # Update edges to skip this node
        new_edges = []
        for edge in graph.edges:
            if edge.get("to") == node_id:
                for other_edge in graph.edges:
                    if other_edge.get("from") == node_id:
                        new_edges.append({"from": edge.get("from"), "to": other_edge.get("to")})
            elif edge.get("from") != node_id:
                new_edges.append(edge)
        graph.edges = new_edges
        self._add_event(graph, "non_critical_failure", {"action": "bypass", "node": node_id})

    def _simplify_path(self, graph: TaskGraph, node_id: str):
        graph.nodes[node_id]["simplified"] = True
        graph.nodes[node_id]["description"] = f"[Simplified] {graph.nodes[node_id].get('description', '')}"
        self._add_event(graph, "repeated_failure", {"action": "simplify", "node": node_id})

    def _add_fallback(self, graph: TaskGraph, node_id: str):
        fallback_id = f"{node_id}_fallback"
        graph.nodes[fallback_id] = {
            "title": f"Fallback for {node_id}",
            "description": f"Alternative approach if {node_id} fails",
            "status": "pending",
            "is_fallback": True,
        }
        graph.edges.append({"from": node_id, "to": fallback_id, "condition": "on_failure"})
        self._add_event(graph, "node_failure", {"action": "add_fallback", "node": node_id, "fallback": fallback_id})

    def _add_event(self, graph: TaskGraph, trigger: str, changes: Dict):
        import uuid
        event = EvolutionEvent(
            id=str(uuid.uuid4())[:8],
            timestamp=datetime.now().isoformat(),
            trigger=trigger,
            changes=changes,
            quality_before=graph.quality.value,
            quality_after=graph.quality.value,
        )
        graph.history.append(event)

    def get_graph(self, graph_id: str) -> Optional[TaskGraph]:
        return self._graphs.get(graph_id)

    def get_stats(self) -> Dict:
        total = len(self._graphs)
        clean = sum(1 for g in self._graphs.values() if g.quality == GraphQuality.CLEAN)
        degraded = sum(1 for g in self._graphs.values() if g.quality == GraphQuality.DEGRADED)
        failed = sum(1 for g in self._graphs.values() if g.quality == GraphQuality.FAILED)
        total_evolutions = sum(g.evolution_count for g in self._graphs.values())
        return {
            "total_graphs": total,
            "clean": clean,
            "degraded": degraded,
            "failed": failed,
            "total_evolutions": total_evolutions,
            "rewrite_rules": len(self._rewrite_rules),
        }
