"""Tests del cliente MCP: scrub de credenciales para subprocesos y manifiesto.

Critico: los paquetes npx/uvx de terceros NO deben ver API keys del usuario.
"""
import json

from src.core import mcp_client


def test_scrubbed_env_drops_credentials(monkeypatch):
    monkeypatch.setenv("OPENCODE_API_KEY", "sk-secret")
    monkeypatch.setenv("GITHUB_TOKEN", "ghs_secret")
    monkeypatch.setenv("DB_PASSWORD", "hunter2")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "s3cr3t")
    monkeypatch.setenv("HTTP_AUTHORIZATION", "Bearer x")
    monkeypatch.setenv("MY_CREDENTIALS", "x")
    env = mcp_client._scrubbed_env()
    for k in ["OPENCODE_API_KEY", "GITHUB_TOKEN", "DB_PASSWORD",
              "AWS_SECRET_ACCESS_KEY", "HTTP_AUTHORIZATION", "MY_CREDENTIALS"]:
        assert k not in env, f"{k} se filtro al subproceso"


def test_scrubbed_env_keeps_normal_vars(monkeypatch):
    monkeypatch.setenv("NEXUS_TEST_NORMAL", "/usr/bin")
    env = mcp_client._scrubbed_env()
    assert env.get("NEXUS_TEST_NORMAL") == "/usr/bin"
    assert "PATH" in env


def test_scrubbed_env_merges_extra_as_strings():
    env = mcp_client._scrubbed_env({"NPM_CONFIG_CACHE": 123, "FOO": "bar"})
    assert env["NPM_CONFIG_CACHE"] == "123"
    assert env["FOO"] == "bar"


def test_scrubbed_env_is_a_copy(monkeypatch):
    monkeypatch.setenv("NEXUS_TEST_COPY", "1")
    env = mcp_client._scrubbed_env()
    env["NEXUS_TEST_COPY"] = "mutado"
    assert mcp_client._scrubbed_env()["NEXUS_TEST_COPY"] == "1"


def test_load_manifest_missing_file_is_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "_manifest_cache", {})
    assert mcp_client.load_manifest(tmp_path) == []


def test_load_manifest_filters_entries_without_command(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "_manifest_cache", {})
    d = tmp_path / "data" / "mcp_bundle"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text(json.dumps({"entries": [
        {"name": "a", "command": "npx", "args": ["-y", "x"]},
        {"name": "b"},
    ]}), encoding="utf-8")
    entries = mcp_client.load_manifest(tmp_path)
    assert [e["name"] for e in entries] == ["a"]


def test_load_manifest_broken_json_degrades_to_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(mcp_client, "_manifest_cache", {})
    d = tmp_path / "data" / "mcp_bundle"
    d.mkdir(parents=True)
    (d / "manifest.json").write_text("{ esto no es json", encoding="utf-8")
    assert mcp_client.load_manifest(tmp_path) == []
