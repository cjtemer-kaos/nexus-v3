"""Service interfaces for NEXUS v3 architecture.

Every feature implements one of these contracts. The director never instantiates
implementations directly — it asks the registry for them by name.

This is what prevents the 'god object' problem: features can be added/removed
without touching the director.
"""
from src.core.interfaces.service import BaseService

__all__ = ["BaseService"]
