"""
Capability Security - SuperNEXUS v2
Capability-based security inspired by openfang's capability model.

Each gema receives an immutable CapabilitySet that defines exactly what
operations it may perform. The CapabilityManager enforces these at every
decision boundary — no capability, no action.

Capabilities are granular, composable, and time-bounded. Immutable sets
cannot be altered after creation (used for sandboxed/locked-down gemas).
"""

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ── Capability Enum ──────────────────────────────────────────────────────────

class Capability(Enum):
    """Atomic capability tokens — each grants one specific privilege."""
    READ_FILES       = "read_files"
    WRITE_FILES      = "write_files"
    EXECUTE_SHELL    = "execute_shell"
    NETWORK_ACCESS   = "network_access"
    BROWSER_CONTROL  = "browser_control"
    VOICE_CONTROL    = "voice_control"
    MEMORY_WRITE     = "memory_write"
    MEMORY_DELETE     = "memory_delete"
    GEMA_SPAWN       = "gema_spawn"
    GEMA_KILL        = "gema_kill"
    MODEL_SWITCH     = "model_switch"
    CONFIG_CHANGE    = "config_change"
    REDIS_PUBSUB     = "redis_pubsub"
    DANGEROUS_ACTIONS = "dangerous_actions"
    ADMIN            = "admin"


# ── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class CapabilitySet:
    """Immutable or mutable bundle of capabilities for a gema.

    Attributes:
        gema_id:      owning gema identifier
        capabilities: granted capability tokens
        granted_at:   ISO timestamp of creation
        expires_at:   optional expiry (None = permanent)
        immutable:    if True, grant/revoke will refuse to modify
    """
    gema_id: str
    capabilities: Set[Capability] = field(default_factory=set)
    granted_at: str = ""
    expires_at: Optional[str] = None
    immutable: bool = False

    def __post_init__(self):
        if not self.granted_at:
            self.granted_at = datetime.now().isoformat()

    @property
    def is_expired(self) -> bool:
        if self.expires_at is None:
            return False
        try:
            return datetime.now() > datetime.fromisoformat(self.expires_at)
        except (ValueError, TypeError):
            return False

    def has(self, cap: Capability) -> bool:
        return cap in self.capabilities and not self.is_expired

    def __repr__(self) -> str:
        caps = ", ".join(sorted(c.value for c in self.capabilities))
        return f"CapabilitySet(gema_id={self.gema_id!r}, caps=[{caps}], expired={self.is_expired})"


@dataclass
class SecurityPolicy:
    """Defines capability tiers for the system.

    Attributes:
        default_capabilities:   granted to every new gema automatically
        restricted_capabilities: require explicit admin grant
        dangerous_capabilities: require explicit grant + audit trail
    """
    default_capabilities: Set[Capability] = field(default_factory=set)
    restricted_capabilities: Set[Capability] = field(default_factory=set)
    dangerous_capabilities: Set[Capability] = field(default_factory=set)


@dataclass
class AuditEntry:
    """Single audit log record."""
    timestamp: str
    gema_id: str
    action: str  # grant | revoke | check_pass | check_fail | create | expired
    capability: Optional[Capability] = None
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "gema_id": self.gema_id,
            "action": self.action,
            "capability": self.capability.value if self.capability else None,
            "detail": self.detail,
        }


# ── CapabilityManager (Singleton) ───────────────────────────────────────────

