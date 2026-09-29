"""
memory_extractor — Extracción automática de hechos duraderos de conversaciones.

Patrón (inspirado en Odysseus): después de cada respuesta del LLM, extraemos
hechos personales relevantes del usuario y los almacenamos en memoria.

Flujo:
    1. after_response() se llama post-respuesta del LLM
    2. Extrae últimos N mensajes de la conversación
    3. LLM extrae máx 2 hechos duraderos (o fallback regex)
    4. Dedup via similitud de texto (Jaccard)
    5. Auto-pin hechos de identidad (nombre, ubicación)
    6. Trigger auditoría cada N extracciones

Diferenciación con Odysseus:
    - Usa el LLM del Director (no necesita endpoint separado)
    - Integra con el brain_consolidator existente
    - Fallback regex más robusto
    - Fingerprinting para saltar auditorías innecesarias
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import time
from typing import Dict, List, Optional

logger = logging.getLogger("nexus-memory-extractor")

# ── Configuración ────────────────────────────────────────────────────────────

CONTEXT_WINDOW = 6  # mensajes recientes a analizar
MAX_FACTS_PER_EXTRACT = 2  # máximo hechos por extracción
MIN_FACT_LEN = 10  # largo mínimo de un hecho
MAX_FACT_LEN = 120  # largo máximo
AUDIT_INTERVAL = 5  # trigger auditoría cada N extracciones
DEDUP_THRESHOLD = 0.4  # Jaccard similarity para dedup

# ── Prompts ──────────────────────────────────────────────────────────────────

EXTRACT_SYSTEM_PROMPT = (
    "Eres un asistente de extracción de memoria. Analiza la conversación y extrae "
    "SOLO hechos personales duraderos del usuario que serían útiles en futuras conversaciones.\n\n"
    "Buenos ejemplos: nombre, título profesional, ciudad, familiares, proyectos a largo plazo, "
    "preferencias fuertes.\n"
    "Malos ejemplos: lo que preguntó hoy, estados de ánimo temporales, declaraciones genéricas, "
    "lo que el asistente dijo, tareas de una sola vez, opiniones sobre el tema actual.\n\n"
    "Reglas:\n"
    "- MÁX 2 hechos por conversación — solo los más importantes\n"
    "- Solo extrae hechos que el USUARIO declaró o implicó claramente\n"
    "- Cada hecho debe ser una oración corta (menos de 15 palabras)\n"
    "- Si un hecho es similar a algo probablemente ya conocido, omítelo\n"
    "- Si no se reveló nada duradero, retorna []\n\n"
    "Retorna un array JSON de objetos con campos 'text' y 'category'.\n"
    "Categorías: 'identity', 'preference', 'fact', 'contact', 'project', 'goal'\n\n"
    "Retorna SOLO JSON válido, sin fences de markdown."
)

AUDIT_SYSTEM_PROMPT = (
    "Eres un curador de base de datos de memoria. Sé CONSERVATIVO: elimina solo "
    "duplicados VERDADEROS y entradas claramente inútiles. Cada hecho distinto debe sobrevivir.\n"
    "En caso de duda, MANTÉN la entrada.\n\n"
    "Reglas:\n"
    "1. FUSIONA solo entradas que declaren el MISMO hecho con diferentes palabras.\n"
    "2. ELIMINA solo entradas genuinamente inútiles (sobre lo que la IA hizo, vacías, sin sentido).\n"
    "3. Mantén la redacción original. Solo recorta redundancia obvia.\n"
    "4. Preserva el 'id' de la entrada que mantienes al fusionar.\n"
    "5. Nunca inventes hechos. En caso de duda, MANTÉN.\n\n"
    "Retorna un array JSON de objetos con campos: id, text, category.\n"
    "Retorna SOLO JSON válido, sin fences de markdown."
)

# ── Helpers ──────────────────────────────────────────────────────────────────

def _tokenize(text: str) -> set:
    """Tokeniza y limpia texto para comparación."""
    return {w.strip('.,!?";:()[]') for w in (text or "").lower().split() if len(w) > 1}


def _jaccard(a: set, b: set) -> float:
    """Similitud Jaccard entre dos sets de tokens."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _fingerprint(entries: List[Dict]) -> str:
    """Hash estable del estado de memorias (order-independent)."""
    items = sorted(
        (str(e.get("id", "")), e.get("text", ""), e.get("category", ""))
        for e in entries if isinstance(e, dict)
    )
    h = hashlib.sha256()
    for triple in items:
        h.update(("\x1f".join(triple) + "\x1e").encode("utf-8"))
    return h.hexdigest()


