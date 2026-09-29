"""
Swarm Foundation — typed states, permission scopes, plugin boundaries.
Adapted from Hermes swarm-foundation.ts.

Define el core typesystem para NexusHive: estados de workers,
checkpoints, artifacts, permisos y boundaries.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Worker states
# ---------------------------------------------------------------------------

class WorkerState(str, Enum):
    IDLE = "idle"
    EXECUTING = "executing"
    THINKING = "thinking"
    WAITING = "waiting"
    BLOCKED = "blocked"
    SYNCING = "syncing"
    REVIEWING = "reviewing"
    OFFLINE = "offline"


class CheckpointStatus(str, Enum):
    NONE = "none"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    BLOCKED = "blocked"
    HANDOFF = "handoff"
    NEEDS_INPUT = "needs_input"


class TaskSource(str, Enum):
    RUNTIME = "runtime"
    API = "api"
    PLUGIN = "plugin"
    INFERRED = "inferred"
    HIVE = "hive"


class ArtifactKind(str, Enum):
    FILE = "file"
    DIFF = "diff"
    PATCH = "patch"
    BUILD = "build"
    LOG = "log"
    REPORT = "report"
    PREVIEW = "preview"


# ---------------------------------------------------------------------------
# Scopes & Boundaries
# ---------------------------------------------------------------------------

class PluginScope(str, Enum):
    WORKER_REGISTRY_READ = "worker-registry:read"
    WORKER_RUNTIME_READ = "worker-runtime:read"
    WORKER_RUNTIME_WRITE = "worker-runtime:write"
    WORKER_DISPATCH_SEND = "worker-dispatch:send"
    WORKER_SESSION_ATTACH = "worker-session:attach"
    WORKER_ARTIFACTS_WRITE = "worker-artifacts:write"
    WORKER_PREVIEW_PUBLISH = "worker-preview:publish"
    WORKSPACE_FILES_READ = "workspace-files:read"
    WORKSPACE_FILES_WRITE = "workspace-files:write"
    WORKSPACE_UI_REGISTER = "workspace-ui:register"
    WORKSPACE_ROUTING_READ = "workspace-routing:read"
    NEXUS_MEMORY_READ = "nexus-memory:read"
    NEXUS_MEMORY_WRITE = "nexus-memory:write"
    NEXUS_HIVE_SEND = "nexus-hive:send"
    NEXUS_HIVE_READ = "nexus-hive:read"


SCOPE_HIERARCHY: Dict[PluginScope, List[PluginScope]] = {
    PluginScope.WORKER_RUNTIME_WRITE: [PluginScope.WORKER_RUNTIME_READ],
    PluginScope.WORKSPACE_FILES_WRITE: [PluginScope.WORKSPACE_FILES_READ],
    PluginScope.NEXUS_MEMORY_WRITE: [PluginScope.NEXUS_MEMORY_READ],
    PluginScope.WORKER_DISPATCH_SEND: [PluginScope.WORKER_REGISTRY_READ],
}


class PluginBoundary(str, Enum):
    WORKSPACE_ONLY = "workspace-only"
    RUNTIME_READONLY = "runtime-readonly"
    RUNTIME_CONTROL = "runtime-control"
    HYBRID = "hybrid"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class SwarmWorker:
    worker_id: str
    role: str
    state: WorkerState = WorkerState.IDLE
    phase: str = "init"
    current_task: Optional[str] = None
    active_tool: Optional[str] = None
    cwd: Optional[str] = None
    last_output_at: Optional[float] = None
    started_at: Optional[float] = None
    last_check_in: Optional[str] = None
    last_summary: Optional[str] = None
    needs_human: bool = False
    blocked_reason: Optional[str] = None
    checkpoint_status: CheckpointStatus = CheckpointStatus.NONE
    next_action: Optional[str] = None
    assigned_task_count: int = 0
    scopes: List[PluginScope] = field(default_factory=list)
    boundary: PluginBoundary = PluginBoundary.RUNTIME_CONTROL


@dataclass
class SwarmTask:
    id: str
    title: str
    status: str = "pending"
    source: TaskSource = TaskSource.RUNTIME
    priority: int = 0
    assignee: Optional[str] = None
    updated_at: Optional[float] = None


@dataclass
class SwarmArtifact:
    id: str
    kind: ArtifactKind
    label: str
    worker_id: str
    path: Optional[str] = None
    updated_at: Optional[float] = None
    source: str = "runtime"
    size_bytes: Optional[int] = None


@dataclass
class PluginManifest:
    name: str
    version: str = "1.0.0"
    scopes: List[PluginScope] = field(default_factory=list)
    boundary: PluginBoundary = PluginBoundary.WORKSPACE_ONLY


# ---------------------------------------------------------------------------
# Permission checker
# ---------------------------------------------------------------------------

def check_scope(manifest: PluginManifest, required_scope: PluginScope) -> bool:
    if required_scope in manifest.scopes:
        return True
    implied = SCOPE_HIERARCHY.get(required_scope, [])
    return any(s in manifest.scopes for s in implied)


def check_boundary(manifest: PluginManifest, boundary: PluginBoundary) -> bool:
    order = [PluginBoundary.WORKSPACE_ONLY, PluginBoundary.RUNTIME_READONLY,
             PluginBoundary.RUNTIME_CONTROL, PluginBoundary.HYBRID]
    return order.index(manifest.boundary) >= order.index(boundary)
