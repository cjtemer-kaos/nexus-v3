"""
ComfyUI Gateway — Job queue for image generation on Remote Node.

Features:
- Async job queue (BullMQ-inspired, using asyncio)
- VRAM/OOM error classification
- Workflow template rendering
- Status tracking
"""

import asyncio
import hashlib
import json
import logging
import os
from typing import Any, Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

logger = logging.getLogger(__name__)

REMOTE_COMFYUI_URL = f"http://{os.environ.get('SUPER_NEXUS_REMOTE_NODE_IP', 'localhost')}:8188"

# Ideogram-4 (nf4/fp8) needs CUDA (bitsandbytes) → must run on the local CUDA node
# (PC1 / RTX 3060), NOT the AMD remote node. Defaults to localhost where the
# NEXUS master runs.
IDEOGRAM_COMFYUI_URL = f"http://{os.environ.get('SUPER_NEXUS_CUDA_NODE_IP', 'localhost')}:8188"

# Workflows that require CUDA — routed to IDEOGRAM_COMFYUI_URL, never the PC2 executor.
CUDA_WORKFLOWS = {"ideogram_v4"}


class JobStatus(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    OOM = "oom"  # GPU out of memory


@dataclass
class ImageJob:
    id: str
    prompt: str
    workflow: str = "default"
    params: Dict[str, Any] = field(default_factory=dict)
    status: JobStatus = JobStatus.PENDING
    result_url: Optional[str] = None
    error: Optional[str] = None
    created_at: str = ""
    completed_at: Optional[str] = None
    retries: int = 0
    cache_key: str = ""

    def __post_init__(self):
        if not self.created_at:
            self.created_at = datetime.now().isoformat()
        if not self.cache_key:
            # SHA-256 of params for cache (from comfyui-gateway skill)
            raw = json.dumps({"prompt": self.prompt, **self.params}, sort_keys=True)
            self.cache_key = hashlib.sha256(raw.encode()).hexdigest()[:16]


# Workflow templates (ComfyUI API format placeholders)
WORKFLOW_TEMPLATES = {
    "default": {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": "{{seed}}", "steps": "{{steps|20}}", "cfg": "{{cfg|7}}",
            "sampler_name": "euler", "scheduler": "normal",
            "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
        }},
        "6": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{prompt}}", "clip": ["4", 1]}},
        "7": {"class_type": "CLIPTextEncode", "inputs": {"text": "{{negative|bad quality}}", "clip": ["4", 1]}},
    },
    "fast": {
        "3": {"class_type": "KSampler", "inputs": {
            "seed": "{{seed}}", "steps": "{{steps|8}}", "cfg": "{{cfg|2}}",
            "sampler_name": "lcm", "scheduler": "sgm_uniform",
            "positive": ["6", 0], "negative": ["7", 0], "latent_image": ["5", 0],
        }},
    },
    # Ideogram 4.0 (open weights, local, CUDA). Best for text-in-image:
    # carteles, títulos, lower-thirds, thumbnails. nf4 por default (≤8GB VRAM).
    # Magic Prompt se omite a propósito — el gem `design` expande el prompt con
    # Ollama local antes de submit (soberanía, sin API externa).
    # Nodos: ComfyUI-Ideogram4 (ideogram-oss). class_type confirmados.
    "ideogram_v4": {
        "1": {"class_type": "Ideogram4PipelineLoader", "inputs": {
            "model_weights": "{{model_weights|4.0 NF4}}",
        }},
        "2": {"class_type": "Ideogram4Generate", "inputs": {
            "pipeline": ["1", 0],
            "prompt": "{{prompt}}",
            "width": "{{width|1024}}",
            "height": "{{height|1024}}",
            "sampler_preset": "{{sampler_preset|4.0 Default 20}}",
            "num_steps": "{{num_steps|20}}",
            "guidance_scale": "{{guidance_scale|5.0}}",
            "mu": "{{mu|0.0}}",
            "std": "{{std|1.0}}",
            "seed": "{{seed|0}}",
        }},
        "3": {"class_type": "SaveImage", "inputs": {
            "images": ["2", 0],
            "filename_prefix": "{{filename_prefix|ideogram_v4}}",
        }},
    },
}


