"""Fase 11: jail real de exec + guards de body JSON (400 en vez de 500)."""
from pathlib import Path

from fastapi.testclient import TestClient

from src.api.server import _exec_jail, _json_body, app
from src.core.scope import Scope, ScopeConfig


def _scope(permission: str) -> Scope:
    cfg = ScopeConfig(allowed_roots=("./workspace", "./data"), permission=permission)
    return Scope(cfg, project_root=Path("."))


def test_exec_jail_allows_safe_single_commands():
    scope = _scope("workspace_write")
    cwd = scope.check_path("./workspace")
    for cmd in ["ls -la", "git status", "python --version", "echo hola mundo",
                "pip install requests", "npm run build", "cat notas.txt",
                "grep -r foo ./src", "docker images --format json"]:
        assert _exec_jail(cmd, cwd, scope) is None, f"no debia bloquear: {cmd}"


def test_exec_jail_blocks_chaining_and_redirection():
    scope = _scope("workspace_write")
    cwd = scope.check_path("./workspace")
    for cmd in ["ls; rm -rf x", "a && b", "a || b", "cat x | tee y", "echo x > y",
                "echo x >> y", "cat < y", "x `whoami`", "echo $(whoami)", "a & b"]:
        assert _exec_jail(cmd, cwd, scope) is not None, f"debia bloquear: {cmd}"


def test_exec_jail_blocks_paths_outside_roots():
    scope = _scope("workspace_write")
    cwd = scope.check_path("./workspace")
    for cmd in ["cat /etc/passwd", "cat C:\\Users\\x\\.ssh\\id_rsa",
                "type ../../secret.txt", "cat ..\\..\\boot.ini", "ls /"]:
        assert _exec_jail(cmd, cwd, scope) is not None, f"debia bloquear: {cmd}"


def test_exec_jail_allows_paths_inside_roots():
    scope = _scope("workspace_write")
    cwd = scope.check_path("./workspace")
    for cmd in ["cat ./notas.txt", "ls ./sub", "cat notas.txt"]:
        assert _exec_jail(cmd, cwd, scope) is None, f"no debia bloquear: {cmd}"


def test_exec_jail_full_permission_unrestricted():
    scope = _scope("full")
    cwd = scope.check_path("./workspace")
    for cmd in ["ls; rm -rf x", "cat /etc/passwd", "cat C:\\Users\\x\\a.txt"]:
        assert _exec_jail(cmd, cwd, scope) is None, f"full debia permitir: {cmd}"


def test_exec_endpoint_jail_blocks_chaining():
    client = TestClient(app)
    r = client.post("/api/exec", json={"command": "ls; rm -rf /", "cwd": "./workspace"})
    assert r.status_code == 403
    assert "jail" in r.text or "bloqueado" in r.text


def test_json_body_guard_returns_400_not_500():
    client = TestClient(app)
    r = client.post("/api/slash", data="{ esto no es json",
                    headers={"content-type": "application/json"})
    assert r.status_code == 400
    assert "json" in r.text.lower()


def test_json_body_helper_required_and_optional():
    import asyncio

    class _Req:
        def __init__(self, fail: bool):
            self._fail = fail

        async def json(self):
            if self._fail:
                raise ValueError("bad json")
            return {"ok": 1}

    from fastapi import HTTPException

    assert asyncio.run(_json_body(_Req(False))) == {"ok": 1}
    assert asyncio.run(_json_body(_Req(True), required=False)) == {}
    try:
        asyncio.run(_json_body(_Req(True)))
        raise AssertionError("debia lanzar 400")
    except HTTPException as e:
        assert e.status_code == 400