def _is_duplicate(new_text: str, existing: List[Dict], threshold: float = DEDUP_THRESHOLD) -> bool:
    """Verifica si un texto es duplicado de alguna memoria existente."""
    new_tokens = _tokenize(new_text)
    if not new_tokens:
        return False
    for entry in existing:
        old_tokens = _tokenize(entry.get("text", ""))
        if old_tokens and _jaccard(new_tokens, old_tokens) >= threshold:
            return True
    return False


def _is_identity_fact(text: str) -> bool:
    """Detecta si un hecho es de identidad (nombre, ubicación, etc.)."""
    text_lower = text.lower()
    identity_patterns = [
        r'\b[A-Z][a-z]+ [A-Z][a-z]+\b',  # Nombre propio
        r'(?:name is|called|named|my name)',
        r'(?:live in|from|located in)',
        r'(?:work at|employed at|job at)',
    ]
    return any(re.search(p, text, re.I) for p in identity_patterns)


def _clean_fact(value: str, max_len: int = MAX_FACT_LEN) -> str:
    """Limpia y valida un texto de hecho."""
    value = re.sub(r"\s+", " ", value or "").strip(" .,!?:;\"'`""''")
    value = re.sub(r"^(?:the|a|an)\s+", "", value, flags=re.I)
    if not value or len(value) > max_len or len(value) < MIN_FACT_LEN:
        return ""
    if re.search(r"https?://|@|[{}<>]", value):
        return ""
    return value


# ── Fallback Regex (sin LLM) ────────────────────────────────────────────────

def _fallback_candidates(messages: List[Dict]) -> List[Dict]:
    """Extrae hechos obvios sin LLM — patrones regex para identidad/preferencias."""
    candidates = []
    seen = set()

    def add(text: str, category: str):
        text = _clean_fact(text, 120)
        if not text:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        candidates.append({"text": text, "category": category})

    for msg in messages:
        role = msg.get("role", "")
        if role != "user":
            continue
        text = msg.get("content", "")
        if isinstance(text, list):
            text = " ".join(str(p.get("text", "") if isinstance(p, dict) else p) for p in text)
        if not text:
            continue

        # Nombre
        m = re.search(r"\bmy name is\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", text, re.I)
        if m:
            add(f"Nombre del usuario: {m.group(1).strip()}.", "identity")

        m = re.search(r"\bcall me\s+([A-Za-z][A-Za-z0-9 .'\-]{1,50})\b", text, re.I)
        if m:
            add(f"Usuario quiere que lo llamen {m.group(1).strip()}.", "identity")

        # Ubicación
        m = re.search(r"\bi (?:live in|am from|'m from)\s+([^.!?\n]{2,80})", text, re.I)
        if m:
            add(f"Usuario vive en {_clean_fact(m.group(1), 80)}.", "identity")

        # Preferencias
        m = re.search(r"\bi (prefer|like|love|hate|do not like|don't like)\s+([^.!?\n]{4,100})", text, re.I)
        if m:
            verb = m.group(1).lower()
            pref = _clean_fact(m.group(2), 100)
            if pref:
                if verb in ("hate", "do not like", "don't like"):
                    add(f"Usuario no le gusta {pref}.", "preference")
                else:
                    add(f"Usuario prefiere {pref}.", "preference")

        # Metas
        m = re.search(
            r"\bi (?:(?:want|would like|plan|hope) to|wanna) "
            r"(?:go|travel|move|visit) to\s+([^.!?\n]{2,80})",
            text, re.I,
        )
        if m:
            add(f"Usuario quiere visitar {_clean_fact(m.group(1), 80)}.", "goal")

    return candidates[:MAX_FACTS_PER_EXTRACT]


# ── MemoryExtractor ─────────────────────────────────────────────────────────

