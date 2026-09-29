"""
Snapshot Ref System - Deterministic element references for browser snapshots.
Absorbed from agent-browser pattern — names cleaned.
"""

import logging
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class ElementRef:
    ref_id: str
    tag: str
    text: str
    role: str
    bounds: Optional[Dict] = None
    interactive: bool = True


@dataclass
class SnapshotResult:
    text: str
    refs: Dict[str, ElementRef] = field(default_factory=dict)
    token_count: int = 0
    element_count: int = 0


class RefMap:
    """Deterministic element reference mapping."""

    def __init__(self):
        self._refs: Dict[str, ElementRef] = {}
        self._counter = 0

    def clear(self):
        self._refs.clear()
        self._counter = 0

    def assign_ref(self, tag: str, text: str, role: str = "", bounds: Optional[Dict] = None) -> str:
        self._counter += 1
        ref_id = f"@e{self._counter}"
        self._refs[ref_id] = ElementRef(
            ref_id=ref_id, tag=tag, text=text, role=role, bounds=bounds
        )
        return ref_id

    def resolve(self, ref_id: str) -> Optional[ElementRef]:
        return self._refs.get(ref_id)

    def resolve_from_text(self, text: str) -> List[ElementRef]:
        matches = re.findall(r"@e\d+", text)
        return [self._refs[m] for m in matches if m in self._refs]

    @property
    def all_refs(self) -> Dict[str, ElementRef]:
        return dict(self._refs)


class SnapshotBuilder:
    """Build compact snapshots from accessibility trees with refs."""

    def __init__(self):
        self.ref_map = RefMap()

    def build_compact(self, ax_tree: Dict, max_tokens: int = 500) -> SnapshotResult:
        self.ref_map.clear()
        lines: List[str] = []
        self._walk_tree(ax_tree, lines, depth=0)
        text = "\n".join(lines)
        token_est = len(text.split())
        return SnapshotResult(
            text=text,
            refs=self.ref_map.all_refs,
            token_count=token_est,
            element_count=self.ref_map._counter,
        )

    def _walk_tree(self, node: Dict, lines: List[str], depth: int):
        role = node.get("role", "")
        name = node.get("name", "")
        tag = node.get("tagName", "")
        children = node.get("children", [])
        interactive_roles = {"button", "link", "textbox", "checkbox", "radio", "combobox", "menuitem", "tab", "option"}

        if role in interactive_roles or tag in {"input", "button", "a", "select", "textarea"}:
            ref_id = self.ref_map.assign_ref(tag, name, role)
            indent = "  " * depth
            lines.append(f"{indent}{ref_id} [{role or tag}] {name}")
        elif name and not name.startswith("__"):
            indent = "  " * depth
            lines.append(f"{indent}{name}")

        for child in children:
            if isinstance(child, dict):
                self._walk_tree(child, lines, depth + 1)

    def build_from_text_nodes(self, nodes: List[Tuple[str, str, str]]) -> SnapshotResult:
        """Build from flat list of (tag, role, name) tuples."""
        self.ref_map.clear()
        lines: List[str] = []
        for tag, role, name in nodes:
            ref_id = self.ref_map.assign_ref(tag, name, role)
            lines.append(f"{ref_id} [{role or tag}] {name}")
        text = "\n".join(lines)
        return SnapshotResult(
            text=text,
            refs=self.ref_map.all_refs,
            token_count=len(text.split()),
            element_count=self.ref_map._counter,
        )
