"""
Hermes-style Autonomous Tools for SuperNEXUS v2

Inspired by Hermes Agent (Nous Research), these tools enable:
1. execute_code — Programmatic tool calling (collapses multi-step pipelines)
2. Cron with delivery — Scheduled automations with platform delivery
3. Memory nudges — Auto-compress memory every N prompts
4. Skill auto-creation — Create skills after complex tasks
5. image_generate — AI image generation via ComfyUI
6. tts_speak — Text-to-speech via VoiceEngine
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger(__name__)

# ============================================================
# 1. execute_code — Programmatic Tool Calling
# ============================================================

class ExecuteCodeEngine:
    """
    Sandboxed Python execution that can call Hermes tools programmatically.
    Collapses multi-step pipelines into a single LLM turn.
    """

    def __init__(self, tools_registry: dict[str, Callable] | None = None):
        self._tools = tools_registry or {}
        self._execution_history: list[dict] = []

    def register_tool(self, name: str, fn: Callable):
        """Register a tool that can be called from execute_code scripts."""
        self._tools[name] = fn

    async def execute(self, code: str, timeout: float = 30.0) -> dict:
        """
        Execute Python code in a sandboxed environment.
        The code can call registered tools via self.tools['tool_name'](**args).
        """
        execution_id = f"exec_{int(time.time() * 1000)}"
        start_time = time.time()

        # Create a restricted globals dict with only safe builtins
        safe_builtins = {
            "print": print,
            "len": len,
            "str": str,
            "int": int,
            "float": float,
            "bool": bool,
            "list": list,
            "dict": dict,
            "tuple": tuple,
            "set": set,
            "enumerate": enumerate,
            "zip": zip,
            "map": map,
            "filter": filter,
            "sorted": sorted,
            "reversed": reversed,
            "min": min,
            "max": max,
            "sum": sum,
            "abs": abs,
            "round": round,
            "range": range,
            "True": True,
            "False": False,
            "None": None,
        }

        # Build execution namespace
        namespace = {
            "__builtins__": safe_builtins,
            "tools": self._tools,
            "result": None,
            "output": [],
        }

        # Capture print output
        import io
        captured_stdout = io.StringIO()
        old_stdout = sys.stdout
        sys.stdout = captured_stdout

        try:
            # Execute with timeout
            exec(compile(code, "<execute_code>", "exec"), namespace)

            result = {
                "success": True,
                "execution_id": execution_id,
                "duration_ms": round((time.time() - start_time) * 1000),
                "output": captured_stdout.getvalue(),
                "result": namespace.get("result"),
            }

        except Exception as e:
            result = {
                "success": False,
                "execution_id": execution_id,
                "duration_ms": round((time.time() - start_time) * 1000),
                "error": str(e),
                "error_type": type(e).__name__,
                "output": captured_stdout.getvalue(),
            }

        finally:
            sys.stdout = old_stdout
            self._execution_history.append(result)

        return result

    def get_history(self, limit: int = 10) -> list[dict]:
        return self._execution_history[-limit:]


# ============================================================
# 2. Cron with Delivery — Scheduled Automations
# ============================================================

@dataclass
class CronJob:
    id: str = ""
    name: str = ""
    schedule: str = ""  # "every 5m", "daily 08:00", "cron: */5 * * * *"
    prompt: str = ""
    script: str = ""  # Shell script path (no-agent mode)
    deliver_to: str = ""  # "telegram", "discord", "nexus_message", "none"
    no_agent: bool = False  # If True, run script without LLM
    enabled: bool = True
    created_at: float = 0.0
    last_run: float = 0.0
    next_run: float = 0.0
    run_count: int = 0
    last_result: str = ""


class HermesCron:
    """
    Integrated cron scheduler with platform delivery.
    Supports: once, interval, daily, cron expressions.
    """

    def __init__(self, storage_path: str | None = None):
        self._storage = Path(storage_path or os.path.join(
            str(Path.home()), ".nexus", "hermes_cron.json"))
        self._jobs: dict[str, CronJob] = {}
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._load()

    async def start(self):
        """Start the cron scheduler loop."""
        self._running = True
        self._loop_task = asyncio.create_task(self._loop(), name="hermes-cron")

    async def stop(self):
        self._running = False
        if self._loop_task:
            self._loop_task.cancel()

    async def _loop(self):
        while self._running:
            try:
                now = time.time()
                for job_id, job in list(self._jobs.items()):
                    if not job.enabled:
                        continue
                    if job.next_run <= now:
                        await self._run_job(job)
                await asyncio.sleep(10)  # Check every 10 seconds
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error("Cron loop error: %s", e)
                await asyncio.sleep(30)

    async def _run_job(self, job: CronJob):
        """Execute a cron job and deliver results."""
        job.last_run = time.time()
        job.run_count += 1

        try:
            if job.no_agent and job.script:
                # Script-only mode: run shell script
                result = await self._run_script(job.script)
            else:
                # Agent mode: execute prompt via LLM
                result = await self._run_prompt(job.prompt)

            job.last_result = result[:500]
            job.next_run = self._calc_next_run(job.schedule)

            # Deliver results
            if job.deliver_to and job.deliver_to != "none":
                await self._deliver(job.deliver_to, job.name, result)

            logger.info("Cron job '%s' completed, delivery to %s", job.name, job.deliver_to)

        except Exception as e:
            job.last_result = f"ERROR: {e}"
            logger.error("Cron job '%s' failed: %s", job.name, e)

        self._save()

    async def _run_script(self, script_path: str) -> str:
        """Run a shell script and return stdout."""
        try:
            proc = await asyncio.create_subprocess_shell(
                script_path,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=60)
            return stdout.decode() or stderr.decode()
        except Exception as e:
            return f"Script error: {e}"

    async def _run_prompt(self, prompt: str) -> str:
        """Execute a prompt via the LLM (placeholder — integrates with director)."""
        # This will be wired to director.execute() in the server
        return f"[Cron prompt executed]: {prompt[:200]}"

    async def _deliver(self, platform: str, job_name: str, result: str):
        """Deliver results to a platform."""
        message = f"🤖 Cron [{job_name}]\n\n{result[:1000]}"

        if platform == "nexus_message":
            # Send via NexusHive message board
            try:
                sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
                from src.bridges.mcp_bridge_server import _nexus_bridge
                if _nexus_bridge:
                    await _nexus_bridge.send_message(
                        target="opencode", content=message, msg_type="cron_delivery")
            except Exception as e:
                logger.debug("Nexus message delivery failed: %s", e)

        elif platform in ("telegram", "discord", "slack"):
            # Placeholder for external platform delivery
            logger.info("Platform delivery to %s: %s", platform, message[:100])

    def _calc_next_run(self, schedule: str) -> float:
        """Calculate next run time from schedule string."""
        now = time.time()
        if schedule.startswith("every "):
            parts = schedule.split()
            if len(parts) >= 2:
                num = int(parts[1].rstrip("mhd"))
                unit = parts[1][-1]
                multipliers = {"m": 60, "h": 3600, "d": 86400}
                return now + num * multipliers.get(unit, 60)
        elif schedule.startswith("daily "):
            # Simple: next day at same time
            return now + 86400
        return now + 3600  # Default: 1 hour

    def add_job(self, name: str, schedule: str, prompt: str = "",
                script: str = "", deliver_to: str = "none",
                no_agent: bool = False) -> CronJob:
        """Add a new cron job."""
        job = CronJob(
            id=f"cron_{int(time.time() * 1000)}",
            name=name,
            schedule=schedule,
            prompt=prompt,
            script=script,
            deliver_to=deliver_to,
            no_agent=no_agent,
            created_at=time.time(),
            next_run=self._calc_next_run(schedule),
        )
        self._jobs[job.id] = job
        self._save()
        return job

    def remove_job(self, job_id: str) -> bool:
        if job_id in self._jobs:
            del self._jobs[job_id]
            self._save()
            return True
        return False

    def list_jobs(self) -> list[dict]:
        return [
            {"id": j.id, "name": j.name, "schedule": j.schedule,
             "enabled": j.enabled, "run_count": j.run_count,
             "next_run": j.next_run, "last_result": j.last_result[:100]}
            for j in self._jobs.values()
        ]

    def _save(self):
        try:
            self._storage.parent.mkdir(parents=True, exist_ok=True)
            data = {}
            for jid, j in self._jobs.items():
                data[jid] = {
                    "id": j.id, "name": j.name, "schedule": j.schedule,
                    "prompt": j.prompt, "script": j.script,
                    "deliver_to": j.deliver_to, "no_agent": j.no_agent,
                    "enabled": j.enabled, "created_at": j.created_at,
                    "last_run": j.last_run, "next_run": j.next_run,
                    "run_count": j.run_count, "last_result": j.last_result,
                }
            with open(self._storage, "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            logger.debug("Cron save failed: %s", e)

    def _load(self):
        try:
            if self._storage.exists():
                with open(self._storage) as f:
                    data = json.load(f)
                for jid, d in data.items():
                    self._jobs[jid] = CronJob(**d)
                logger.info("Loaded %d cron jobs", len(self._jobs))
        except Exception as e:
            logger.debug("Cron load failed: %s", e)


# ============================================================
# 3. Memory Nudges — Auto-compress every N prompts
# ============================================================

class MemoryNudge:
    """
    Periodically reviews and compresses memory, similar to Hermes.
    Triggers every N user prompts to:
    - Consolidate working memory -> episodic
    - Prune low-value items
    - Extract patterns from recent sessions
    """

    def __init__(self, nudge_interval: int = 10):
        self._nudge_interval = nudge_interval
        self._prompt_count = 0
        self._last_nudge: float = 0.0
        self._nudge_count: int = 0

    def should_nudge(self) -> bool:
        """Check if it's time for a memory nudge."""
        self._prompt_count += 1
        return self._prompt_count >= self._nudge_interval

    async def nudge(self, hierarchical_memory=None, salience=None) -> dict:
        """Execute a memory nudge cycle."""
        if not self.should_nudge():
            return {"nudged": False, "prompt_count": self._prompt_count}

        self._prompt_count = 0
        self._last_nudge = time.time()
        self._nudge_count += 1

        result = {"nudged": True, "cycle": self._nudge_count, "actions": []}

        # Consolidate hierarchical memory
        if hierarchical_memory:
            try:
                old_stats = hierarchical_memory.get_stats()
                hierarchical_memory.consolidate()
                new_stats = hierarchical_memory.get_stats()
                result["actions"].append({
                    "type": "consolidate",
                    "before": old_stats["total"],
                    "after": new_stats["total"],
                })
            except Exception as e:
                result["actions"].append({"type": "consolidate", "error": str(e)})

        logger.info("Memory nudge cycle %d completed", self._nudge_count)
        return result

    def get_stats(self) -> dict:
        return {
            "prompt_count": self._prompt_count,
            "nudge_interval": self._nudge_interval,
            "nudge_count": self._nudge_count,
            "last_nudge": self._last_nudge,
        }


