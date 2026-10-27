"""Preguntas al usuario en medio del turno (equivalente a `question` de opencode).

Un agente no puede inventar la decisión del usuario: cuando necesita elegir
entre caminos, deja la pregunta PENDIENTE y espera. La UI (vista Preguntas) o
un POST a la API la responde y el turno sigue con esa respuesta.

Diseño:
- `ask()` es async y espera un `asyncio.Event` con timeout. El server corre un
  solo loop, así que el POST que responde despierta el mismo event.
- Sin respuesta en `timeout` el turno NO se cuelga: devuelve `answered: false`
  con una instrucción ("seguí con la opción más razonable y decí qué asumiste").
- Con `options`, la respuesta tiene que ser una de ellas (se valida y se
  devuelve la lista en el error).
"""
from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Dict, List

MAX_OPCIONES = 12
MAX_PENDIENTES = 64
TIMEOUT_DEFAULT = 300.0
TIMEOUT_MIN = 5.0
TIMEOUT_MAX = 900.0


@dataclass
class Question:
    id: str
    session_id: str
    question: str
    header: str = ""
    options: List[str] = field(default_factory=list)
    multiple: bool = False
    created_at: float = field(default_factory=time.time)
    answered_at: float = 0.0
    answer: List[str] = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False

    def public(self) -> dict:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "question": self.question,
            "header": self.header,
            "options": list(self.options),
            "multiple": self.multiple,
            "created_at": self.created_at,
            "answered_at": self.answered_at,
            "answer": list(self.answer),
            "cancelled": self.cancelled,
            "timed_out": self.timed_out,
            "status": self.status,
        }

    @property
    def status(self) -> str:
        if self.answer:
            return "answered"
        if self.cancelled:
            return "cancelled"
        if self.timed_out:
            return "timed_out"
        return "pending"


def _limpiar_opciones(options: Any) -> List[str]:
    """Acepta lista o CSV y deduplica conservando el orden (los modelos chicos
    mandan 'A,B,C' aunque el schema pida array)."""
    if options is None:
        return []
    if isinstance(options, str):
        crudo = [p.strip() for p in options.split(",")]
    elif isinstance(options, (list, tuple, set)):
        crudo = [str(p).strip() for p in options]
    else:
        crudo = [str(options).strip()]
    out: List[str] = []
    for o in crudo:
        if o and o not in out:
            out.append(o[:200])
    return out[:MAX_OPCIONES]