def classify_error(error_msg: str) -> JobStatus:
    """Classify ComfyUI errors (from comfyui-gateway skill)."""
    oom_signals = ["out of memory", "oom", "cuda", "vram", "allocat"]
    if any(s in error_msg.lower() for s in oom_signals):
        return JobStatus.OOM
    return JobStatus.FAILED


def render_workflow(template_name: str, params: Dict[str, Any]) -> Dict:
    """Render workflow template with params (placeholder substitution)."""
    template = WORKFLOW_TEMPLATES.get(template_name, WORKFLOW_TEMPLATES["default"])
    rendered = json.dumps(template)

    import re
    def replace_placeholder(match):
        key = match.group(1)
        if "|" in key:
            key, default = key.split("|", 1)
            return str(params.get(key.strip(), default.strip()))
        return str(params.get(key.strip(), match.group(0)))

    rendered = re.sub(r'\{\{(\w+(?:\|[^}]*)?)\}\}', replace_placeholder, rendered)
    graph = json.loads(rendered)

    # Coerce pure-numeric leaf strings to int/float — ComfyUI nodes (e.g.
    # Ideogram4Generate) type-check INT/FLOAT inputs and reject "1536" (str).
    # String-expected fields (prompt, sampler_preset) aren't pure-numeric, so
    # they're left untouched.
    int_re = re.compile(r'^-?\d+$')
    float_re = re.compile(r'^-?\d+\.\d+$')

    def coerce(value):
        if isinstance(value, str):
            if int_re.match(value):
                return int(value)
            if float_re.match(value):
                return float(value)
            return value
        if isinstance(value, dict):
            return {k: coerce(v) for k, v in value.items()}
        # Lists are ComfyUI node-reference edges [node_id_str, port_int] —
        # the node id MUST stay a string. Never coerce list elements.
        return value

    return coerce(graph)