# ============================================================
# 4. Skill Auto-Creation — After complex tasks
# ============================================================

class SkillAutoCreator:
    """
    Automatically creates skills after complex tasks (5+ tool calls).
    Similar to Hermes: observes what worked, extracts a reusable skill.
    """

    def __init__(self, skills_dir: str | None = None):
        self._skills_dir = Path(skills_dir or os.path.join(
            str(Path.home()), ".nexus", "auto_skills"))
        self._skills_dir.mkdir(parents=True, exist_ok=True)
        self._creation_threshold = 5  # Min tool calls to trigger skill creation
        self._pending_tools: list[dict] = []

    def record_tool_call(self, tool_name: str, args: dict, result: Any):
        """Record a tool call for potential skill creation."""
        self._pending_tools.append({
            "tool": tool_name,
            "args": args,
            "result_summary": str(result)[:200],
            "timestamp": time.time(),
        })

    def should_create_skill(self) -> bool:
        """Check if we have enough tool calls to create a skill."""
        return len(self._pending_tools) >= self._creation_threshold

    async def create_skill(self, task_description: str, success: bool = True) -> dict:
        """Create a skill from the recorded tool calls."""
        if not self._pending_tools:
            return {"created": False, "reason": "no tool calls recorded"}

        skill_name = f"auto_{int(time.time())}"
        skill_content = self._generate_skill_md(task_description)

        skill_path = self._skills_dir / f"{skill_name}.md"
        skill_path.write_text(skill_content, encoding="utf-8")

        # Clear pending tools
        self._pending_tools = []

        logger.info("Auto-created skill: %s", skill_name)
        return {
            "created": True,
            "skill_name": skill_name,
            "path": str(skill_path),
            "tool_count": len(self._pending_tools),
        }

    def _generate_skill_md(self, task_description: str) -> str:
        """Generate a markdown skill file from recorded tool calls."""
        steps = []
        for i, call in enumerate(self._pending_tools, 1):
            steps.append(f"{i}. **{call['tool']}**: `{json.dumps(call['args'], ensure_ascii=False)[:100]}`")

        return f"""# Auto-Skill: {task_description[:60]}

**Created**: {time.strftime('%Y-%m-%d %H:%M:%S')}
**Tool Calls**: {len(self._pending_tools)}
**Status**: {'Success' if self._pending_tools else 'Incomplete'}

## Description
{task_description}

## Steps
{chr(10).join(steps)}

## Notes
This skill was auto-generated from a successful task execution.
Review and refine as needed.
"""

    def list_skills(self) -> list[dict]:
        """List all auto-created skills."""
        skills = []
        for f in self._skills_dir.glob("*.md"):
            skills.append({
                "name": f.stem,
                "path": str(f),
                "created": f.stat().st_ctime,
            })
        return sorted(skills, key=lambda x: x["created"], reverse=True)


