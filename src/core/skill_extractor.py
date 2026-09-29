"""
skill_extractor — Extracción automática de skills de sesiones complejas.

Patrón (inspirado en Odysseus): cuando el agente toma >=2 rondas o >=2 tool calls
para completar una tarea, extraemos el procedimiento como skill reutilizable.

Flujo:
    1. after_session() se llama al final de una sesión compleja
    2. Analiza la secuencia de tool calls y resultados
    3. LLM distila el procedimiento en un skill estructurado
    4. Guarda como SKILL.md con YAML frontmatter
    5. Actualiza contadores de uso

Diferenciación con Odysseus:
    - Usa el LLM del Director (no necesita endpoint separado)
    - Integra con ProgressiveSkillLoader existente
    - Formato SKILL.md compatible con el sistema de skills actual
    - Filtro de confianza mínimo (MIN_CONFIDENCE = 0.6)
"""
from __future__ import annotations

import json
import logging
import re
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger("nexus-skill-extractor")

# ── Configuración ────────────────────────────────────────────────────────────

MIN_ROUNDS = 2  # mínimo de rondas para considerar "compleja"
MIN_TOOL_CALLS = 2  # mínimo de tool calls
MIN_CONFIDENCE = 0.6  # confianza mínima para guardar el skill
CONTEXT_WINDOW = 12  # mensajes a incluir en el prompt
SKILLS_DIR = Path.home() / ".nexus" / "skills" / "auto-extracted"

# ── Prompt de Extracción ─────────────────────────────────────────────────────

SKILL_EXTRACT_PROMPT = (
    "Estás analizando una sesión de trabajo de un agente de IA. El agente tomó "
    "{rounds} rondas y {tool_count} tool calls para completar la tarea.\n\n"
    "Extrae un 'skill' reutilizable SOLO si la sesión contiene un procedimiento "
    "concreto y repetible que el agente pueda seguir para resolver un problema "
    "similar EN LA COMPUTADORA la próxima vez (ej. secuencia de comandos shell, "
    "código, ediciones de archivos, llamadas a API, o uso de herramientas).\n\n"
    "Retorna null (la palabra sin JSON) cuando la sesión NO sea un procedimiento "
    "reutilizable de computadora, incluyendo:\n"
    "- El trabajo real ocurrió FUERA de la computador (el usuario hizo algo "
    "físicamente, en persona, en otro dispositivo, o a mano).\n"
    "- Una tarea de una sola vez, personal, o específica del contexto que no se "
    "repetirá (recados personales, persona/lugar/fecha específico, conversación casual).\n"
    "- Una simple pregunta/respuesta o explicación sin método transferible.\n"
    "- El agente falló, se rindió, o el enfoque no vale la pena repetir.\n\n"
    "Cuando (y solo cuando) exista un procedimiento genuinamente reutilizable, "
    "retorna un objeto JSON con:\n"
    '- "title": nombre corto (menos de 10 palabras)\n'
    '- "problem": cuál fue el desafío (1-2 oraciones)\n'
    '- "solution": qué funcionó (1-2 oraciones)\n'
    '- "steps": array de instrucciones paso a paso (3-7 pasos cortos)\n'
    '- "tags": array de palabras clave relevantes (3-5 tags)\n'
    '- "category": categoría del skill (coding, research, automation, devops, creative)\n'
    '- "confidence": 0.0-1.0 qué tan confiable Y reutilizable es este procedimiento\n\n'
    "Sé conservador: en caso de duda, retorna null.\n"
    "Retorna SOLO JSON válido (o la palabra null), sin fences de markdown."
)


# ── Helpers ──────────────────────────────────────────────────────────────────

def _slugify(text: str) -> str:
    """Convierte texto a slug para nombres de archivo."""
    text = text.lower().strip()
    text = re.sub(r'[^\w\s-]', '', text)
    text = re.sub(r'[\s_]+', '-', text)
    text = re.sub(r'-+', '-', text)
    return text[:50].strip('-')


def _parse_json_object(text: str) -> Optional[dict]:
    """Extrae un objeto JSON de la respuesta del LLM tolerando ruido."""
    if not text:
        return None
    s = text.strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    if s.lower() == "null":
        return None

    end = s.rfind("}")
    if end == -1:
        return None

    def _as_dict(candidate):
        try:
            obj = json.loads(candidate)
            return obj if isinstance(obj, dict) else None
        except (json.JSONDecodeError, ValueError):
            return None

    obj = _as_dict(s)
    if obj is not None:
        return obj

    start = s.find("{")
    while 0 <= start < end:
        obj = _as_dict(s[start:end + 1])
        if obj is not None:
            return obj
        start = s.find("{", start + 1)
    return None


