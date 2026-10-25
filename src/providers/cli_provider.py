"""CLIProvider: los CLIs de agentes (claude, codex, opencode) como proveedor mas.

No son "modelos": son procesos que ya traen su propio agente, sus tools y su
cuenta. Aca se usan como CONSULTORES (una pregunta, una respuesta), asi que el
proveedor declara `supports_tools=False` y el router lo saltea solo cuando el
turno necesita herramientas.

Tres cosas que costaron sangre y quedan fijas en el codigo:

1. `claude -p` se colgaba para siempre (45 s, stdout y stderr vacios). No era el
   CLI: `ANTHROPIC_BASE_URL` apuntaba a `127.0.0.1:3456`, un router local caido.
   Sin esa variable el mismo comando falla en 2,5 s con un 401 explicito. Antes
   de lanzar se sondea ese base_url: si no responde, se corta con un mensaje que
   dice que levantar, en vez de comerse el timeout entero.
2. `codex` lee stdin cuando no es una TTY ("Reading additional input from
   stdin..."): con un pipe abierto se queda esperando. Siempre stdin=DEVNULL.
3. En Windows `codex` y `opencode` se instalan con npm como shims .cmd/.ps1, que
   NO son ejecutables Win32. Se resuelven al prefijo correcto (cmd /c,
   powershell -File, node script.js) en vez de morir con "no es una aplicacion
   valida".

Nada de esto escribe en el repo por defecto: el cwd arranca en un directorio
descartable y cada CLI va en su modo mas restrictivo (claude
`--permission-mode plan`, codex `-s read-only`). Para que un CLI vea el proyecto
hay que pedirlo en config.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from src.core.provider_base import BaseProvider, LLMMessage, LLMResponse

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_S = 180.0
# El prompt viaja como argv: Windows corta en 32767 caracteres y los CLIs suman
# los suyos. Se recorta antes de acercarse.
MAX_PROMPT_CHARS = 24000
_HEAD_CHARS = 8000

# Salida de color/ANSI ensucia el parseo de JSON.
_BASE_ENV = {"NO_COLOR": "1", "FORCE_COLOR": "0", "CLICOLOR": "0", "TERM": "dumb"}


class CLIProviderError(RuntimeError):
    """Fallo al invocar el CLI, con el comando y la cola de stderr."""

    def __init__(self, message: str, *, command: list[str] | None = None,
                 returncode: int | None = None, stderr: str = "",
                 stdout: str = ""):
        self.command = list(command or [])
        self.returncode = returncode
        self.stderr = (stderr or "").strip()
        self.stdout = (stdout or "").strip()
        parts = [message]
        if returncode is not None:
            parts.append(f"exit={returncode}")
        if self.stderr:
            parts.append(f"stderr: {_tail(self.stderr, 600)}")
        elif self.stdout:
            parts.append(f"stdout: {_tail(self.stdout, 600)}")
        super().__init__(" | ".join(parts))


def _tail(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else "..." + text[-limit:]


def trim_prompt(text: str, limit: int = MAX_PROMPT_CHARS) -> str:
    """Recorta conservando el principio y el final.

    El final es donde va la pregunta; el principio, donde va el sistema. Lo que
    se tira es el medio, y queda marcado para que el modelo lo sepa.
    """
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    head = max(1000, min(_HEAD_CHARS, limit // 3))
    rest = limit - head - 40
    return f"{text[:head]}\n\n[...recortado {len(text) - limit} caracteres...]\n\n{text[-rest:]}"


def flatten_messages(messages: list[LLMMessage]) -> tuple[str, str]:
    """(system, user) de la conversacion; el CLI recibe un prompt unico.

    Se usa el ULTIMO mensaje de usuario como pregunta y se resumen los turnos
    previos: mandar la historia completa en un argv solo gasta caracteres y
    confunde a un CLI que ya trae su propia sesion.
    """
    system_parts: list[str] = []
    turns: list[str] = []
    for m in messages or []:
        content = (m.content or "").strip()
        if not content:
            continue
        role = (m.role or "user").lower()
        if role == "system":
            system_parts.append(content)
        elif role == "user":
            turns.append(content)
        elif role == "assistant":
            turns.append(f"(respuesta previa) {content}")
    system = "\n\n".join(system_parts)
    if not turns:
        return system, ""
    user = turns[-1]
    if len(turns) > 1:
        prev = "\n".join(_short(t, 400) for t in turns[:-1][-4:])
        user = f"Contexto de turnos previos:\n{prev}\n\nPedido actual:\n{user}"
    return system, user


def _short(text: str, limit: int) -> str:
    text = (text or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[:limit] + "..."
_SHIM_REF = re.compile(r'([^"\r\n]*node_modules[^"\r\n]*?\.(?:js|mjs|cjs|exe))', re.I)


def _shim_target(path: Path) -> list[str] | None:
    """Binario real detras de un shim de npm (.cmd/.ps1).

    Pasar el prompt por `cmd /c` rompe con `%`, `&` y comillas (cmd los
    interpreta antes que el hijo). Leyendo el shim se llega al `.exe` o al
    `.js`, y ahi el argv viaja intacto.
    """
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    base = path.parent
    for raw in _SHIM_REF.findall(text):
        cand = (raw.replace("%~dp0", str(base)).replace("%dp0%", str(base))
                .replace("$basedir", str(base)).strip().strip('"'))
        if not cand:
            continue
        p = Path(cand)
        if not p.is_absolute():
            p = base / cand
        if not p.exists():
            continue
        if p.suffix.lower() == ".exe":
            return [str(p)]
        return [shutil.which("node") or "node", str(p)]
    return None


def resolve_command(command: str | list[str] | tuple[str, ...]) -> list[str]:
    """Prefijo argv real para lanzar `command` (resuelve shims de npm en Windows).

    Acepta una lista (ya resuelta, util para tests y para `python script.py`).
    """
    if isinstance(command, (list, tuple)):
        parts = [str(p) for p in command if str(p)]
        if not parts:
            raise CLIProviderError("comando vacio")
        return parts
    raw = str(command or "").strip()
    if not raw:
        raise CLIProviderError("comando vacio")
    path = raw if (os.path.isabs(raw) or os.sep in raw or "/" in raw) else (shutil.which(raw) or "")
    if not path:
        raise CLIProviderError(
            f"no encontre {raw!r} en PATH (instalalo o poné la ruta completa en providers.cli)")
    path = str(Path(path))
    suffix = Path(path).suffix.lower()
    if suffix in (".cmd", ".bat", ".ps1"):
        target = _shim_target(Path(path))
        if target:
            return target
        if suffix == ".ps1":
            return ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", path]
        return ["cmd", "/c", path]
    if suffix in (".js", ".mjs", ".cjs"):
        node = shutil.which("node") or "node"
        return [node, path]
    return [path]


def check_base_url(url: str, *, timeout_s: float = 1.0) -> str:
    """'' si el host del url acepta TCP, si no una explicacion corta.

    Existe por el cuelgue de `claude -p`: con `ANTHROPIC_BASE_URL` apuntando a un
    proxy muerto, el CLI reintenta sin escribir nada y nunca termina.
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    try:
        parsed = urlparse(raw if "//" in raw else f"http://{raw}")
        host = parsed.hostname or ""
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except Exception:
        return f"{raw} no es una URL valida"
    if not host:
        return f"{raw} no tiene host"
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            return ""
    except OSError as e:
        return (f"{url} no responde ({type(e).__name__}). "
                f"Si es un router/proxy local, levantaló; si no, borrá esa variable de entorno.")


