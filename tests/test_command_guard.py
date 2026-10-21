"""Guard de comandos de plan mode (port de Cline command-guard)."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.core.command_guard import (
    find_file_editing_command,
    format_plan_mode_blocked_error,
    is_allowed_redirect_target,
)
from src.core.scope import Scope, ScopeConfig, ScopeDenied


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf build",
        "rmdir old",
        "mv a.txt b.txt",
        "cp a.txt b.txt",
        "touch nuevo.txt",
        "mkdir nueva",
        "chmod +x script.sh",
        "truncate -s 0 log.txt",
        "del /q a.txt",
        "Remove-Item -Recurse dist",
        "New-Item -ItemType File x.txt",
        "Set-Content -Path a.txt -Value hola",
    ],
)
def test_blocks_direct_file_mutations(command: str):
    assert find_file_editing_command(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m wip",
        "git add .",
        "git checkout -- .",
        "git stash",
        "git stash pop",
        "git reset --hard",
        "git clean -fd",
        "npm install",
        "npm i lodash",
        "pnpm add react",
        "yarn",
        "yarn install",
        "pip install requests",
        "pip uninstall requests",
        "python -m pip install requests",
        "cargo add serde",
        "go get github.com/x/y",
        "composer require foo/bar",
        "dotnet add package Foo",
        "winget install Git.Git",
        "apt-get install curl",
        "brew install jq",
    ],
)
def test_blocks_mutating_subcommands(command: str):
    assert find_file_editing_command(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "sed -i 's/a/b/' file.txt",
        "sed -i.bak 's/a/b/' file.txt",
        "sed --in-place 's/a/b/' file.txt",
        "perl -pi -e 's/a/b/' file.txt",
        "awk -i inplace '{print}' file.txt",
        "sort -o sorted.txt input.txt",
        "curl -o out.txt https://example.com",
        "curl -O https://example.com/file.zip",
        "curl --output out.txt https://example.com",
        "wget https://example.com/file.zip",
        "find . -delete",
        "find . -exec rm {} \\;",
        "find . -execdir touch x {} \\;",
    ],
)
def test_blocks_tools_that_write_through_flags(command: str):
    assert find_file_editing_command(command) is not None


@pytest.mark.parametrize(
    "command",
    [
        "echo hola > out.txt",
        "echo hola >> out.txt",
        "cat a.txt > b.txt",
        "ls 2> err.txt",
        "tee out.txt",
        "echo hola | tee -a out.txt",
    ],
)
def test_blocks_output_redirection_to_files(command: str):
    assert find_file_editing_command(command) is not None


def test_allows_redirection_only_to_null_and_temp():
    for command in [
        "echo hola > /dev/null",
        "ls > /tmp/listado.txt",
        "ls > %TEMP%\\listado.txt",
        "ls > $env:TEMP\\listado.txt",
        "make 2>&1",
    ]:
        assert find_file_editing_command(command) is None, command
    assert is_allowed_redirect_target("/dev/null")
    assert not is_allowed_redirect_target("./out.txt")


def test_blocks_compound_commands_if_any_segment_mutates():
    assert find_file_editing_command("git status && rm -rf build") is not None
    assert find_file_editing_command("ls; touch x") is not None
    assert find_file_editing_command("echo ok | tee log.txt") is not None
    assert find_file_editing_command("(cd src && rm a.py)") is not None


@pytest.mark.parametrize(
    "command",
    [
        "ls -la",
        "pwd",
        "git status",
        "git diff",
        "git log --oneline",
        "git show HEAD",
        "git blame src/main.py",
        "git stash list",
        "git stash show",
        "git worktree list",
        "git submodule status",
        "grep -rn TODO src",
        "rg --files",
        "cat README.md",
        "head -n 20 file.txt",
        "tail -f log.txt",
        "wc -l src/main.py",
        "find . -name '*.py'",
        "python --version",
        "python script.py",
        "python -m pytest -q",
        "node --version",
        "pytest -q",
        "docker images --format json",
        "curl https://example.com",
        "curl -s https://example.com",
        "wget --spider https://example.com",
        "wget -O- https://example.com",
    ],
)
def test_allows_read_only_commands(command: str):
    assert find_file_editing_command(command) is None


def test_ignores_quoted_heredoc_and_commented_text():
    assert find_file_editing_command('echo "rm -rf /"') is None
    assert find_file_editing_command("grep 'touch x' file.txt") is None
    heredoc = "cat <<'EOF'\nrm -rf build\nEOF"
    assert find_file_editing_command(heredoc) is None
    assert find_file_editing_command("ls # rm -rf build") is None
    assert find_file_editing_command('python -c "print(1)"') is None


def test_unwraps_sudo_env_and_xargs():
    assert find_file_editing_command("sudo rm -rf /") is not None
    assert find_file_editing_command("env FOO=1 rm a.txt") is not None
    assert find_file_editing_command("timeout 5 rm a.txt") is not None
    assert find_file_editing_command("find . -type f | xargs touch") is not None
    assert find_file_editing_command("sudo git status") is None
    assert find_file_editing_command("env FOO=1 python -m pytest") is None


def test_accepts_structured_command_with_argv():
    assert find_file_editing_command({"command": "git", "args": ["status"]}) is None
    assert find_file_editing_command({"command": "git", "args": ["commit", "-m", "x"]}) is not None
    assert find_file_editing_command({"command": "rm", "args": ["-rf", "x"]}) is not None


def test_windows_paths_and_exe_suffix_are_normalized():
    assert find_file_editing_command(r"C:\Windows\System32\cmd.exe /c del a.txt") is not None
    assert find_file_editing_command(r"/usr/bin/rm a.txt") is not None
    assert find_file_editing_command(r"/usr/bin/git status") is None


def test_error_message_explains_plan_mode_and_escape_hatch():
    message = format_plan_mode_blocked_error("`rm`")
    assert "plan mode" in message.lower()
    assert "`rm`" in message
    assert "%TEMP%" in message


def _read_only_scope() -> Scope:
    cfg = ScopeConfig(allowed_roots=("./workspace",), permission="read_only")
    return Scope(cfg, project_root=Path("."))


def test_read_only_scope_allows_only_non_mutating_shell_commands():
    scope = _read_only_scope()
    scope.check_tool("exec", {"command": "git status"})
    scope.check_tool("exec", {"command": "grep -rn TODO src"})
    with pytest.raises(ScopeDenied, match="plan mode"):
        scope.check_tool("exec", {"command": "rm -rf build"})
    with pytest.raises(ScopeDenied, match="plan mode"):
        scope.check_tool("exec", {"command": "git commit -m x"})


def test_read_only_scope_keeps_blocking_non_shell_mutating_tools():
    scope = _read_only_scope()
    with pytest.raises(ScopeDenied, match="solo lectura"):
        scope.check_tool("write_file", {"path": "./workspace/a.txt", "content": "x"})
    with pytest.raises(ScopeDenied, match="solo lectura"):
        scope.check_tool("apply_patch", {"patch": "..."})

