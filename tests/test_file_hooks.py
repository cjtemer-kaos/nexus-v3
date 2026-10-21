"""Hooks por archivo: descubrimiento, JSON por stdin, exit codes y timeout."""
from __future__ import annotations

import asyncio
import sys

from src.core.file_hooks import install_file_hooks
from src.core.hooks_engine import HookAction, HooksEngine

PY = sys.executable


def _write_hook(root, phase: str, name: str, body: str):
    d = root / ".nexus" / "hooks" / phase
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(body, encoding="utf-8")
    return p


def _emit(root, payload, config=None):
    hooks = HooksEngine({})
    count = install_file_hooks(hooks, root, config or {"files": {"enabled": True}})
    assert count >= 1
    return asyncio.run(hooks.emit("pre_tool_use", payload))


def test_file_hook_stop_with_json(tmp_path):
    _write_hook(tmp_path, "pre_tool_use", "block.py", (
        "import json, sys\n"
        "p = json.load(sys.stdin)\n"
        "print(json.dumps({'action': 'stop', 'message': 'blocked:' + p.get('tool', '')}))\n"
    ))
    res = _emit(tmp_path, {"tool": "exec", "args": {}})
    assert res.action is HookAction.STOP
    assert "blocked:exec" in res.message


def test_file_hook_inject(tmp_path):
    _write_hook(tmp_path, "pre_tool_use", "inject.py", (
        "import json\n"
        "print(json.dumps({'action': 'inject', 'inject_content': 'memoria relevante'}))\n"
    ))
    res = _emit(tmp_path, {"tool": "read_file"})
    assert res.action is HookAction.INJECT
    assert res.inject_content == "memoria relevante"


def test_file_hook_exit_2_blocks(tmp_path):
    _write_hook(tmp_path, "pre_tool_use", "deny.py", (
        "import sys\n"
        "print('prohibido por politica', file=sys.stderr)\n"
        "sys.exit(2)\n"
    ))
    res = _emit(tmp_path, {"tool": "write_file"})
    assert res.action is HookAction.STOP
    assert "prohibido" in res.message


def test_file_hook_nonzero_continues_by_default(tmp_path):
    _write_hook(tmp_path, "pre_tool_use", "warn.py", "raise SystemExit(3)\n")
    res = _emit(tmp_path, {"tool": "read_file"})
    assert res.action is HookAction.CONTINUE


def test_file_hook_timeout_continues_or_blocks(tmp_path):
    _write_hook(tmp_path, "pre_tool_use", "slow.py", "import time; time.sleep(5)\n")
    cfg = {"files": {"enabled": True, "timeout_s": 0.5, "fail_closed": False}}
    res = _emit(tmp_path, {"tool": "read_file"}, cfg)
    assert res.action is HookAction.CONTINUE

    cfg["files"]["fail_closed"] = True
    res = _emit(tmp_path, {"tool": "read_file"}, cfg)
    assert res.action is HookAction.STOP


def test_file_hooks_disabled_and_outside_workspace(tmp_path):
    _write_hook(tmp_path, "pre_tool_use", "block.py", "print('{}')\n")
    hooks = HooksEngine({})
    assert install_file_hooks(hooks, tmp_path, {"files": {"enabled": False}}) == 0
    assert install_file_hooks(
        hooks, tmp_path, {"files": {"enabled": True, "dir": str(tmp_path.parent)}}) == 0