def _spawn_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        flags = 0
        for name in ("CREATE_NEW_PROCESS_GROUP", "CREATE_NO_WINDOW"):
            flags |= int(getattr(subprocess, name, 0) or 0)
        return {"creationflags": flags}
    return {"start_new_session": True}


async def kill_process_tree(proc) -> None:
    """Mata el proceso y TODOS sus hijos.

    Los CLIs son wrappers (node -> binario, cmd -> node): matar solo el padre
    deja al nieto escribiendo y el `communicate()` no vuelve nunca.
    """
    if proc is None or proc.returncode is not None:
        return
    try:
        if os.name == "nt":
            await asyncio.create_subprocess_exec(
                "taskkill", "/F", "/T", "/PID", str(proc.pid),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        else:
            import signal

            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except Exception:
        pass
    try:
        proc.kill()
    except (ProcessLookupError, OSError):
        pass
    try:
        await asyncio.wait_for(proc.wait(), 5)
    except Exception:
        pass


async def run_cli(prefix: list[str], args: list[str], *, timeout_s: float,
                  env: dict[str, str], cwd: Path) -> tuple[int, str, str]:
    """Corre el CLI y devuelve (exit, stdout, stderr). Timeout -> mata el arbol."""
    argv = [*prefix, *args]
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(cwd),
            env=env,
            **_spawn_kwargs(),
        )
    except FileNotFoundError as e:
        raise CLIProviderError(f"no pude ejecutar {argv[0]!r}: {e}", command=argv) from e
    except OSError as e:
        raise CLIProviderError(f"no pude ejecutar {argv[0]!r}: {e}", command=argv) from e
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=max(5.0, float(timeout_s)))
    except asyncio.TimeoutError:
        await kill_process_tree(proc)
        raise CLIProviderError(
            f"{Path(argv[0]).name} no termino en {int(timeout_s)}s (proceso y hijos terminados)",
            command=argv)
    return (proc.returncode if proc.returncode is not None else -1,
            (out or b"").decode("utf-8", "replace"),
            (err or b"").decode("utf-8", "replace"))