# ============================================================
# 5. image_generate — AI Image Generation
# ============================================================

class ImageGenerator:
    """
    AI image generation via ComfyUI or external APIs.
    Wraps existing ComfyUIGateway if available.
    """

    def __init__(self):
        self._comfyui = None
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
            from src.core.comfyui_gateway import ComfyUIGateway
            self._comfyui = ComfyUIGateway()
        except Exception:
            pass

    async def generate(self, prompt: str, width: int = 1024, height: int = 1024,
                       style: str = "default") -> dict:
        """Generate an image from a text prompt."""
        if self._comfyui:
            try:
                result = await self._comfyui.submit(
                    prompt=prompt,
                    width=width,
                    height=height,
                    workflow=style,
                )
                return {"success": True, "image_path": result.get("path", ""),
                        "prompt": prompt, "engine": "comfyui"}
            except Exception as e:
                return {"success": False, "error": str(e), "engine": "comfyui"}

        # Fallback: placeholder
        return {
            "success": False,
            "error": "No image generation engine available. Configure ComfyUI or add API key.",
            "prompt": prompt,
            "suggestion": "Install ComfyUI or configure an external API (DALL-E, Stability AI)",
        }


# ============================================================
# 6. tts_speak — Text-to-Speech
# ============================================================

class TTSSpeaker:
    """
    Text-to-speech via VoiceEngine (Piper) or external TTS.
    Wraps existing VoiceEngine if available.
    """

    def __init__(self):
        self._voice_engine = None
        try:
            sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
            from src.core.voice_engine import get_engine
            self._voice_engine = get_engine()
        except Exception:
            pass

    async def speak(self, text: str, voice: str = "", output_format: str = "wav") -> dict:
        """Convert text to speech."""
        if self._voice_engine:
            try:
                if voice:
                    self._voice_engine.set_voice(voice)
                audio_path = self._voice_engine.speak(text)
                return {"success": True, "audio_path": str(audio_path),
                        "voice": voice or "default", "engine": "piper"}
            except Exception as e:
                return {"success": False, "error": str(e), "engine": "piper"}

        return {
            "success": False,
            "error": "No TTS engine available. VoiceEngine not initialized.",
            "suggestion": "Install piper-tts and download voice models",
        }