class ComfyUIGateway:
    """Async job queue for ComfyUI on Remote Node."""

    def __init__(self, comfyui_url: str = REMOTE_COMFYUI_URL, max_concurrent: int = 2,
                 cuda_url: str = IDEOGRAM_COMFYUI_URL):
        self.comfyui_url = comfyui_url
        self.cuda_url = cuda_url  # local CUDA node for Ideogram/bitsandbytes workflows
        self.max_concurrent = max_concurrent
        self.jobs: Dict[str, ImageJob] = {}
        self.queue: asyncio.Queue = asyncio.Queue()
        self._cache: Dict[str, str] = {}  # cache_key → result_url
        self._running = False

    async def submit(self, prompt: str, workflow: str = "default", **params) -> ImageJob:
        """Submit an image generation job."""
        import uuid
        job = ImageJob(
            id=str(uuid.uuid4())[:8],
            prompt=prompt,
            workflow=workflow,
            params=params,
        )

        # Check cache
        if job.cache_key in self._cache:
            job.status = JobStatus.COMPLETED
            job.result_url = self._cache[job.cache_key]
            job.completed_at = datetime.now().isoformat()
            logger.info(f"Cache hit for job {job.id}")
            self.jobs[job.id] = job
            return job

        self.jobs[job.id] = job
        await self.queue.put(job.id)
        logger.info(f"Job {job.id} queued: '{prompt[:50]}...'")
        return job

    async def _process_job(self, job: ImageJob, execute_on_pc2=None) -> ImageJob:
        """Process a single job via ComfyUI API.

        CUDA workflows (Ideogram) run on the local CUDA node (self.cuda_url) via
        direct HTTP — never through the PC2/AMD executor, which can't run them.
        """
        job.status = JobStatus.RUNNING

        is_cuda = job.workflow in CUDA_WORKFLOWS
        target_url = self.cuda_url if is_cuda else self.comfyui_url
        use_remote = bool(execute_on_pc2) and not is_cuda

        try:
            workflow = render_workflow(job.workflow, {"prompt": job.prompt, **job.params})

            if use_remote:
                # Use Remote Node executor
                payload = json.dumps({"prompt": workflow})
                result = await execute_on_pc2(
                    f"curl -s -X POST {target_url}/prompt "
                    f"-H 'Content-Type: application/json' "
                    f"-d '{payload}'"
                )
                result_str = str(result)
                classified = classify_error(result_str)
                if classified == JobStatus.OOM:
                    job.status = JobStatus.OOM
                    job.error = (
                        "GPU Out of Memory (OOM). Recommended: Reduce resolution, decrease batch size, "
                        f"or limit steps. The system will automatically retry at halved resolution. Original details: {result_str[:200]}"
                    )
                else:
                    job.status = JobStatus.COMPLETED
                    job.result_url = result_str[:500]
                    job.completed_at = datetime.now().isoformat()
                    self._cache[job.cache_key] = job.result_url
            else:
                # Direct HTTP (if accessible)
                try:
                    import httpx
                    async with httpx.AsyncClient(timeout=180) as client:
                        r = await client.post(
                            f"{target_url}/prompt",
                            json={"prompt": workflow},
                        )
                        if r.status_code == 200:
                            data = r.json()
                            job.status = JobStatus.COMPLETED
                            job.result_url = data.get("prompt_id", "")
                            job.completed_at = datetime.now().isoformat()
                            self._cache[job.cache_key] = job.result_url
                        else:
                            job.status = classify_error(r.text)
                            if job.status == JobStatus.OOM:
                                job.error = (
                                    "GPU Out of Memory (OOM). Recommended: Reduce resolution, decrease batch size, "
                                    f"or limit steps. The system will automatically retry at halved resolution. Original details: {r.text[:200]}"
                                )
                            else:
                                job.error = r.text[:500]
                except ImportError:
                    job.status = JobStatus.FAILED
                    job.error = "httpx not available and no Remote Node executor"

        except Exception as e:
            error_msg = str(e)
            job.status = classify_error(error_msg)
            if job.status == JobStatus.OOM:
                job.error = (
                    "GPU Out of Memory (OOM). Recommended: Reduce resolution, decrease batch size, "
                    f"or limit steps. The system will automatically retry at halved resolution. Original details: {error_msg[:200]}"
                )
            else:
                job.error = error_msg[:500]

        # Auto-retry for OOM (reduce resolution)
        if job.status == JobStatus.OOM and job.retries < 2:
            job.retries += 1
            job.params["width"] = job.params.get("width", 1024) // 2
            job.params["height"] = job.params.get("height", 1024) // 2
            logger.warning(f"OOM on job {job.id}, retrying at lower resolution ({job.retries}/2)")
            return await self._process_job(job, execute_on_pc2)

        return job

    def get_job(self, job_id: str) -> Optional[ImageJob]:
        return self.jobs.get(job_id)

    def list_jobs(self, status: Optional[str] = None) -> List[Dict]:
        jobs = self.jobs.values()
        if status:
            jobs = [j for j in jobs if j.status.value == status]
        return [
            {
                "id": j.id, "prompt": j.prompt[:80], "status": j.status.value,
                "workflow": j.workflow, "created": j.created_at,
                "completed": j.completed_at, "error": j.error,
            }
            for j in jobs
        ]

    def get_stats(self) -> Dict:
        status_counts = {}
        for j in self.jobs.values():
            status_counts[j.status.value] = status_counts.get(j.status.value, 0) + 1
        return {
            "total_jobs": len(self.jobs),
            "queue_size": self.queue.qsize(),
            "cache_entries": len(self._cache),
            "status_breakdown": status_counts,
        }

