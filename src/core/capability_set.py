"""CapabilitySet — permisos inmutables por gema al spawn (openfang parity).

Cada gema declara capabilities requeridas; CapabilityManager autoriza por agente.
Previene privilege escalation. Inmutable tras creacion (frozen).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import FrozenSet

# Capabilities atomicas
CAP_NET = "net"              # acceso red (web_fetch, http)
CAP_FS_WRITE = "fs.write"    # escritura filesystem
CAP_FS_READ = "fs.read"      # lectura (siempre permitido si workspace)
CAP_SHELL = "shell"          # exec / persistent_shell
CAP_BRAIN_WRITE = "brain.write"  # escribir grafo/memoria
CAP_BRAIN_READ = "brain.read"
CAP_GEMA_SPAWN = "gema.spawn"    # spawnear otras gemas
CAP_APPROVAL_BYPASS = "approval.bypass"  # bypasear approval gate (nunca por defecto)

ALL_CAPS: frozenset[str] = frozenset({
    CAP_NET, CAP_FS_WRITE, CAP_FS_READ, CAP_SHELL,
    CAP_BRAIN_WRITE, CAP_BRAIN_READ, CAP_GEMA_SPAWN, CAP_APPROVAL_BYPASS,
})

# Perfil por defecto: lectura siempre, escritura/net segun manifest
DEFAULT_CAPS: frozenset[str] = frozenset({CAP_FS_READ, CAP_BRAIN_READ})


@dataclass(frozen=True, slots=True)
class CapabilitySet:
    """Conjunto inmutable de capabilities otorgadas a una gema."""
    caps: frozenset[str] = field(default_factory=lambda: DEFAULT_CAPS)

    def allows(self, cap: str) -> bool:
        return cap in self.caps

    def allows_any(self, caps: set[str] | frozenset[str]) -> bool:
        return bool(self.caps & set(caps))

    def allows_all(self, caps: set[str] | frozenset[str]) -> bool:
        return set(caps) <= self.caps

    @classmethod
    def from_manifest(cls, manifest: Any) -> "CapabilitySet":
        # Accept GemaManifest or list[str]
        if manifest is None:
            return cls(caps=DEFAULT_CAPS)
        if isinstance(manifest, (list, tuple, set, frozenset)):
            manifest_caps = manifest
        elif hasattr(manifest, "capabilities"):
            manifest_caps = getattr(manifest, "capabilities") or []
        else:
            manifest_caps = manifest
        if not manifest_caps:
            return cls(caps=DEFAULT_CAPS)
        # Validar contra ALL_CAPS, ignorar desconocidas (soporta fs_read / fs.read)
        normalized = [c.replace("_", ".") for c in manifest_caps]  # type: ignore[union-attr]
        valid = {c for c in normalized if c in ALL_CAPS}
        # Siempre incluir defaults
        return cls(caps=frozenset(valid | set(DEFAULT_CAPS)))

    @classmethod
    def full(cls) -> "CapabilitySet":
        """Full caps — solo para Director/core, nunca para gemas."""
        return cls(caps=ALL_CAPS)
