"""
Loops Engine — Trigger ? Act ? Check ? Repeat until exit condition.

Integrates the 41 curated loop patterns from loops.elorm.xyz into the
Nexus autonomous agent ecosystem. Each loop is a self-contained contract:

    Loop(name, goal, max_iters, check_cmd, exit_condition, step_1)

The engine runs a loop by executing the check command each iteration and
comparing output against the exit condition. Supports manual, interval,
and event trigger types.
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Loop:
    name: str
    goal: str
    max_iters: int | None
    check_cmd: str
    exit_condition: str
    step_1: str
    category: str = ""
    url: str = ""
    tags: list[str] = field(default_factory=list)
    agents: list[str] = field(default_factory=list)
    trigger_type: str = "manual"  # manual | interval | event


@dataclass
class LoopResult:
    name: str
    success: bool
    iterations: int
    total_seconds: float
    last_output: str
    error: str | None = None
    history: list[dict] = field(default_factory=list)


# ---------------------------------------------------------------------------
# The 41 curated loops
# ---------------------------------------------------------------------------

LOOP_REGISTRY: dict[str, Loop] = {}


def _register(loop: Loop) -> None:
    LOOP_REGISTRY[loop.name] = loop


# ── CI ──
_register(Loop("ship-pr-until-green", "PR is open with all CI checks passing", 10,
               "gh pr checks", "all PR checks are success",
               "Implement the change, test locally, push, open PR, and fix CI until green.",
               category="CI", url="/loops/ship-pr-until-green", tags=["pr", "ci", "ship"],
               agents=["Claude Code", "Cursor", "Codex"]))
_register(Loop("build-until-green", "production build succeeds", 10,
               "npm run build", "npm run build exits 0",
               "Run the build. If it fails, fix the first error, then repeat until green.",
               category="CI", url="/loops/build-until-green", tags=["build", "compile", "ci"],
               agents=["Cursor", "Claude Code"]))
_register(Loop("fix-ci-until-green", "latest CI run on this branch passes", 8,
               "gh run list --branch $(git branch --show-current) --limit 1 --json conclusion -q '.[0].conclusion'",
               "latest run conclusion is success",
               "Find the latest failed CI run, read logs, reproduce locally, fix root cause, push, and verify.",
               category="CI", url="/loops/fix-ci-until-green", tags=["ci", "fix"],
               agents=["Claude Code", "Codex"]))
_register(Loop("ci-failure-watcher", "latest CI run on this branch is green", 12,
               "gh run list --branch $(git branch --show-current) --limit 1",
               "latest run conclusion is success",
               "Check CI status. If failed, read logs, fix root cause, verify locally, and push if needed.",
               category="CI", url="/loops/ci-failure-watcher", tags=["ci", "watch"],
               agents=["Codex", "Cursor"], trigger_type="interval"))

# ── Testing ──
_register(Loop("e2e-until-green", "E2E suite passes", 10,
               "npm run test:e2e", "E2E command exits 0",
               "Run E2E tests. Fix the first failing spec, then repeat.",
               category="Testing", url="/loops/e2e-until-green", tags=["e2e", "playwright", "testing"],
               agents=["Cursor", "Claude Code"]))
_register(Loop("coverage-until-threshold", "coverage meets target threshold (80%) with all tests passing", 12,
               "npm test -- --coverage", "coverage threshold is met and tests exit 0",
               "Run coverage. Add focused tests for the biggest uncovered gaps, then repeat.",
               category="Testing", url="/loops/coverage-until-threshold", tags=["coverage", "testing", "quality"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("test-until-green", "all tests pass", 10,
               "npm test", "tests exit 0",
               "Run tests. If there are failures, fix the smallest root cause, then repeat.",
               category="Testing", url="/loops/test-until-green", tags=["testing"],
               agents=["Claude Code", "Cursor", "Codex"]))
_register(Loop("autoloop-tdd", "implement target behavior test-first with green suite", 12,
               "npm test", "target behavior is covered and all tests pass",
               "Write a failing test for the next behavior, implement minimum code to pass, refactor, repeat.",
               category="Testing", url="/loops/autoloop-tdd", tags=["tdd", "testing"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("flaky-test-triage", "every failure classified; real regressions fixed", 5,
               "npm test -- --testPathPattern=<failing-suite>",
               "every failure is classified and real regressions fixed",
               "Run the failing suite multiple times. Classify each failure, fix real ones, document flaky.",
               category="Testing", url="/loops/flaky-test-triage", tags=["flaky", "testing", "triage"],
               agents=["Claude Code", "Cursor"]))

# ── Review ──
_register(Loop("de-sloppify-pass", "recent changes are clean, minimal, convention-aligned", 4,
               "npm run lint && npm test",
               "review finds no slop and checks pass",
               "Review the diff for debug code, dead branches, and naming issues. Fix with minimal diffs.",
               category="Review", url="/loops/de-sloppify-pass", tags=["review", "quality", "cleanup"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("pr-self-review", "three clean self-review passes on current diff", 3,
               "git diff main...HEAD",
               "three passes complete with no critical findings",
               "Review the diff like a senior reviewer. Fix findings, then re-review.",
               category="Review", url="/loops/pr-self-review", tags=["review", "pr", "quality"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("post-edit-test-guard", "after each edit batch, related tests pass before continuing", None,
               "npm test -- --findRelatedTests <edited files>",
               "related tests exit 0",
               "After edits, run related tests. If they fail, fix before making more changes.",
               category="Review", url="/loops/post-edit-test-guard", tags=["review", "testing", "guard"],
               agents=["Claude Code", "Cursor"], trigger_type="event"))

# ── Quality ──
_register(Loop("format-until-clean", "formatter runs cleanly with no remaining diff", 5,
               "npm run format && git diff",
               "format command succeeds and git diff is empty",
               "Run the formatter. Fix issues it cannot auto-fix, then repeat.",
               category="Quality", url="/loops/format-until-clean", tags=["format", "quality"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("lint-typecheck-fix", "lint and typecheck are clean", 8,
               "npm run lint && npx tsc --noEmit",
               "both commands exit 0",
               "Run lint and typecheck. Fix reported issues with minimal diffs.",
               category="Quality", url="/loops/lint-typecheck-fix", tags=["lint", "typecheck", "quality"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("a11y-audit-until-clean", "zero serious accessibility violations on changed UI", 8,
               "npm run test:a11y", "a11y audit exits 0",
               "Run a11y audit on changed routes. Fix each violation, prioritize keyboard and screen reader.",
               category="Quality", url="/loops/a11y-audit-until-clean", tags=["a11y", "quality"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("bundle-size-budget", "client bundle stays under size-limit budget", 6,
               "npm run build && npm run size-limit",
               "size-limit exits 0",
               "Build and measure bundle size. Lazy-load or trim deps until size-limit passes.",
               category="Quality", url="/loops/bundle-size-budget", tags=["bundle", "performance", "quality"],
               agents=["Claude Code", "Cursor"]))

# ── Planning ──
_register(Loop("spec-first-ship", "every requirement in spec.md is implemented and checked off", 15,
               "npm test", "spec.md has no unchecked requirements",
               "Read spec.md, implement the first unchecked item, verify it, mark [x], stop this iteration.",
               category="Planning", url="/loops/spec-first-ship", tags=["planning", "spec", "requirements"],
               agents=["Claude Code", "Cursor", "Codex"]))

# ── Debugging ──
_register(Loop("reflexion-debug-loop", "the failing test or repro passes", 8,
               "npm test -- --testNamePattern=<failing-test>",
               "the repro test exits 0",
               "Reproduce the bug. If it fails, append a reflection to .loops/reflexion.md before trying a fix.",
               category="Debugging", url="/loops/reflexion-debug-loop", tags=["debug", "reflexion"],
               agents=["Claude Code", "Codex"]))
_register(Loop("investigation-script-loop", "prove root cause with minimal repro script", 8,
               "node scripts/investigate.mjs",
               "script output demonstrates root cause",
               "Write a tiny throwaway script that reproduces the issue. Iterate on what the output shows.",
               category="Debugging", url="/loops/investigation-script-loop", tags=["debug", "investigation"],
               agents=["Claude Code", "Cursor"]))

# ── Maintenance ──
_register(Loop("migration-until-applied", "all database migrations apply cleanly", 6,
               "npx prisma migrate status",
               "migrate status shows no pending failures",
               "Run migrations. Fix schema or SQL errors, then repeat until status is clean.",
               category="Database", url="/loops/migration-until-applied", tags=["database", "migration", "prisma"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("dependency-upgrade-one-by-one", "critical outdated deps upgraded with green tests", 15,
               "npm outdated && npm test && npm run build",
               "npm outdated shows no critical packages left",
               "Pick one outdated package, upgrade it, fix breakages, commit. One per iteration.",
               category="Maintenance", url="/loops/dependency-upgrade-one-by-one", tags=["deps", "maintenance"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("changelog-sync-after-ship", "CHANGELOG.md has accurate entries for this ship", 3,
               "git log -5 --oneline",
               "changelog covers all user-visible changes",
               "Review recent commits, write Keep-a-Changelog entries, verify completeness.",
               category="Maintenance", url="/loops/changelog-sync-after-ship", tags=["changelog", "ship"],
               agents=["Claude Code"]))
_register(Loop("knip-until-clean", "no unused code or dependencies", 5,
               "npx knip", "knip exits 0",
               "Run knip. Remove dead exports and unused deps; verify tests still pass.",
               category="Maintenance", url="/loops/knip-until-clean", tags=["knip", "dead-code", "quality"],
               agents=["Claude Code", "Cursor"]))

# ── Security ──
_register(Loop("npm-audit-fix-loop", "no high or critical npm audit vulnerabilities", 10,
               "npm audit --audit-level=high && npm test",
               "npm audit reports no high/critical issues",
               "Pick one high/critical advisory, apply safest fix, run tests, repeat.",
               category="Security", url="/loops/npm-audit-fix-loop", tags=["security", "npm", "audit"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("iac-security-review", "all IaC changes reviewed; critical+high findings fixed", None,
               "git diff main --name-only -- '*.tf' '*.tfvars'",
               "critical + high findings == 0",
               "Collect IaC diff, send to Flare API, fix critical/high findings, re-check.",
               category="Security", url="/loops/iac-security-review-mq6r1k5a", tags=["security", "iac", "terraform"],
               agents=["Cursor"], trigger_type="interval"))

# ── Automation ──
_register(Loop("pr-babysitter", "open PRs labeled codex-watch are healthy (CI green, rebased)", 20,
               "gh pr list --label codex-watch",
               "each watched PR is green and current, or escalated",
               "List watched PRs. Fix CI once, rebase if behind, comment if stale. Escalate repeated failures.",
               category="Automation", url="/loops/pr-babysitter", tags=["pr", "github", "ci"],
               agents=["Codex", "Cursor"], trigger_type="interval"))
_register(Loop("deploy-verification-loop", "all post-deploy health endpoints return success", 8,
               "curl -fsS <health-url>",
               "every configured endpoint succeeds",
               "Hit health/smoke URLs. If any fail, inspect deploy logs and fix or escalate.",
               category="Automation", url="/loops/deploy-verification-loop", tags=["deploy", "health", "ops"],
               agents=["Codex", "Cursor"], trigger_type="interval"))
_register(Loop("post-merge-regression-guard", "smoke tests pass immediately after every merge or rebase", None,
               "npm run test:smoke", "smoke suite exits 0",
               "After a merge, run smoke tests. Fix regressions before continuing other work.",
               category="Automation", url="/loops/post-merge-regression-guard", tags=["merge", "smoke", "regression"],
               agents=["Claude Code"], trigger_type="event"))
_register(Loop("staging-smoke-test", "staging smoke checklist passes", 6,
               "npm run smoke:staging", "smoke command exits 0",
               "Run the staging smoke checklist. Fix the first failing item, then repeat.",
               category="Automation", url="/loops/staging-smoke-test", tags=["staging", "smoke", "testing"],
               agents=["Claude Code", "Cursor"]))

# ── API / Docs ──
_register(Loop("api-contract-until-match", "API implementation matches published contract", 10,
               "npm run test:contract", "contract test suite exits 0",
               "Run contract tests. Fix each schema/response mismatch with minimal diffs.",
               category="API", url="/loops/api-contract-until-match", tags=["api", "contract", "testing"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("openapi-sync-until-valid", "openapi.yaml lints clean and matches implemented routes", 8,
               "npx @redocly/cli lint openapi.yaml",
               "OpenAPI lint exits 0",
               "Lint openapi.yaml. Fix spec errors and handler drift until lint passes.",
               category="API", url="/loops/openapi-sync-until-valid", tags=["openapi", "spec", "api"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("docs-sync-after-edits", "documentation matches current code changes", 3,
               "git diff main...HEAD --name-only",
               "all affected docs are updated and verified",
               "Review the diff, find stale docs, update them, verify accuracy.",
               category="Docs", url="/loops/docs-sync-after-edits", tags=["docs", "documentation"],
               agents=["Claude Code", "Cursor"]))

# ── DevOps ──
_register(Loop("merge-conflict-resolver", "branch rebased on main with no conflicts and tests pass", 8,
               "npm test",
               "rebase completes and tests exit 0",
               "Rebase on main. Resolve conflicts one file at a time, run tests, continue.",
               category="DevOps", url="/loops/merge-conflict-resolver", tags=["merge", "conflict", "git"],
               agents=["Claude Code", "Cursor"]))
_register(Loop("independent-verifier-pass", "build, lint, tests pass under independent verification", 8,
               "npm run build && npm run lint && npm test",
               "all verifier commands exit 0",
               "Run build, lint, and tests as a verifier. Trust only command output, not prior claims.",
               category="DevOps", url="/loops/independent-verifier-pass", tags=["verification", "quality"],
               agents=["Claude Code", "Codex"]))

# ── Git ──
_register(Loop("pre-commit-guard", "block git commits when tests are failing", None,
               "npm test", "tests exit 0 before each commit",
               "Before any git commit, run tests. Fix failures before committing.",
               category="Git", url="/loops/pre-commit-guard", tags=["hooks", "testing", "git", "pre-commit"],
               agents=["Cursor", "Claude Code"], trigger_type="event"))

# ── Performance ──
_register(Loop("visual-regression-until-match", "visual regression suite passes with intentional UI only", 6,
               "npx playwright test --grep @visual",
               "visual tests exit 0",
               "Run visual tests. Fix unintended UI diffs; update baselines for deliberate changes.",
               category="Performance", url="/loops/visual-regression-until-match", tags=["visual", "playwright", "testing"],
               agents=["Cursor", "Claude Code"]))

# ── Planning / Meta ──
_register(Loop("ralph-story-executor", "every story in .ralph/prd.json has passes: true", 20,
               "npm test && npm run lint && npm run build",
               "no stories remain with passes: false",
               "Read .ralph/prd.json and .ralph/progress.md. Pick one incomplete story, implement, backpressure, commit.",
               category="Planning", url="/loops/ralph-story-executor", tags=["ralph", "stories", "planning"],
               agents=["Claude Code", "Cursor"]))

# ── Maintenance (interval) ──
_register(Loop("dependency-audit-weekly", "deliver a weekly dependency audit summary", None,
               "npm outdated || true",
               "summary is posted with recommended upgrades",
               "Run npm outdated, categorize updates, propose safe upgrade plan.",
               category="Maintenance", url="/loops/dependency-audit-weekly", tags=["deps", "audit", "weekly"],
               agents=["Claude Code"], trigger_type="interval"))
_register(Loop("security-audit-weekly", "deliver a weekly npm audit summary with remediation plan", None,
               "npm audit --json",
               "summary is posted with prioritized fixes",
               "Run npm audit, triage by severity, propose safe remediation steps.",
               category="Security", url="/loops/security-audit-weekly", tags=["security", "audit", "weekly"],
               agents=["Claude Code"], trigger_type="interval"))

# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

_LOOP_SELF_PACE = (
    "Self-pace this loop. After each iteration, run the check command, "
    "read the output, and only continue if the exit condition is not met. "
    "Stop when the exit condition passes or max iterations is reached. "
    "Give a short status update each pass."
)


def resolve_loop(name_or_alias: str) -> Loop | None:
    """Find a loop by name or alias (case-insensitive, dash/space flexible)."""
    key = name_or_alias.lower().replace(" ", "-").replace("_", "-")
    if key in LOOP_REGISTRY:
        return LOOP_REGISTRY[key]
    for loop in LOOP_REGISTRY.values():
        if key in loop.name or loop.name in key:
            return loop
    for loop in LOOP_REGISTRY.values():
        if any(key in t.lower() for t in loop.tags):
            return loop
    return None


def list_loops(category: str = "", tag: str = "") -> list[Loop]:
    """Filter loops by category and/or tag."""
    results = list(LOOP_REGISTRY.values())
    if category:
        results = [l for l in results if l.category.lower() == category.lower()]
    if tag:
        results = [l for l in results if tag.lower() in [t.lower() for t in l.tags]]
    return sorted(results, key=lambda l: l.name)


def generate_kickoff(loop: Loop, **params: str) -> str:
    """Generate the kickoff prompt for a loop."""
    parts = [
        f'/loop {loop.name}',
        f'Goal: {loop.goal}',
    ]
    if loop.max_iters is not None:
        parts.append(f'Max iterations: {loop.max_iters}')
    else:
        parts.append('Max iterations: continuous (event/interval)')
    parts.append(f'Between iterations: {loop.check_cmd}')
    parts.append(f'Exit when: {loop.exit_condition}')
    parts.append('')
    parts.append(f'Step 1: {loop.step_1}')
    if params:
        param_line = " ".join(f"{k}={v}" for k, v in params.items())
        parts.append(f'Params: {param_line}')
    parts.append('')
    parts.append(_LOOP_SELF_PACE)
    return "\n".join(parts)


def run_loop(
    loop: Loop,
    check_fn: Callable[[str], tuple[int, str]] | None = None,
    max_iters: int | None = None,
    params: dict[str, str] | None = None,
    interval_s: int = 0,
) -> LoopResult:
    """
    Run a loop until exit condition or max iterations.

    ``check_fn`` receives the check command string and must return
    (returncode, stdout+stderr).  Defaults to ``subprocess.run``.
    """
    check_fn = check_fn or _default_check
    iters = max_iters if max_iters is not None else loop.max_iters
    if iters is None:
        iters = 999  # safety cap for continuous loops
    start = time.monotonic()
    history: list[dict] = []

    for i in range(1, iters + 1):
        logger.info("[loops] %s iteration %d/%d", loop.name, i, iters)
        rc, output = check_fn(loop.check_cmd)
        history.append({"iteration": i, "rc": rc, "output": output[:500]})
        exit_met = _exit_condition_met(loop.exit_condition, rc, output)

        if exit_met:
            elapsed = time.monotonic() - start
            logger.info("[loops] %s exit condition met after %d iterations", loop.name, i)
            return LoopResult(
                name=loop.name, success=True, iterations=i,
                total_seconds=elapsed, last_output=output, history=history,
            )
        if i < iters:
            logger.info("[loops] %s iteration %d done, condition not met, continuing", loop.name, i)
        if interval_s:
            time.sleep(interval_s)

    elapsed = time.monotonic() - start
    logger.warning("[loops] %s max iterations (%d) reached without success", loop.name, iters)
    return LoopResult(
        name=loop.name, success=False, iterations=iters,
        total_seconds=elapsed, last_output=history[-1]["output"] if history else "",
        history=history,
        error=f"Max iterations ({iters}) reached without satisfying exit condition",
    )


def _default_check(cmd: str) -> tuple[int, str]:
    """Run a shell command and return (returncode, combined output)."""
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
        out = r.stdout + r.stderr
        return r.returncode, out.strip()
    except subprocess.TimeoutExpired:
        return -1, "TIMEOUT"
    except FileNotFoundError as e:
        return -2, str(e)
    except Exception as e:
        return -3, str(e)


def _exit_condition_met(condition: str, rc: int, output: str) -> bool:
    """Heuristic exit condition checker."""
    cond = condition.lower()

    # "exits 0" / "exit 0"
    if "exit" in cond and "0" in cond:
        return rc == 0

    # "success"
    if cond in ("success", "all checks pass", "clean"):
        return rc == 0

    # "no X" / "zero X"
    no_match = re.search(r"(?:no|zero|0)\s+(\w+)", cond)
    if no_match:
        target = no_match.group(1).lower()
        return target not in output.lower()

    # "is met" / "complete"
    if "is met" in cond or "complete" in cond or "passes" in cond:
        return rc == 0

    # "summary is posted" — interval loops, assume success once run
    if "summary" in cond and "post" in cond:
        return True

    # Default: check return code
    return rc == 0


# ---------------------------------------------------------------------------
# Integration helpers
# ---------------------------------------------------------------------------

LOOP_REGISTRY_JSON: str = ""


def _build_registry_json() -> str:
    """Serialise the full loop registry for brain storage."""
    data = {}
    for name, loop in LOOP_REGISTRY.items():
        data[name] = {
            "name": loop.name,
            "goal": loop.goal,
            "max_iters": loop.max_iters,
            "check_cmd": loop.check_cmd,
            "exit_condition": loop.exit_condition,
            "step_1": loop.step_1,
            "category": loop.category,
            "url": f"https://loops.elorm.xyz{loop.url}",
            "tags": loop.tags,
            "agents": loop.agents,
            "trigger_type": loop.trigger_type,
        }
    return json.dumps(data, indent=2, ensure_ascii=False)


LOOP_REGISTRY_JSON = _build_registry_json()
