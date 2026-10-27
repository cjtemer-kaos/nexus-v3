"""Jobs: el resultado decia `id` pero las tools piden `job_id`.

Medido con el registry real:

    job_start({"command": "echo hola"})
      -> {"id": "20261027_092030_a6cce0", "label": ..., "state": "running", ...}
    job_read({"id": "20261027_092030_a6cce0"})
      -> ValueError: job_read: falta el argumento requerido 'job_id' ...

El modelo copia la clave que vio en el resultado y pierde el turno. Ahora el
registro publica `job_id` (ademas de `id`) y `id` se resuelve como alias.
"""
from __future__ import annotations

import asyncio
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.core.jobs import JobRegistry  # noqa: E402
from src.core.tool_registry import validate_args  # noqa: E402


class _ProcTerminado:
    def poll(self):
        return 0


def _registrar(reg: JobRegistry, job_id: str = "j1") -> str:
    reg._jobs[job_id] = {
        "id": job_id, "label": "echo", "command": "echo", "cwd": ".",
        "pid": 1, "log": "x.log", "proc": _ProcTerminado(), "started": time.time(),
    }
    return job_id


def test_el_resultado_trae_job_id_y_sigue_trayendo_id(tmp_path):
    reg = JobRegistry()
    jid = _registrar(reg)

    pub = reg.get(jid)

    assert pub["job_id"] == jid
    assert pub["id"] == jid
    assert reg.list()[0]["job_id"] == jid


def test_id_es_alias_de_job_id():
    esq = {"type": "object",
           "properties": {"job_id": {"type": "string"}, "cursor": {"type": "integer"}},
           "required": ["job_id"], "additionalProperties": False}

    assert validate_args("job_read", {"id": "abc"}, esq) == {"job_id": "abc"}


def test_job_id_explicito_gana_sobre_id():
    esq = {"type": "object", "properties": {"job_id": {"type": "string"}},
           "required": ["job_id"], "additionalProperties": False}

    assert validate_args("job_read", {"id": "viejo", "job_id": "nuevo"}, esq)["job_id"] == "nuevo"


@pytest.fixture()
def registry_job(monkeypatch):
    from src.api import server as S
    from src.core import jobs as J

    def boom(*_a, **_k):
        raise KeyError("job desconocido: no-existe. Activos: j1")

    fake = SimpleNamespace(read=boom, wait=boom, kill=boom, list=lambda: [])
    monkeypatch.setattr(J, "get_jobs", lambda: fake)

    cfg = S._load_config_safe()
    scope = S._build_scope(cfg)
    reg = S._build_registry(scope, f"test-job-{uuid.uuid4().hex[:8]}", cfg)
    return reg, scope


def test_job_inexistente_es_value_error_sin_comillas(registry_job):
    reg, scope = registry_job

    for tool in ("job_read", "job_wait", "job_kill"):
        with pytest.raises(ValueError) as e:
            asyncio.run(reg.dispatch(tool, {"job_id": "no-existe"}, scope=scope, hooks=None))
        msg = str(e.value)
        assert "job desconocido" in msg, (tool, msg)
        assert not msg.startswith("'"), (tool, msg)
        assert "Activos: j1" in msg
