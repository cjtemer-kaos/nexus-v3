"""Mesa redonda: varias voces independientes y un resumen de consenso.

Un solo modelo contestando un problema de diseño tiende a converger a la primera
idea que se le ocurre. La mesa redonda hace lo contrario:

  1. sienta N voces (gemas con su propio system prompt) sobre el MISMO problema,
     cada una sin ver lo que dicen las otras;
  2. junta las respuestas en paralelo, con tope de tiempo y sin tools: las voces
     solo piensan, no tocan la maquina;
  3. un juez lee todo y devuelve que se acuerdo, donde discrepan y una sintesis.

Lo que la mesa NO hace es votar: un desacuerdo no se resuelve por mayoria, se
muestra. Una respuesta floja se descarta (`ok=False`) y sigue el resto.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)

# Voces por defecto: cada una mira el problema desde un angulo distinto. Si la
# gema no existe en el catalogo, se usa solo el foco (nunca falla por eso).
DEFAULT_SEATS: tuple[tuple[str, str], ...] = (
    ("architect", "Diseño y trade-offs de arquitectura: límites, acoplamiento, lo que se rompe a escala."),
    ("code", "Implementación concreta: qué código hace falta, qué se puede hacer hoy con lo que hay."),
    ("security", "Riesgos, permisos, datos expuestos y qué pasa cuando algo falla."),
    ("tester", "Cómo se verifica: qué prueba demuestra que funciona y qué caso queda sin cubrir."),
)

_SEAT_SYSTEM = (
    "Respondés como {name} en una mesa redonda técnica.\n"
    "Tu ángulo: {focus}\n"
    "Reglas: máximo 220 palabras, concreto, sin repetir el enunciado. "
    "Si algo no lo sabés, decilo. No propongas ejecutar comandos ni tocar archivos: "
    "en la mesa solo se razona."
)

_JUDGE_SYSTEM = (
    "Sos el secretario de una mesa redonda técnica. Recibís la pregunta y las "
    "respuestas de cada voz, y tenés que devolver SOLO un JSON válido con esta forma:\n"
    '{{"consensus": ["..."], "disagreements": [{{"topic": "...", "positions": ["...", "..."]}}], '
    '"final": "...", "confidence": 0.0}}\n'
    "Reglas: `consensus` son los puntos en los que TODAS las voces coinciden (lista vacía si no hay); "
    "`disagreements` solo desacuerdos reales, no matices; `final` es la respuesta recomendada en "
    "máximo 200 palabras, nombrando el desacuerdo si sigue abierto; `confidence` es 0..1 y mide "
    "cuánto sostienen las voces la conclusión, no cuánto te gusta. "
    "No inventes voces que no contestaron."
)


@dataclass
class SeatResult:
    seat: str
    model: str
    ok: bool = False
    content: str = ""
    error: str = ""
    ms: int = 0
    provider: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "seat": self.seat,
            "model": self.model,
            "provider": self.provider,
            "ok": self.ok,
            "answer": self.content,
            "error": self.error,
            "ms": self.ms,
        }


@dataclass
class RoundTableResult:
    task: str
    seats: list[SeatResult] = field(default_factory=list)
    consensus: list[str] = field(default_factory=list)
    disagreements: list[dict[str, Any]] = field(default_factory=list)
    final: str = ""
    confidence: Optional[float] = None
    overlap: Optional[float] = None
    judge_model: str = ""
    judge_ok: bool = False
    judge_error: str = ""
    ms: int = 0

    @property
    def answered(self) -> int:
        return sum(1 for s in self.seats if s.ok)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "seats": [s.to_dict() for s in self.seats],
            "answered": self.answered,
            "consensus": self.consensus,
            "disagreements": self.disagreements,
            "final": self.final,
            "confidence": self.confidence,
            "overlap": self.overlap,
            "judge_model": self.judge_model,
            "judge_ok": self.judge_ok,
            "judge_error": self.judge_error,
            "ms": self.ms,
        }


_TERM = re.compile(r"[0-9a-z\u00e1\u00e9\u00ed\u00f3\u00fa\u00f1_]{4,}", re.I)
_STOP = frozenset({
    "para", "como", "este", "esta", "esto", "esos", "esas", "porque", "cuando", "donde",
    "sobre", "entre", "desde", "hasta", "tiene", "tienen", "puede", "pueden", "hacer",
    "hace", "sino", "solo", "sólo", "todo", "toda", "todos", "todas", "otro", "otra",
    "mas", "más", "pero", "cada", "sera", "será", "esta", "está", "estan", "están",
    "aunque", "mismo", "misma", "parte", "forma", "caso", "cosa", "algo", "algun",
    "alguno", "ningun", "ninguno", "debe", "deben", "habria", "habría", "seria",
    "sería", "tambien", "también", "ademas", "además", "entonces", "mientras",
    "with", "that", "this", "from", "have", "will", "their", "there", "which",
})


def _terms(text: str) -> set[str]:
    norm = (text or "").lower()
    return {t for t in _TERM.findall(norm) if t not in _STOP}


def lexical_overlap(answers: Iterable[str]) -> Optional[float]:
    """Cuánto comparten las voces, en 0..1 (Jaccard medio por pares).

    Es una señal barata y determinista, no un veredicto: dos respuestas largas y
    correctas pueden no compartir casi nada de vocabulario.
    """
    sets = [_terms(a) for a in answers if (a or "").strip()]
    sets = [s for s in sets if s]
    if len(sets) < 2:
        return None
    total = 0.0
    pairs = 0
    for i in range(len(sets)):
        for j in range(i + 1, len(sets)):
            union = sets[i] | sets[j]
            total += (len(sets[i] & sets[j]) / len(union)) if union else 0.0
            pairs += 1
    return round(total / pairs, 3) if pairs else None


def _parse_judge_json(text: str) -> Optional[dict[str, Any]]:
    """Primer objeto JSON del texto del juez (tolera ```json ... ``` y prosa)."""
    if not text:
        return None
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    candidates = [fence.group(1)] if fence else []
    start = text.find("{")
    if start >= 0:
        depth = 0
        for i, ch in enumerate(text[start:], start=start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start:i + 1])
                    break
    for raw in candidates:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _seat_system(name: str, focus: str) -> str:
    return _SEAT_SYSTEM.format(name=name, focus=focus)


def _clean(text: str, limit: int = 4000) -> str:
    out = (text or "").strip()
    return out[:limit]


def _excerpt(text: str, limit: int = 1500) -> str:
    """Recorte para el juez: con 4 voces, el prompt se le va en citas."""
    out = (text or "").strip()
    if len(out) <= limit:
        return out
    return out[:limit].rstrip() + " […recortado]"


async def _ask_seat(router, seat: str, focus: str, model: str, task: str,
                    *, timeout_s: float, temperature: float) -> SeatResult:
    from src.core.provider_base import LLMMessage

    started = time.monotonic()
    res = SeatResult(seat=seat, model=model or "auto")
    try:
        system = _seat_system(seat, focus)
        try:
            from src.core.gema_profiles import system_prompt_for_gema

            extra = system_prompt_for_gema(seat)
            if extra:
                system = f"{system}\n\n{extra}"
        except Exception:
            pass
        resp = await asyncio.wait_for(
            router.chat(
                [LLMMessage(role="system", content=system),
                 LLMMessage(role="user", content=task)],
                model=model or None,
                hint=None,
                temperature=temperature,
            ),
            timeout=max(5.0, float(timeout_s)),
        )
        content = _clean(getattr(resp, "content", "") or "")
        res.model = getattr(resp, "model", "") or res.model
        res.provider = str((getattr(resp, "usage", None) or {}).get("provider") or "")
        if content:
            res.ok = True
            res.content = content
        else:
            res.error = "respuesta vacía"
    except asyncio.TimeoutError:
        res.error = f"timeout ({int(timeout_s)}s)"
    except Exception as e:
        res.error = f"{type(e).__name__}: {e}"
    finally:
        res.ms = int((time.monotonic() - started) * 1000)
    return res


def _pick_models(router, count: int) -> list[str]:
    """Modelos distintos, de proveedores distintos si se puede, sin embeddings."""
    from src.core.provider_router import is_chat_model_id

    try:
        routes = list(router.list_models())
    except Exception:
        return []
    routes.sort(key=lambda r: (r.effective_tier(), r.provider_name))
    seen: set[str] = set()
    per_provider: dict[str, int] = {}
    out: list[str] = []
    for route in routes:
        mid = str(getattr(route, "id", "") or "")
        if not mid or mid in seen:
            continue
        if not is_chat_model_id(mid):
            continue
        provider = str(getattr(route, "provider_name", "") or "")
        if per_provider.get(provider, 0) >= 2:
            continue
        seen.add(mid)
        per_provider[provider] = per_provider.get(provider, 0) + 1
        out.append(mid)
        if len(out) >= count:
            break
    return out


async def convene(
    task: str,
    *,
    router,
    seats: Optional[list[Any]] = None,
    models: Optional[list[str]] = None,
    timeout_s: float = 90.0,
    judge_timeout_s: Optional[float] = None,
    temperature: float = 0.4,
    judge_model: str | None = None,
) -> RoundTableResult:
    """Sienta las voces, las escucha y pide el resumen de consenso.

    `seats` acepta strings ("code") o dicts {"seat": "code", "focus": "..."}.
    `models` fija un modelo por silla; sin `models`, cada silla usa el mejor
    modelo disponible para su perfil y el router decide.
    """
    started = time.monotonic()
    text = _clean(task, 12000)
    result = RoundTableResult(task=text)
    if not text:
        result.judge_error = "tarea vacía"
        return result

    specs: list[tuple[str, str]] = []
    for item in (seats or DEFAULT_SEATS):
        if isinstance(item, dict):
            name = str(item.get("seat") or item.get("name") or "").strip()
            focus = str(item.get("focus") or "").strip()
        elif isinstance(item, (tuple, list)) and item:
            name = str(item[0] or "").strip()
            focus = str(item[1] or "").strip() if len(item) > 1 else ""
        else:
            name, focus = str(item), ""
        if not name and not focus:
            continue
        if not focus:
            focus = dict(DEFAULT_SEATS).get(name, "Tu especialidad.")
        specs.append((name or "voz", focus))
    if not specs:
        specs = list(DEFAULT_SEATS)

    chosen = [str(m) for m in (models or []) if str(m).strip()]
    if not chosen:
        chosen = _pick_models(router, len(specs))
    # Sin modelos detectados: el router resuelve por su cuenta en cada silla.
    while len(chosen) < len(specs):
        chosen.append("")

    tasks = [
        _ask_seat(router, name, focus, chosen[i], text,
                  timeout_s=timeout_s, temperature=temperature)
        for i, (name, focus) in enumerate(specs)
    ]
    result.seats = list(await asyncio.gather(*tasks))
    answered = [s for s in result.seats if s.ok]
    result.overlap = lexical_overlap([s.content for s in answered])
    if not answered:
        result.ms = int((time.monotonic() - started) * 1000)
        result.judge_error = "ninguna voz respondió"
        return result

    from src.core.provider_base import LLMMessage

    transcript = "\n\n".join(
        f"### {s.seat} ({s.model})\n{_excerpt(s.content)}" for s in answered
    )
    judge_prompt = f"PREGUNTA:\n{text}\n\nRESPUESTAS:\n{transcript}"
    # El juez recibe un prompt mas grande que una silla y suele caer en un modelo
    # local: necesita su propio tope, mas largo, o se pierde la sintesis.
    _judge_to = float(judge_timeout_s) if judge_timeout_s else max(60.0, float(timeout_s) * 1.5)
    try:
        jr = await asyncio.wait_for(
            router.chat(
                [LLMMessage(role="system", content=_JUDGE_SYSTEM),
                 LLMMessage(role="user", content=judge_prompt)],
                model=judge_model or None,
                hint="reasoning",
                temperature=0.2,
            ),
            timeout=_judge_to,
        )
        result.judge_model = getattr(jr, "model", "") or (judge_model or "")
        parsed = _parse_judge_json(getattr(jr, "content", "") or "")
        if parsed:
            result.judge_ok = True
            consensus = parsed.get("consensus") or []
            result.consensus = [str(c) for c in consensus if str(c).strip()][:12] if isinstance(consensus, list) else []
            disagreements = parsed.get("disagreements") or []
            result.disagreements = [
                {"topic": str(d.get("topic") or ""), "positions": [str(p) for p in (d.get("positions") or [])]}
                for d in disagreements if isinstance(d, dict)
            ][:8] if isinstance(disagreements, list) else []
            result.final = _clean(str(parsed.get("final") or ""), 6000)
            try:
                conf = float(parsed.get("confidence"))
                result.confidence = round(min(1.0, max(0.0, conf)), 3)
            except (TypeError, ValueError):
                result.confidence = None
        else:
            # El juez no devolvió JSON: se guarda su texto tal cual, sin inventar
            # un consenso que nadie declaró.
            result.judge_error = "el juez no devolvió JSON"
            result.final = _clean(getattr(jr, "content", "") or "", 6000)
    except asyncio.TimeoutError:
        result.judge_error = f"timeout del juez ({int(timeout_s)}s)"
    except Exception as e:
        result.judge_error = f"{type(e).__name__}: {e}"

    if not result.final:
        # El juez dio consenso pero no redactó la síntesis: se arma con lo que
        # él mismo declaró, sin inventar nada nuevo.
        parts: list[str] = []
        if result.consensus:
            parts.append("Consenso:\n" + "\n".join(f"- {c}" for c in result.consensus))
        for d in result.disagreements:
            topic = str(d.get("topic") or "").strip()
            positions = [str(p).strip() for p in (d.get("positions") or []) if str(p).strip()]
            if topic or positions:
                parts.append(f"Desacuerdo abierto — {topic or 'sin título'}:\n"
                             + "\n".join(f"- {p}" for p in positions))
        if parts:
            result.final = "\n\n".join(parts)
            result.judge_error = result.judge_error or "el juez no redactó la síntesis: se armó con su consenso"
        else:
            best = max(answered, key=lambda s: len(s.content))
            result.final = best.content
            result.judge_error = result.judge_error or "sin síntesis: se devuelve la respuesta más completa"

    result.ms = int((time.monotonic() - started) * 1000)
    return result