class CapabilityManager:
    """Central enforcement point for capability-based security.

    Singleton — one manager controls all gemas globally.

    Usage::

        mgr = CapabilityManager.instance()
        cs = mgr.create_capability_set("code", {Capability.READ_FILES})
        ok  = mgr.check("code", Capability.READ_FILES)  # True
        mgr.grant("code", Capability.WRITE_FILES)
        mgr.revoke("code", Capability.READ_FILES)
        log = mgr.audit_log()
    """

    _instance: Optional["CapabilityManager"] = None

    # ── singleton plumbing ───────────────────────────────────────────────

    def __new__(cls, *args, **kwargs) -> "CapabilityManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    @classmethod
    def instance(cls) -> "CapabilityManager":
        """Get or create the singleton."""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        """Reset singleton — use in tests only."""
        cls._instance = None

    # ── init ─────────────────────────────────────────────────────────────

    def __init__(self) -> None:
        if getattr(self, "_initialized", False):
            return  # already constructed
        self._initialized = True

        self._capability_sets: Dict[str, CapabilitySet] = {}
        self._audit: List[AuditEntry] = []
        self._policy: SecurityPolicy = self._default_policy()

        logger.info("CapabilityManager initialised with default policy")
        logger.info(
            "  default:     %s",
            sorted(c.value for c in self._policy.default_capabilities),
        )
        logger.info(
            "  restricted:  %s",
            sorted(c.value for c in self._policy.restricted_capabilities),
        )
        logger.info(
            "  dangerous:   %s",
            sorted(c.value for c in self._policy.dangerous_capabilities),
        )

    # ── default policy ───────────────────────────────────────────────────

    @staticmethod
    def _default_policy() -> SecurityPolicy:
        """Return the standard SuperNEXUS security policy."""
        return SecurityPolicy(
            default_capabilities={
                Capability.READ_FILES,
                Capability.MEMORY_WRITE,
            },
            restricted_capabilities={
                Capability.EXECUTE_SHELL,
                Capability.NETWORK_ACCESS,
                Capability.DANGEROUS_ACTIONS,
                Capability.CONFIG_CHANGE,
            },
            dangerous_capabilities={
                Capability.DANGEROUS_ACTIONS,
                Capability.ADMIN,
                Capability.GEMA_KILL,
                Capability.MEMORY_DELETE,
            },
        )

    @property
    def policy(self) -> SecurityPolicy:
        return self._policy

    # ── public API ───────────────────────────────────────────────────────

    def create_capability_set(
        self,
        gema_id: str,
        capabilities: Optional[Set[Capability]] = None,
        expires_in_seconds: Optional[int] = None,
        immutable: bool = False,
    ) -> CapabilitySet:
        """Create a new CapabilitySet for *gema_id*.

        Merges caller-requested caps with the policy defaults. Restricted
        capabilities are included only if explicitly requested.

        Args:
            gema_id:             unique gema identifier
            capabilities:        extra capabilities to grant (in addition to defaults)
            expires_in_seconds:  auto-expire after N seconds (None = permanent)
            immutable:           lock the set against future grant/revoke
        """
        if gema_id in self._capability_sets:
            existing = self._capability_sets[gema_id]
            if existing.immutable:
                logger.warning("create_capability_set: %s is immutable, returning existing", gema_id)
                return existing
            # Replace if mutable
            logger.info("create_capability_set: replacing mutable set for %s", gema_id)

        caps = set(self._policy.default_capabilities)
        if capabilities:
            caps |= capabilities

        expires_at = None
        if expires_in_seconds is not None:
            expires_at = (datetime.now() + timedelta(seconds=expires_in_seconds)).isoformat()

        cs = CapabilitySet(
            gema_id=gema_id,
            capabilities=caps,
            expires_at=expires_at,
            immutable=immutable,
        )
        self._capability_sets[gema_id] = cs
        self._log(gema_id, "create", detail=f"caps={sorted(c.value for c in caps)}")

        logger.info("CapabilitySet created for %s: %s", gema_id, cs)
        return cs

    def check(self, gema_id: str, capability: Capability) -> bool:
        """Check whether *gema_id* holds *capability*. Returns False if
        the set is expired, missing, or lacks the capability."""
        cs = self._capability_sets.get(gema_id)
        if cs is None:
            self._log(gema_id, "check_fail", capability=capability, detail="no CapabilitySet")
            logger.debug("check FAIL: %s has no CapabilitySet", gema_id)
            return False

        if cs.is_expired:
            self._log(gema_id, "expired", capability=capability)
            logger.warning("check FAIL: %s CapabilitySet is expired", gema_id)
            return False

        ok = capability in cs.capabilities
        action = "check_pass" if ok else "check_fail"
        self._log(gema_id, action, capability=capability)

        if not ok:
            logger.debug("check FAIL: %s lacks %s", gema_id, capability.value)
        return ok

    def grant(self, gema_id: str, capability: Capability) -> bool:
        """Grant *capability* to *gema_id*. Returns False if immutable or missing set."""
        cs = self._capability_sets.get(gema_id)
        if cs is None:
            logger.warning("grant: no CapabilitySet for %s", gema_id)
            return False
        if cs.immutable:
            logger.warning("grant: %s is immutable, refusing grant of %s", gema_id, capability.value)
            return False
        cs.capabilities.add(capability)
        self._log(gema_id, "grant", capability=capability)
        logger.info("grant: %s ← %s", gema_id, capability.value)
        return True

    def revoke(self, gema_id: str, capability: Capability) -> bool:
        """Revoke *capability* from *gema_id*. Returns False if immutable or missing."""
        cs = self._capability_sets.get(gema_id)
        if cs is None:
            logger.warning("revoke: no CapabilitySet for %s", gema_id)
            return False
        if cs.immutable:
            logger.warning("revoke: %s is immutable, refusing revoke of %s", gema_id, capability.value)
            return False
        if capability not in cs.capabilities:
            logger.debug("revoke: %s already lacks %s", gema_id, capability.value)
            return False
        cs.capabilities.discard(capability)
        self._log(gema_id, "revoke", capability=capability)
        logger.info("revoke: %s ← %s (removed)", gema_id, capability.value)
        return True

    def get_capabilities(self, gema_id: str) -> Optional[Set[Capability]]:
        """Return the current capability set for *gema_id*, or None if absent."""
        cs = self._capability_sets.get(gema_id)
        if cs is None:
            return None
        if cs.is_expired:
            logger.warning("get_capabilities: %s CapabilitySet is expired", gema_id)
            return set()  # empty — expired
        return set(cs.capabilities)

    def list_all(self) -> Dict[str, CapabilitySet]:
        """Return all registered CapabilitySets (including expired ones)."""
        return dict(self._capability_sets)

    def audit_log(self, gema_id: Optional[str] = None) -> List[AuditEntry]:
        """Return audit entries, optionally filtered by gema_id."""
        if gema_id is None:
            return list(self._audit)
        return [e for e in self._audit if e.gema_id == gema_id]

    def remove_gema(self, gema_id: str) -> bool:
        """Fully remove a gema's CapabilitySet and purge its audit trail."""
        removed = gema_id in self._capability_sets
        self._capability_sets.pop(gema_id, None)
        self._audit = [e for e in self._audit if e.gema_id != gema_id]
        if removed:
            self._log(gema_id, "remove", detail="CapabilitySet deleted")
            logger.info("remove_gema: %s", gema_id)
        return removed

    def summary(self) -> Dict:
        """Return a compact summary of all capability sets."""
        return {
            "total_gemas": len(self._capability_sets),
            "active": sum(1 for cs in self._capability_sets.values() if not cs.is_expired),
            "expired": sum(1 for cs in self._capability_sets.values() if cs.is_expired),
            "immutable": sum(1 for cs in self._capability_sets.values() if cs.immutable),
            "audit_entries": len(self._audit),
            "policy": {
                "default": sorted(c.value for c in self._policy.default_capabilities),
                "restricted": sorted(c.value for c in self._policy.restricted_capabilities),
                "dangerous": sorted(c.value for c in self._policy.dangerous_capabilities),
            },
        }

    # ── internals ────────────────────────────────────────────────────────

    def _log(self, gema_id: str, action: str, capability: Optional[Capability] = None, detail: str = "") -> None:
        entry = AuditEntry(
            timestamp=datetime.now().isoformat(),
            gema_id=gema_id,
            action=action,
            capability=capability,
            detail=detail,
        )
        self._audit.append(entry)


# ── Module-level shortcut ────────────────────────────────────────────────────

def get_capability_manager() -> CapabilityManager:
    """Convenience accessor — returns the singleton."""
    return CapabilityManager.instance()