def _format_skill_md(skill_data: Dict) -> str:
    """Formatea un skill como SKILL.md con YAML frontmatter."""
    title = skill_data.get("title", "Untitled Skill")
    problem = skill_data.get("problem", "")
    solution = skill_data.get("solution", "")
    steps = skill_data.get("steps", [])
    tags = skill_data.get("tags", [])
    category = skill_data.get("category", "general")
    confidence = skill_data.get("confidence", 0.5)

    frontmatter = {
        "title": title,
        "category": category,
        "tags": tags,
        "confidence": round(confidence, 2),
        "source": "auto-extracted",
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }

    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, list):
            lines.append(f"{key}: {json.dumps(value)}")
        else:
            lines.append(f"{key}: \"{value}\"")
    lines.append("---")
    lines.append("")
    lines.append(f"# {title}")
    lines.append("")
    if problem:
        lines.append("## Problema")
        lines.append(problem)
        lines.append("")
    if solution:
        lines.append("## Solución")
        lines.append(solution)
        lines.append("")
    if steps:
        lines.append("## Pasos")
        for i, step in enumerate(steps, 1):
            lines.append(f"{i}. {step}")
        lines.append("")

    return "\n".join(lines)


# ── SkillExtractor ───────────────────────────────────────────────────────────

class SkillExtractor:
    """
    Extracción automática de skills de sesiones complejas.

    Uso:
        extractor = SkillExtractor(director)
        await extractor.after_session(session_data)
    """

    def __init__(self, director=None, skills_dir: Path = None):
        self.director = director
        self.skills_dir = skills_dir or SKILLS_DIR
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        self._usage_file = self.skills_dir / "_usage.json"
        self._usage = self._load_usage()

    async def after_session(
        self,
        messages: List[Dict],
        tool_calls: List[Dict] = None,
        session_id: str = "",
    ) -> Dict:
        """
        Analiza una sesión compleja y extrae skills reutilizables.

        Args:
            messages: Lista de mensajes de la sesión [{role, content}]
            tool_calls: Lista de tool calls realizados [{name, args, result}]
            session_id: ID de sesión

        Returns:
            Dict con estadísticas de la extracción
        """
        if not tool_calls or len(tool_calls) < MIN_TOOL_CALLS:
            return {"extracted": 0, "reason": "insufficient_tool_calls"}

        # Contar rondas (cambios de rol user->assistant)
        rounds = 0
        last_role = ""
        for msg in messages:
            role = msg.get("role", "")
            if role == "user" and last_role != "user":
                rounds += 1
            last_role = role

        if rounds < MIN_ROUNDS:
            return {"extracted": 0, "reason": "insufficient_rounds", "rounds": rounds}

        # Extraer vía LLM
        skill_data = await self._llm_extract(messages, tool_calls, rounds)
        if not skill_data:
            return {"extracted": 0, "reason": "no_skill_extracted"}

        # Validar confianza
        confidence = skill_data.get("confidence", 0)
        if confidence < MIN_CONFIDENCE:
            return {"extracted": 0, "reason": "low_confidence", "confidence": confidence}

        # Guardar skill
        saved_path = self._save_skill(skill_data, session_id)
        if saved_path:
            self._update_usage(skill_data.get("title", ""))
            return {
                "extracted": 1,
                "title": skill_data.get("title"),
                "confidence": confidence,
                "path": str(saved_path),
            }

        return {"extracted": 0, "reason": "save_failed"}

    async def _llm_extract(
        self,
        messages: List[Dict],
        tool_calls: List[Dict],
        rounds: int,
    ) -> Optional[Dict]:
        """Extrae skill vía LLM del Director."""
        if not self.director:
            return None

        try:
            provider = None
            if hasattr(self.director, 'provider_registry'):
                provider = self.director.provider_registry.get("gema-con-fallback")

            if not provider:
                return None

            # Construir contexto de la sesión
            recent = messages[-CONTEXT_WINDOW:] if len(messages) > CONTEXT_WINDOW else messages
            session_context = []
            for msg in recent:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, list):
                    content = " ".join(str(p.get("text", "") if isinstance(p, dict) else p) for p in content)
                if content:
                    session_context.append(f"[{role}]: {content[:500]}")

            # Agregar info de tool calls
            tools_summary = []
            for tc in tool_calls[:10]:  # últimos 10
                name = tc.get("name", "unknown")
                args = str(tc.get("args", ""))[:100]
                success = tc.get("success", True)
                tools_summary.append(f"  - {name}({args}) -> {'OK' if success else 'ERROR'}")

            prompt = SKILL_EXTRACT_PROMPT.format(
                rounds=rounds,
                tool_count=len(tool_calls),
            )

            user_content = (
                "## Contexto de la sesión\n\n"
                + "\n".join(session_context)
                + "\n\n## Tool calls realizados\n"
                + "\n".join(tools_summary)
            )

            from src.core.provider_base import LLMMessage
            from src.core.agent_runner import AgentRunner, AgentRunSpec

            llm_messages = [
                LLMMessage(role="system", content=prompt),
                LLMMessage(role="user", content=user_content),
            ]

            runner = AgentRunner(provider)
            spec = AgentRunSpec(
                messages=llm_messages,
                tools_definitions=[],
                max_iterations=1,
                max_tokens=1000,
                temperature=0.2,
            )
            result = await runner.run(spec)

            if result.stop_reason == "error":
                return None

            return _parse_json_object(result.content or "")

        except Exception as e:
            logger.debug(f"[skill-extract] LLM extraction failed: {e}")
            return None

    def _save_skill(self, skill_data: Dict, session_id: str = "") -> Optional[Path]:
        """Guarda el skill como SKILL.md."""
        try:
            title = skill_data.get("title", "untitled")
            slug = _slugify(title)
            if not slug:
                slug = f"skill-{int(time.time())}"

            category = skill_data.get("category", "general")
            cat_dir = self.skills_dir / _slugify(category)
            cat_dir.mkdir(parents=True, exist_ok=True)

            skill_dir = cat_dir / slug
            skill_dir.mkdir(parents=True, exist_ok=True)

            skill_file = skill_dir / "SKILL.md"
            content = _format_skill_md(skill_data)

            skill_file.write_text(content, encoding="utf-8")
            logger.info(f"[skill-extract] Skill guardado: {skill_file}")

            return skill_file

        except Exception as e:
            logger.error(f"[skill-extract] Error guardando skill: {e}")
            return None

    def _load_usage(self) -> Dict:
        """Carga contadores de uso."""
        if self._usage_file.exists():
            try:
                with open(self._usage_file, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _save_usage(self):
        """Guarda contadores de uso."""
        try:
            with open(self._usage_file, "w", encoding="utf-8") as f:
                json.dump(self._usage, f, indent=2)
        except Exception:
            pass

    def _update_usage(self, skill_title: str):
        """Actualiza contador de uso para un skill."""
        if not skill_title:
            return
        key = _slugify(skill_title)
        if key not in self._usage:
            self._usage[key] = {"uses": 0, "last_used": 0}
        self._usage[key]["uses"] += 1
        self._usage[key]["last_used"] = int(time.time())
        self._save_usage()

    def list_extracted_skills(self) -> List[Dict]:
        """Lista todos los skills auto-extraídos."""
        skills = []
        for skill_file in self.skills_dir.rglob("SKILL.md"):
            try:
                content = skill_file.read_text(encoding="utf-8")
                # Extraer frontmatter
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 3:
                        frontmatter = {}
                        for line in parts[1].strip().split("\n"):
                            if ":" in line:
                                key, value = line.split(":", 1)
                                frontmatter[key.strip()] = value.strip().strip('"')
                        skills.append({
                            "title": frontmatter.get("title", skill_file.parent.name),
                            "category": frontmatter.get("category", "general"),
                            "confidence": float(frontmatter.get("confidence", 0)),
                            "path": str(skill_file),
                            "uses": self._usage.get(_slugify(frontmatter.get("title", "")), {}).get("uses", 0),
                        })
            except Exception:
                continue
        return sorted(skills, key=lambda s: s.get("uses", 0), reverse=True)


# ── Instancia global ─────────────────────────────────────────────────────────

_extractor: Optional[SkillExtractor] = None


def get_skill_extractor(director=None) -> SkillExtractor:
    """Obtiene o crea la instancia global del extractor de skills."""
    global _extractor
    if _extractor is None:
        _extractor = SkillExtractor(director=director)
    elif director and not _extractor.director:
        _extractor.director = director
    return _extractor
