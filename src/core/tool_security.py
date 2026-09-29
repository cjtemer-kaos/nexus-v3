"""
Tool Security - SuperNEXUS v2
Dual-list policy: denylist for non-admin, allowlist for plan mode.
Absorbed from odysseus/src/tool_security.py — names cleaned.
"""

import logging
from typing import Optional, Set

logger = logging.getLogger(__name__)


# Tools non-admin users must not execute directly
NON_ADMIN_BLOCKED_TOOLS = {
    "bash", "python", "terminal",
    "read_file", "write_file", "edit_file",
    "grep", "glob", "ls",
    "manage_memory", "manage_skills",
    "manage_tasks", "manage_webhooks",
    "manage_tokens", "manage_settings",
    "manage_calendar", "manage_email",
    "send_email", "reply_to_email",
    "vault_search", "vault_get",
    "download_model", "serve_model",
}

# Plan mode: only read-only tools allowed
PLAN_MODE_READONLY_TOOLS = {
    "read_file", "grep", "glob", "ls",
    "web_search", "web_fetch",
    "search_chats", "list_models",
    "list_sessions", "list_emails",
    "read_email", "list_notes",
    "list_calendar_events",
    "search_library", "list_presets",
}

# Known mutators — always blocked in plan mode
_PLAN_MODE_KNOWN_MUTATORS = {
    "write_file", "edit_file", "bash", "python", "terminal",
    "manage_memory", "manage_skills", "manage_tasks",
    "manage_webhooks", "manage_tokens", "manage_settings",
    "manage_calendar", "manage_email",
    "send_email", "reply_to_email",
    "download_model", "serve_model",
    "generate_image", "edit_image",
}


def plan_mode_disabled_tools() -> Set[str]:
    """Tool names to denylist in plan mode (inverse of allowlist)."""
    return _PLAN_MODE_KNOWN_MUTATORS - PLAN_MODE_READONLY_TOOLS


def is_public_blocked_tool(tool_name: Optional[str]) -> bool:
    """True if non-admin user must not execute this tool. Fails CLOSED."""
    if tool_name is None or tool_name == "":
        return False
    if not isinstance(tool_name, str):
        return True
    return tool_name in NON_ADMIN_BLOCKED_TOOLS or tool_name.startswith("mcp__")


def blocked_tools_for_role(role: str) -> Set[str]:
    """Tools to hide/disable for a given role."""
    if role in ("admin", "owner"):
        return set()
    return set(NON_ADMIN_BLOCKED_TOOLS)


def is_tool_allowed_in_plan(tool_name: str) -> bool:
    """True if tool is allowed in plan/read-only mode."""
    return tool_name in PLAN_MODE_READONLY_TOOLS