def _json_objects(text: str) -> list[dict]:
    """Objetos JSON de una salida JSONL (o de un unico objeto), sin romper por una linea sucia."""
    raw = (text or "").strip()
    if not raw:
        return []
    try:
        whole = json.loads(raw)
    except ValueError:
        whole = None
    if isinstance(whole, dict):
        return [whole]
    if isinstance(whole, list):
        return [o for o in whole if isinstance(o, dict)]
    out: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def _json_result(text: str) -> dict | None:
    """El objeto `type=result` de claude (o el ultimo JSON de la salida)."""
    objects = _json_objects(text)
    for obj in reversed(objects):
        if obj.get("type") == "result":
            return obj
    for obj in reversed(objects):
        if "result" in obj or "is_error" in obj:
            return obj
    return objects[-1] if objects else None


def _usage(inp: int = 0, out: int = 0, cost: float = 0.0) -> dict[str, Any]:
    u: dict[str, Any] = {"prompt_tokens": int(inp or 0), "completion_tokens": int(out or 0)}
    u["total_tokens"] = u["prompt_tokens"] + u["completion_tokens"]
    if cost:
        u["cost_usd"] = float(cost)
    return u


def _fail(flavor: str, detail: str, rc: int, out: str, err: str, argv: list[str]):
    return CLIProviderError(f"{flavor}: {detail}", command=argv, returncode=rc,
                            stderr=err, stdout=out)


# --- claude: un unico objeto {"type":"result", "result": "...", "is_error": bool}
def _args_claude(prompt: str, model: str, cwd: Path, opts: dict) -> tuple[list[str], dict]:
    args = ["-p", prompt, "--output-format", "json"]
    if model:
        args += ["--model", model]
    # Consultor, no ejecutor: plan = investiga y responde, no edita archivos.
    mode = opts.get("permission_mode", "plan")
    if mode:
        args += ["--permission-mode", str(mode)]
    return args + [str(a) for a in (opts.get("extra_args") or [])], {}


def _parse_claude(rc: int, out: str, err: str, meta: dict, argv: list[str]) -> tuple[str, dict]:
    data = _json_result(out)
    if data is None:
        raise _fail("claude", "no devolvio JSON", rc, out, err, argv)
    text = str(data.get("result") or "").strip()
    usage = data.get("usage") or {}
    tokens = _usage(usage.get("input_tokens"), usage.get("output_tokens"),
                    data.get("total_cost_usd") or 0.0)
    if data.get("is_error"):
        raise CLIProviderError(f"claude: {text or 'error sin detalle'}",
                               command=argv, returncode=rc, stdout=out, stderr=err)
    if not text:
        raise _fail("claude", "respuesta vacia", rc, out, err, argv)
    return text, tokens


# --- codex exec: JSONL de eventos + el texto final en `-o archivo`
def _args_codex(prompt: str, model: str, cwd: Path, opts: dict) -> tuple[list[str], dict]:
    out_file = Path(tempfile.mkdtemp(prefix="nexus-codex-")) / "last.txt"
    args = ["exec", prompt, "--json", "--skip-git-repo-check", "-C", str(cwd),
            "-o", str(out_file)]
    if model:
        args += ["-m", model]
    sandbox = opts.get("sandbox", "read-only")
    if sandbox:
        args += ["-s", str(sandbox)]
    args += [str(a) for a in (opts.get("extra_args") or [])]
    return args, {"out_file": out_file}


def _parse_codex(rc: int, out: str, err: str, meta: dict, argv: list[str]) -> tuple[str, dict]:
    objects = _json_objects(out)
    for obj in objects:
        if obj.get("type") in ("turn.failed", "error"):
            detail = obj.get("message") or (obj.get("error") or {}).get("message") or "turno fallido"
            raise _fail("codex", str(detail), rc, out, err, argv)
    text = ""
    out_file = meta.get("out_file")
    if out_file and Path(out_file).exists():
        try:
            text = Path(out_file).read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            text = ""
    if not text:
        for obj in objects:
            if obj.get("type") != "item.completed":
                continue
            item = obj.get("item") or {}
            if item.get("type") in ("agent_message", "assistant_message") and item.get("text"):
                text = str(item["text"]).strip()
    usage = {}
    for obj in objects:
        if obj.get("type") == "turn.completed" and isinstance(obj.get("usage"), dict):
            u = obj["usage"]
            usage = _usage(u.get("input_tokens"), u.get("output_tokens"))
    if not text:
        raise _fail("codex", "respuesta vacia", rc, out, err, argv)
    return text, usage