class MemoryExtractor:
    """
    Extracción automática de memoria — extrae hechos duraderos de conversaciones.

    Uso:
        extractor = MemoryExtractor(director)
        await extractor.after_response(session_messages)
    """

    def __init__(self, director=None, memory_consolidator=None):
        self.director = director
        self.consolidator = memory_consolidator
        self._extractions_since_audit = 0
        self._last_fingerprint = ""
        self._tidy_state_path = os.path.join(
            os.path.expanduser("~"), ".nexus", "brain", "memory_tidy_state.json"
        )

    async def after_response(
        self,
        messages: List[Dict],
        session_id: str = "",
        owner: str = "",
    ) -> Dict:
        """
        Extrae hechos de los últimos mensajes y los almacena.

        Args:
            messages: Lista de mensajes de la conversación [{role, content}]
            session_id: ID de sesión actual
            owner: Owner de las memorias

        Returns:
            Dict con estadísticas de la extracción
        """
        recent = messages[-CONTEXT_WINDOW:] if len(messages) > CONTEXT_WINDOW else messages
        if len(recent) < 2:
            return {"added": 0, "reason": "insufficient_messages"}

        # Fallback regex (sin LLM)
        fallback = _fallback_candidates(recent)

        # Intentar extracción vía LLM
        llm_facts = await self._llm_extract(recent)

        # Combinar resultados
        facts = llm_facts + fallback
        if not facts:
            return {"added": 0, "reason": "no_facts_extracted"}

        # Obtener memorias existuentes
        existing = self._load_memories(owner)
        added = 0

        for fact in facts:
            fact_text = fact.get("text", "") if isinstance(fact, dict) else str(fact)
            category = fact.get("category", "fact") if isinstance(fact, dict) else "fact"

            if not fact_text or len(fact_text) < MIN_FACT_LEN:
                continue

            # Dedup
            if _is_duplicate(fact_text, existing):
                continue

            # Crear entrada
            entry = {
                "id": hashlib.sha256(f"{fact_text}{time.time()}".encode()).hexdigest()[:12],
                "text": fact_text,
                "timestamp": int(time.time()),
                "source": "auto-extract",
                "category": category,
                "uses": 0,
                "session_id": session_id,
            }
            if owner:
                entry["owner"] = owner

            # Auto-pin identidad
            if category == "identity" or _is_identity_fact(fact_text):
                entry["pinned"] = True

            existing.append(entry)
            added += 1

        if added > 0:
            self._save_memories(existing)
            self._extractions_since_audit += added
            logger.info(f"[memory-extract] +{added} hechos extraídos")

            # Trigger auditoría
            if self._extractions_since_audit >= AUDIT_INTERVAL:
                self._extractions_since_audit = 0
                await self._audit(owner)

        return {"added": added, "total_facts": len(facts)}

    async def _llm_extract(self, messages: List[Dict]) -> List[Dict]:
        """Extrae hechos vía LLM del Director."""
        if not self.director:
            return []

        try:
            # Construir prompt de extracción
            extraction_messages = [
                {"role": "system", "content": EXTRACT_SYSTEM_PROMPT},
            ] + messages

            # Usar el provider del Director
            provider = None
            if hasattr(self.director, 'provider_registry'):
                provider = self.director.provider_registry.get("gema-con-fallback")

            if not provider:
                return []

            from src.core.provider_base import LLMMessage
            from src.core.agent_runner import AgentRunner, AgentRunSpec

            llm_messages = [LLMMessage(role=m["role"], content=m["content"]) for m in extraction_messages]
            runner = AgentRunner(provider)
            spec = AgentRunSpec(
                messages=llm_messages,
                tools_definitions=[],
                max_iterations=1,
                max_tokens=500,
                temperature=0.1,
            )
            result = await runner.run(spec)

            if result.stop_reason == "error":
                return []

            # Parsear JSON
            text = (result.content or "").strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

            try:
                facts = json.loads(text)
                return facts if isinstance(facts, list) else []
            except json.JSONDecodeError:
                return []

        except Exception as e:
            logger.debug(f"[memory-extract] LLM extraction failed: {e}")
            return []

    def _load_memories(self, owner: str = "") -> List[Dict]:
        """Carga memorias existentes."""
        if self.consolidator and hasattr(self.consolidator, 'search'):
            # Usar consolidator si está disponible
            try:
                all_mems = []
                # Esto es una simplificación — en producción se usaría la DB
                return all_mems
            except Exception:
                pass

        # Fallback: archivo JSON
        memory_file = os.path.join(os.path.expanduser("~"), ".nexus", "brain", "memories.json")
        if os.path.exists(memory_file):
            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if isinstance(data, list):
                        return [e for e in data if e.get("owner") == owner or not owner]
            except Exception:
                pass
        return []

    def _save_memories(self, memories: List[Dict]):
        """Guarda memorias al archivo JSON."""
        memory_file = os.path.join(os.path.expanduser("~"), ".nexus", "brain", "memories.json")
        os.makedirs(os.path.dirname(memory_file), exist_ok=True)

        tmp_file = memory_file + ".tmp"
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(memories, f, ensure_ascii=False, indent=2)
        os.replace(tmp_file, memory_file)

    async def _audit(self, owner: str = ""):
        """Auditoría de memoria — consolida duplicados y elimina basura."""
        try:
            existing = self._load_memories(owner)
            if not existing or len(existing) < 8:
                return

            # Fingerprint check
            current_fp = _fingerprint(existing)
            last_state = self._load_tidy_state()
            if last_state.get(owner, {}).get("fingerprint") == current_fp:
                logger.debug("[memory-audit] Estado sin cambios, saltando")
                return

            # Construir payload para LLM
            payload = [
                {"id": m["id"], "text": m["text"], "category": m.get("category", "fact")}
                for m in existing
            ]

            if not self.director:
                return

            provider = None
            if hasattr(self.director, 'provider_registry'):
                provider = self.director.provider_registry.get("gema-con-fallback")

            if not provider:
                return

            from src.core.provider_base import LLMMessage
            from src.core.agent_runner import AgentRunner, AgentRunSpec

            audit_messages = [
                LLMMessage(role="system", content=AUDIT_SYSTEM_PROMPT),
                LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False)),
            ]

            runner = AgentRunner(provider)
            spec = AgentRunSpec(
                messages=audit_messages,
                tools_definitions=[],
                max_iterations=1,
                max_tokens=4096,
                temperature=0.1,
            )
            result = await runner.run(spec)

            if result.stop_reason == "error":
                return

            # Parsear resultado
            text = (result.content or "").strip()
            text = re.sub(r'<think(?:ing)?>[\s\S]*?</think(?:ing)?>', '', text, flags=re.I).strip()

            cleaned = self._parse_json_list(text)
            if not cleaned:
                return

            # Safety net: no eliminar más del 50%
            if len(existing) >= 8 and len(cleaned) < len(existing) * 0.5:
                logger.warning(f"[memory-audit] Rechazado: {len(existing)} -> {len(cleaned)} (>50% eliminado)")
                return

            # Reconstruir con metadata original
            originals = {m["id"]: m for m in existing}
            final = []
            for item in cleaned:
                if not isinstance(item, dict):
                    continue
                mid = item.get("id", "")
                new_text = item.get("text", "").strip()
                if not new_text:
                    continue
                if mid in originals:
                    entry = originals[mid].copy()
                    entry["text"] = new_text
                    if item.get("category"):
                        entry["category"] = item["category"]
                    final.append(entry)

            # Guardar
            all_mems = self._load_memories()
            other = [e for e in all_mems if e.get("owner") != owner and e.get("owner")]
            self._save_memories(final + other)

            # Guardar fingerprint
            self._save_tidy_state(owner, _fingerprint(final))

            logger.info(f"[memory-audit] {len(existing)} -> {len(final)} entradas")

        except Exception as e:
            logger.error(f"[memory-audit] Error: {e}")

    def _parse_json_list(self, text: str) -> Optional[List]:
        """Parsea una lista JSON tolerando ruido de modelos de razonamiento."""
        if not text:
            return None
        # Intentar parseo directo
        for candidate in (text, re.sub(r',(\s*[}\]])', r'\1', text)):
            try:
                v = json.loads(candidate)
                if isinstance(v, list):
                    return v
            except Exception:
                continue
        # Buscar en markdown fences
        m = re.search(r'```(?:json)?\s*\n?([\s\S]*?)```', text)
        if m:
            try:
                v = json.loads(m.group(1).strip())
                if isinstance(v, list):
                    return v
            except Exception:
                pass
        # Buscar array en texto
        a, b = text.find('['), text.rfind(']')
        if a >= 0 and b > a:
            try:
                v = json.loads(text[a:b + 1])
                if isinstance(v, list):
                    return v
            except Exception:
                pass
        return None

    def _load_tidy_state(self) -> Dict:
        """Carga estado de auditoría previa."""
        try:
            if os.path.exists(self._tidy_state_path):
                with open(self._tidy_state_path, "r", encoding="utf-8") as f:
                    return json.load(f)
        except Exception:
            pass
        return {}

    def _save_tidy_state(self, owner: str, fingerprint: str):
        """Guarda estado de auditoría."""
        try:
            state = self._load_tidy_state()
            state[owner or ""] = {"fingerprint": fingerprint}
            os.makedirs(os.path.dirname(self._tidy_state_path), exist_ok=True)
            with open(self._tidy_state_path, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        except Exception as e:
            logger.debug(f"[memory-audit] No se pudo guardar tidy state: {e}")


# ── Instancia global ─────────────────────────────────────────────────────────

_extractor: Optional[MemoryExtractor] = None


def get_memory_extractor(director=None, consolidator=None) -> MemoryExtractor:
    """Obtiene o crea la instancia global del extractor."""
    global _extractor
    if _extractor is None:
        _extractor = MemoryExtractor(director=director, memory_consolidator=consolidator)
    elif director and not _extractor.director:
        _extractor.director = director
    return _extractor