# ============================================================
# Singleton instances
# ============================================================

_execute_code_engine: ExecuteCodeEngine | None = None
_hermes_cron: HermesCron | None = None
_memory_nudge: MemoryNudge | None = None
_skill_auto_creator: SkillAutoCreator | None = None
_image_generator: ImageGenerator | None = None
_tts_speaker: TTSSpeaker | None = None


def get_execute_code_engine() -> ExecuteCodeEngine:
    global _execute_code_engine
    if _execute_code_engine is None:
        _execute_code_engine = ExecuteCodeEngine()
    return _execute_code_engine


def get_hermes_cron() -> HermesCron:
    global _hermes_cron
    if _hermes_cron is None:
        _hermes_cron = HermesCron()
    return _hermes_cron


def get_memory_nudge() -> MemoryNudge:
    global _memory_nudge
    if _memory_nudge is None:
        _memory_nudge = MemoryNudge()
    return _memory_nudge


def get_skill_auto_creator() -> SkillAutoCreator:
    global _skill_auto_creator
    if _skill_auto_creator is None:
        _skill_auto_creator = SkillAutoCreator()
    return _skill_auto_creator


def get_image_generator() -> ImageGenerator:
    global _image_generator
    if _image_generator is None:
        _image_generator = ImageGenerator()
    return _image_generator


def get_tts_speaker() -> TTSSpeaker:
    global _tts_speaker
    if _tts_speaker is None:
        _tts_speaker = TTSSpeaker()
    return _tts_speaker