class QuestionRegistry:
    def __init__(self) -> None:
        self._items: Dict[str, Question] = {}
        self._events: Dict[str, asyncio.Event] = {}
        self._loops: Dict[str, Any] = {}

    # -- estado ---------------------------------------------------------
    def get(self, qid: str) -> Question | None:
        return self._items.get(qid)

    def pending(self, session_id: str = "") -> List[dict]:
        vivas = [q for q in self._items.values()
                 if q.status == "pending" and (not session_id or q.session_id == session_id)]
        vivas.sort(key=lambda q: q.created_at)
        return [q.public() for q in vivas]

    def all(self, session_id: str = "") -> List[dict]:
        vivas = [q for q in self._items.values()
                 if not session_id or q.session_id == session_id]
        vivas.sort(key=lambda q: q.created_at)
        return [q.public() for q in vivas]

    def pending_count(self) -> int:
        return sum(1 for q in self._items.values() if q.status == "pending")

    # -- ciclo de vida --------------------------------------------------
    async def ask(self, session_id: str, question: str, options: Any = None,
                  multiple: bool = False, header: str = "",
                  timeout_s: float = TIMEOUT_DEFAULT) -> dict:
        texto = (question or "").strip()
        if not texto:
            raise ValueError("question: la pregunta está vacía")
        ops = _limpiar_opciones(options)
        limite = max(TIMEOUT_MIN, min(TIMEOUT_MAX, float(timeout_s or TIMEOUT_DEFAULT)))
        self._podar()
        rec = Question(id=f"q-{uuid.uuid4().hex[:10]}", session_id=session_id or "",
                       question=texto[:4000], header=(header or "").strip()[:120],
                       options=ops, multiple=bool(multiple))
        ev = asyncio.Event()
        self._items[rec.id] = rec
        self._events[rec.id] = ev
        try:
            self._loops[rec.id] = asyncio.get_running_loop()
        except RuntimeError:
            self._loops.pop(rec.id, None)
        try:
            await asyncio.wait_for(ev.wait(), timeout=limite)
        except asyncio.TimeoutError:
            rec.timed_out = True
        except asyncio.CancelledError:
            rec.cancelled = True
            raise
        finally:
            self._events.pop(rec.id, None)
            self._loops.pop(rec.id, None)
        return self.resumen(rec, limite)

    def _despertar(self, qid: str) -> None:
        """Despierta al turno que espera la respuesta.

        El que espera y el que responde pueden estar en loops distintos (el
        POST lo puede atender otro hilo/worker): `Event.set()` desde afuera del
        loop no lo despierta, hay que pasar por `call_soon_threadsafe`.
        """
        ev = self._events.get(qid)
        if ev is None:
            return
        loop = self._loops.get(qid)
        try:
            en_curso = asyncio.get_running_loop()
        except RuntimeError:
            en_curso = None
        if loop is None or loop is en_curso:
            ev.set()
            return
        try:
            loop.call_soon_threadsafe(ev.set)
        except RuntimeError:
            ev.set()

    def resumen(self, rec: Question, limite: float = TIMEOUT_DEFAULT) -> dict:
        if rec.answer:
            return {"ok": True, "answered": True, "id": rec.id,
                    "answer": list(rec.answer), "text": " / ".join(rec.answer)}
        if rec.cancelled:
            return {"ok": True, "answered": False, "cancelled": True, "id": rec.id,
                    "reason": "el usuario canceló la pregunta: no adivines, preguntá de otra "
                              "forma o seguí sin esa decisión"}
        return {"ok": True, "answered": False, "timed_out": True, "id": rec.id,
                "reason": (f"sin respuesta del usuario en {int(limite)}s. Seguí con la opción "
                           f"más razonable y decí explícitamente qué asumiste")}

    def answer(self, qid: str, value: Any) -> dict:
        rec = self._items.get(qid)
        if rec is None:
            vivas = ", ".join(q.id for q in self._items.values() if q.status == "pending")
            cola = f" Pendientes: {vivas}." if vivas else " No hay preguntas pendientes."
            return {"ok": False, "error": f"pregunta desconocida: {qid}.{cola}"}
        if rec.answer:
            return {"ok": False, "error": "esa pregunta ya estaba respondida"}
        if rec.cancelled or rec.timed_out:
            return {"ok": False, "error": f"la pregunta ya no está pendiente ({rec.status})"}
        vals = value if isinstance(value, (list, tuple)) else [value]
        vals = [str(v).strip() for v in vals if str(v).strip()]
        if not vals:
            return {"ok": False, "error": "la respuesta está vacía"}
        if rec.options:
            invalidas = [v for v in vals if v not in rec.options]
            if invalidas:
                return {"ok": False,
                        "error": f"opción inválida: {invalidas[0]!r}. "
                                 f"Válidas: {', '.join(repr(o) for o in rec.options)}"}
        if not rec.multiple:
            vals = vals[:1]
        rec.answer = vals
        rec.answered_at = time.time()
        self._despertar(qid)
        return {"ok": True, "id": qid, "answer": list(vals)}

    def cancel(self, qid: str) -> dict:
        rec = self._items.get(qid)
        if rec is None:
            return {"ok": False, "error": f"pregunta desconocida: {qid}"}
        if rec.status != "pending":
            return {"ok": False, "error": f"la pregunta ya no está pendiente ({rec.status})"}
        rec.cancelled = True
        self._despertar(qid)
        return {"ok": True, "id": qid, "status": rec.status}

    def clear(self) -> int:
        """Borra las resueltas (answered/cancelled/timed_out). Devuelve cuántas."""
        muertas = [k for k, q in self._items.items() if q.status != "pending"]
        for k in muertas:
            self._items.pop(k, None)
        return len(muertas)

    def _podar(self) -> None:
        if len(self._items) < MAX_PENDIENTES:
            return
        for k in [k for k, q in sorted(self._items.items(), key=lambda kv: kv[1].created_at)
                  if q.status != "pending"]:
            self._items.pop(k, None)
        # Si todas siguen pendientes, se descarta la más vieja (no se puede
        # esperar a que el usuario conteste para aceptar preguntas nuevas).
        while len(self._items) >= MAX_PENDIENTES:
            k = min(self._items, key=lambda kk: self._items[kk].created_at)
            self._items[k].timed_out = True
            self._despertar(k)
            self._events.pop(k, None)
            self._loops.pop(k, None)
            self._items.pop(k, None)


_REGISTRY: QuestionRegistry | None = None


def get_questions() -> QuestionRegistry:
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = QuestionRegistry()
    return _REGISTRY