# --- opencode run: JSONL con parts de texto y tokens en step_finish
def _args_opencode(prompt: str, model: str, cwd: Path, opts: dict) -> tuple[list[str], dict]:
    args = ["run", prompt, "--format", "json", "--dir", str(cwd)]
    if model:
        args += ["-m", model]
    return args + [str(a) for a in (opts.get("extra_args") or [])], {}


def _parse_opencode(rc: int, out: str, err: str, meta: dict, argv: list[str]) -> tuple[str, dict]:
    objects = _json_objects(out)
    chunks: list[str] = []
    usage: dict = {}
    for obj in objects:
        kind = obj.get("type")
        if kind == "error":
            detail = obj.get("message") or ((obj.get("error") or {}).get("message")) or "error"
            raise _fail("opencode", str(detail), rc, out, err, argv)
        part = obj.get("part") or {}
        if kind == "text" and part.get("text"):
            chunks.append(str(part["text"]))
        elif kind == "step_finish" and isinstance(part.get("tokens"), dict):
            t = part["tokens"]
            usage = _usage(t.get("input"), t.get("output"), part.get("cost") or 0.0)
    text = "".join(chunks).strip()
    if not text:
        raise _fail("opencode", "respuesta vacia", rc, out, err, argv)
    return text, usage


# --- custom: argv propio y stdout crudo
def _args_custom(prompt: str, model: str, cwd: Path, opts: dict) -> tuple[list[str], dict]:
    template = opts.get("argv") or []
    if not isinstance(template, (list, tuple)) or not template:
        raise CLIProviderError("flavor custom necesita providers.cli.<name>.argv")
    args = [str(a).replace("{prompt}", prompt).replace("{model}", model or "")
            .replace("{cwd}", str(cwd)) for a in template]
    return args, {}


def _parse_custom(rc: int, out: str, err: str, meta: dict, argv: list[str]) -> tuple[str, dict]:
    text = (out or "").strip()
    if not text:
        raise _fail("custom", "respuesta vacia", rc, out, err, argv)
    return text, {}


