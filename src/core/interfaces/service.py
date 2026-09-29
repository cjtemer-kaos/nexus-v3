"""BaseService — contract every NEXUS service must implement.

Design (12-factor + opencode App pattern):
    - Services are independent units with a clear contract
    - They expose lifecycle hooks (init / start / stop)
    - They report status via a uniform interface
    - One service's failure must NOT cascade to others
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from src.core.nexus import NexusApp


class BaseService(ABC):
    """Base contract for any service in the NEXUS app.

    Lifecycle:
        1. __init__(self): zero-arg cheap construction (no I/O)
        2. await init(app):  one-time setup, can be slow (DB, files, network)
        3. await start():    optional — start background tasks
        4. await stop():     optional — clean up on shutdown
        5. get_status():     non-blocking status snapshot for /api/status

    Naming:
        Each service MUST set a unique `name` class attribute, used as the
        registry key.

    Service-to-service communication:
        Services access each other via the `app` reference passed to init():
            other = app.get("other_service")
            if other is not None:
                ...
        NEVER hold a hard reference to another service across calls — it may
        not be initialized yet, or may have been replaced.
    """

    name: str = ""  # override in subclass

    @abstractmethod
    async def init(self, app: "NexusApp") -> None:
        """One-time async initialization.

        Args:
            app: the NexusApp container. Use `app.get(name)` to access
                 other services. Other services may not yet be initialized,
                 so check for None.

        Raises:
            Any exception. The registry catches it and marks the service
            as failed without bringing down the rest of the system.
        """
        ...

    async def start(self) -> None:
        """Optional: start background tasks AFTER init.

        Called once by registry.start_all() after all services have inited.
        Use this for tasks that need other services to be ready.
        """
        return None

    async def stop(self) -> None:
        """Optional: clean shutdown.

        Called once by registry.stop_all().
        """
        return None

    def get_status(self) -> Dict[str, Any]:
        """Status snapshot. MUST be fast (no I/O, no awaits).

        Returns:
            Dict with at least `name`. Implementations add their own keys.
        """
        return {"name": self.name, "alive": True}

    def __repr__(self) -> str:
        return f"<{type(self).__name__} name={self.name!r}>"