FLAVORS = {
    "claude": (_args_claude, _parse_claude),
    "codex": (_args_codex, _parse_codex),
    "opencode": (_args_opencode, _parse_opencode),
    "custom": (_args_custom, _parse_custom),
}
class CLIProvider(BaseProvider):
    """Un CLI de agentes como proveedor de chat (sin tools).

    config (`config.yaml`):

        providers:
          cli:
            claude:
              enabled: true
              model: claude-sonnet-4-5        # opcional: se pasa con --model
              timeout_s: 180
              cwd: .                          # default: directorio descartable
    """

    def __init__(self, name: str, spec: dict | None = None):
        spec = dict(spec or {})
        self.name = str(name or "cli")
        self.flavor = str(spec.get("flavor") or self.name).lower()
        if self.flavor not in FLAVORS:
            raise CLIProviderError(
                f"flavor {self.flavor!r} desconocido (hay: {', '.join(sorted(FLAVORS))})")
        self._build, self._parse = FLAVORS[self.flavor]
        self.model = str(spec.get("model") or "")
        self.timeout_s = float(spec.get("timeout_s") or DEFAULT_TIMEOUT_S)
        self.opts = dict(spec)
        self._command = spec.get("command") or self.flavor
        self._prefix: list[str] | None = None
        self._prefix_error = ""
        self._env_overrides = {str(k): str(v) for k, v in (spec.get("env") or {}).items()}
        self._drop_env = tuple(str(k) for k in (spec.get("drop_env") or []))
        self.cwd = self._resolve_cwd(spec.get("cwd"))
        # Un CLI que acaba de fallar (sin login, sin cuota, proxy caido) no se
        # vuelve a elegir por un rato: reintentar en cada turno solo gasta tiempo.
        self._cooldown_s = float(spec.get("cooldown_s") or 300.0)
        self._cooldown_until = 0.0
        self.last_error = ""

    def _cool(self, reason: str) -> None:
        self.last_error = reason
        if self._cooldown_s > 0:
            self._cooldown_until = time.monotonic() + self._cooldown_s

    @property
    def cooling_down(self) -> bool:
        return time.monotonic() < self._cooldown_until

    # ---- resolucion ----
    def _resolve_cwd(self, value: Any) -> Path:
        if value:
            p = Path(str(value)).expanduser()
            if not p.is_absolute():
                p = Path(__file__).resolve().parents[2] / p
            p.mkdir(parents=True, exist_ok=True)
            return p
        # Sin cwd explicito: un directorio descartable. Un CLI con su propio
        # agente no deberia arrancar apuntando al repo por accidente.
        base = Path(tempfile.gettempdir()) / "nexus-v3-cli"
        p = base / self.name
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def argv_prefix(self) -> list[str]:
        if self._prefix is None and not self._prefix_error:
            try:
                self._prefix = resolve_command(self._command)
            except CLIProviderError as e:
                self._prefix_error = str(e)
                self._prefix = []
        if self._prefix:
            return self._prefix
        raise CLIProviderError(self._prefix_error or f"{self.name}: comando no disponible")

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env.update(_BASE_ENV)
        # Credenciales y proxy del usuario: se heredan salvo que se pidan fuera.
        for key in self._drop_env:
            env.pop(key, None)
        env.update(self._env_overrides)
        return env

    def _preflight(self, env: dict[str, str]) -> None:
        """Corta antes de lanzar si el CLI va a quedarse colgado."""
        if self.flavor != "claude":
            return
        problem = check_base_url(env.get("ANTHROPIC_BASE_URL", ""))
        if problem:
            raise CLIProviderError(f"claude: {problem}")

    def _model_arg(self, model: str | None) -> str:
        m = str(model or "").strip()
        if not m or m == self.name:
            return self.model
        if m.startswith(f"{self.name}:"):
            return m.split(":", 1)[1]
        return m

    def model_ids(self) -> list[str]:
        """Ids con los que el router publica este CLI."""
        return [f"{self.name}:{self.model}" if self.model else self.name]

    # ---- contrato BaseProvider ----
    async def chat(self, messages: list[LLMMessage], tools: list[dict] | None = None,
                   model: str | None = None, **kw: Any) -> LLMResponse:
        if tools:
            raise CLIProviderError(
                f"{self.name} no soporta tool-calling ({len(tools)} tools pedidas): "
                f"usalo en un turno sin herramientas")
        if self.cooling_down:
            raise CLIProviderError(
                f"{self.name} en enfriamiento por un fallo previo: {self.last_error}")
        env = self._env()
        try:
            self._preflight(env)
            system, user = flatten_messages(messages)
            prompt = trim_prompt(f"{system}\n\n{user}".strip() if system else user)
            if not prompt:
                raise CLIProviderError(f"{self.name}: prompt vacio")
            model_arg = self._model_arg(model if model is not None else kw.get("model"))
            args, meta = self._build(prompt, model_arg, self.cwd, self.opts)
            rc, out, err = await run_cli(self.argv_prefix, args, timeout_s=self.timeout_s,
                                         env=env, cwd=self.cwd)
            text, usage = self._parse(rc, out, err, meta, [*self.argv_prefix, *args])
        except CLIProviderError as e:
            self._cool(str(e)[:300])
            raise
        self.last_error = ""
        self._cooldown_until = 0.0
        usage = dict(usage or {})
        usage.setdefault("provider", self.name)
        return LLMResponse(content=text, model=model_arg or self.name, finish_reason="stop",
                           usage=usage)

    async def health(self) -> bool:
        """Barato: que el comando exista y no este en enfriamiento.

        Correr `--version` aca rompe el probe global (2,5 s para todos).
        """
        if self.cooling_down:
            return False
        try:
            prefix = self.argv_prefix
        except CLIProviderError:
            return False
        head = prefix[0] if prefix else ""
        if not head:
            return False
        if os.path.isabs(head) or os.sep in head or "/" in head:
            return Path(head).exists()
        return bool(shutil.which(head))

    async def probe(self) -> dict[str, Any]:
        """Version real del CLI (para diagnostico, no para health)."""
        try:
            rc, out, err = await run_cli(self.argv_prefix, ["--version"],
                                         timeout_s=20.0, env=self._env(), cwd=self.cwd)
            line = (out or err or "").strip().splitlines()
            return {"ok": rc == 0, "version": line[0] if line else "", "exit": rc}
        except CLIProviderError as e:
            return {"ok": False, "error": str(e)}

    async def list_models(self) -> list[str]:
        return self.model_ids()

    async def supports_tools(self, model: str) -> bool:
        return False

    async def supports_vision(self, model: str) -> bool:
        return False

    async def close(self) -> None:
        return None



