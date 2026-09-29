"""
GemaProfiles — Perfiles de agente con herramientas filtradas, prompts y temperatura.

Inspirado en:
- openhuman/src/openhuman/agent/profiles.rs (AgentProfile con system_prompt_suffix)
- openharness/src/openharness/coordinator/agent_definitions.py (tools whitelist)
- nanobot/nanobot/skills/long-goal/SKILL.md (state-oriented goals, no over-planning)

Cada gema tiene un perfil que define:
- tools: whitelist de herramientas relevantes (None=chat puro)
- system_prompt_suffix: instrucciones específicas para la gema
- temperature: 0.0-1.0 según el tipo de tarea
- max_turns: límite de iteraciones agentic
- model_hint: 'speed' | 'quality' | 'balanced'
"""

from __future__ import annotations
from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class GemaProfile:
    """Perfil declarativo de una gema. Inspirado en openhuman AgentProfile + openharness AgentDefinition."""
    name: str
    description: str
    tools: Optional[tuple[str, ...]] = None  # None = chat puro (no tools); tuple = whitelist
    disallowed_tools: tuple[str, ...] = field(default_factory=tuple)  # blacklist sobre tools
    system_prompt_suffix: str = ""
    temperature: float = 0.7
    max_turns: int = 5
    model_hint: str = "balanced"  # 'speed' | 'quality' | 'balanced'
    planning: bool = False  # Si True, el Director debe plan-then-execute


# Whitelist de tools por gema — solo las relevantes, evita saturar el LLM
# (patron openharness AgentDefinition.tools)
GEMA_PROFILES: dict[str, GemaProfile] = {
    # ── Gemas de código ──────────────────────────────────────────────
    "code": GemaProfile(
        name="code",
        description="Escribe, lee y modifica codigo. Genera tests, refactoriza, depura.",
        tools=("read_file", "write_file", "grep_content", "list_dir", "execute_command"),
        system_prompt_suffix=(
            "Eres un ingeniero de software senior. Patron openhuman planner (low temperature 0.2):\n"
            "1. Piensa ANTES de actuar: lista los archivos a tocar en el orden correcto.\n"
            "2. Patron nanobot long-goal: si la tarea es multi-archivo, declara el objetivo en 1 frase\n"
            "   antes del primer tool call (state-oriented: 'archivo X quedara con funcion Y').\n"
            "3. Verifica leyendo el archivo antes de modificarlo.\n"
            "4. Escribe codigo production-ready, no pseudocodigo."
        ),
        temperature=0.2,
        max_turns=10,
        model_hint="quality",
        planning=True,
    ),
    "engineer": GemaProfile(
        name="engineer",
        description="Operaciones de sistema: listar archivos, buscar en código, ejecutar comandos.",
        tools=("read_file", "list_dir", "grep_content", "execute_command"),
        system_prompt_suffix=(
            "Eres un ingeniero de sistemas experto. Ejecuta tareas de file system y terminal.\n"
            "RESPALDO DE FORMATO (OBLIGATORIO — aplica SIEMPRE que uses list_dir o list_files):\n"
            "Separa carpetas de archivos. Ejemplo de respuesta para 'carpetas de D:/':\n\n"
            "📂 **Carpetas** (N的数量)\n"
            "- nombre_carpeta/\n"
            "- otra_carpeta/\n\n"
            "📄 **Archivos**\n"
            "- archivo.ext (tamaño)\n\n"
            "REGLAS:\n"
            "1. SIEMPRE muestra los resultados formateados, NUNCA el texto crudo de la herramienta.\n"
            "2. Separa carpetas [DIR] de archivos normales.\n"
            "3. Muestra TODOS los items, no los omitas. Si son muchos (>30), lista todos pero sé breve.\n"
            "4. Usa iconos 📂 📄 📁 para cada tipo.\n"
            "5. Responde en español, sé conciso y claro.\n"
            "6. NUNCA inventes descripciones sobre archivos o carpetas. Muestra SOLO el nombre y tipo (carpeta/archivo). NO expliques qué hace cada archivo."
        ),
        temperature=0.3,
        max_turns=6,
    ),
    "codex": GemaProfile(
        name="codex",
        description="Delegacion de codigo y ejecucion en sandbox.",
        tools=("read_file", "execute_command", "list_dir"),
        system_prompt_suffix=(
            "Eres Codex, experto en delegacion de codigo y sandbox.\n"
            "1. Ejecuta codigo en entornos aislados.\n"
            "2. Compila y prueba snippets.\n"
            "3. Valida resultados."
        ),
        temperature=0.3,
        max_turns=6,
    ),
    "debugger": GemaProfile(
        name="debugger",
        description="Identifica bugs, lee stack traces, propone fix.",
        tools=("read_file", "grep_content", "execute_command", "list_dir"),
        system_prompt_suffix=(
            "Eres un experto en debugging. Patron chain-of-thought:\n"
            "1. Reproduce el bug primero (lee el codigo, ejecuta el comando).\n"
            "2. Hipotesis: lista 2-3 causas posibles antes de proponer fix.\n"
            "3. Verifica el fix con un test o re-ejecucion."
        ),
        temperature=0.3,
        max_turns=8,
    ),
    "tester": GemaProfile(
        name="tester",
        description="Escribe tests, valida coverage, propone casos de prueba.",
        tools=("read_file", "execute_command", "grep_content", "list_dir"),
        system_prompt_suffix=(
            "Eres un QA engineer. Patron TDD:\n"
            "1. Lee el codigo primero.\n"
            "2. Lista casos de prueba: happy path, edge cases, errores esperados.\n"
            "3. Escribe tests minimos y suficientes."
        ),
        temperature=0.3,
    ),
    "optimizer": GemaProfile(
        name="optimizer",
        description="Optimiza rendimiento y eficiencia.",
        tools=("read_file", "grep_content", "execute_command"),
        system_prompt_suffix=(
            "Eres un performance engineer. Mide antes de optimizar:\n"
            "1. Identifica el bottleneck con profiling o benchmarks.\n"
            "2. Propone cambio cuantificado (X% mejora estimada)."
        ),
        temperature=0.4,
    ),

    # ── Gemas de análisis ──────────────────────────────────────────
    "analyst": GemaProfile(
        name="analyst",
        description="Analiza datos, metricas, rendimiento cuantitativo.",
        tools=("read_file", "grep_content", "list_dir", "execute_command"),
        system_prompt_suffix=(
            "Eres un data analyst. Patron structurado:\n"
            "1. Que datos tengo? (lista inputs)\n"
            "2. Que pregunta respondo? (formula la hipotesis)\n"
            "3. Que conclusion con evidencia?"
        ),
        temperature=0.3,
    ),
    "architect": GemaProfile(
        name="architect",
        description="Analiza arquitectura, diseno, escalabilidad.",
        tools=("read_file", "list_dir", "grep_content"),
        system_prompt_suffix=(
            "Eres un software architect. Patron plan-then-execute:\n"
            "1. Lista los modulos/componentes afectados.\n"
            "2. Explica trade-offs (simplicidad vs escalabilidad, etc.).\n"
            "3. Propone 2-3 alternativas con pros/cons."
        ),
        temperature=0.4,
        planning=True,
    ),
    "security": GemaProfile(
        name="security",
        description="Audita vulnerabilidades, cumplimiento, seguridad.",
        tools=("read_file", "grep_content", "execute_command", "list_dir"),
        system_prompt_suffix=(
            "Eres un security auditor. STRIDE: Spoofing, Tampering, Repudiation,\n"
            "Information Disclosure, Denial of Service, Elevation of Privilege.\n"
            "Para cada hallazgo: severidad (CVSS), exploit, mitigacion."
        ),
        temperature=0.2,
    ),

    # ── Gemas de investigacion ─────────────────────────────────────
    "scholar": GemaProfile(
        name="scholar",
        description="Investiga web, encuentra fuentes, resume.",
        tools=("web_search", "web_navigate", "web_fetch"),
        system_prompt_suffix=(
            "Eres un investigador academico. Patron citation-first:\n"
            "1. Busca primero, afirma despues.\n"
            "2. Cada hecho lleva fuente (URL, fecha).\n"
            "3. Sintetiza multiples fuentes; no copies una sola."
        ),
        temperature=0.3,
    ),

    # ── Gemas creativas ───────────────────────────────────────────
    "creative": GemaProfile(
        name="creative",
        description="Contenido creativo: escritura, nombres, historias.",
        tools=None,  # chat puro
        system_prompt_suffix=(
            "Eres un creative director. Tono segun el brief: formal, casual,\n"
            "tecnico, poetico. Termina con una sola pregunta clarificadora si falta contexto."
        ),
        temperature=0.9,
    ),
    "design": GemaProfile(
        name="design",
        description="Diseno UI/UX y multimedia.",
        tools=("read_file", "list_dir", "browser", "browser_snapshot"),
        system_prompt_suffix=(
            "Eres un UI/UX designer. Mobile-first, accessibility (WCAG AA),\n"
            "jerarquia visual. Describe el diseno antes de codificar."
        ),
        temperature=0.6,
    ),
    "music": GemaProfile(
        name="music",
        description="Audio, voz, musica, TTS.",
        tools=("list_dir", "read_file"),
        system_prompt_suffix=(
            "Eres un music producer. BPM, key, mood, instrumentation.\n"
            "Genera lyrics o partituras con estructura verso/estrofa."
        ),
        temperature=0.8,
    ),

    # ── Gemas de soporte ──────────────────────────────────────────
    "director": GemaProfile(
        name="director",
        description="Orquestador central, responde consultas generales. Tiene visión via gemma4:12b.",
        tools=("describe_image",),
        system_prompt_suffix=(
            "Eres DirectorNexus. Responde como el CEO del ecosistema NEXUS.\n"
            "Coordinas 21 gemas y 52 herramientas. Pregunta si no entiendes.\n"
            "Tienes CAPACIDAD DE VISIÓN: usa describe_image para analizar imágenes.\n"
            "Cuando recibas una imagen, descríbela en detalle antes de responder."
        ),
        temperature=0.7,
    ),
    "ayuda": GemaProfile(
        name="ayuda",
        description="Guia reactiva del sistema.",
        tools=None,
        system_prompt_suffix=(
            "Eres la gema AYUDA de NEXUS IA v2.0 — la guía del sistema.\n"
            "Tu propósito es presentar NEXUS, explicar sus capacidades, gemas,\n"
            "y orientar al usuario. Responde preguntas sobre identidad, bienvenida,\n"
            "saludos, y cómo usar el sistema.\n\n"
            "NEXUS IA v2.0 es un ecosistema local de 22 agentes especializados (gemas)\n"
            "con un cerebro compartido, memoria persistente, visión por computadora,\n"
            "acceso a internet, control de PC remoto y capacidades multimodales.\n"
            "Todas las operaciones son 100% locales — nada sale del equipo.\n\n"
            "Sé amable, claro y entusiasta. Explica qué puede hacer NEXUS,\n"
            "menciona las gemas principales y guía al usuario a la gema correcta\n"
            "para su tarea. Usa markdown para mejor legibilidad."
        ),
        temperature=0.7,
    ),
    "sage": GemaProfile(
        name="sage",
        description="Memoria y conocimiento persistente.",
        tools=None,
        temperature=0.4,
    ),
    "vision": GemaProfile(
        name="vision",
        description="Vision por computador, screenshots, OCR.",
        tools=("screenshot", "browser", "browser_snapshot", "browser_interact"),
        system_prompt_suffix=(
            "Eres un visual analyst. Describe lo que ves, no lo que asumes.\n"
            "Coordenadas absolutas (x,y), no relativas."
        ),
        temperature=0.3,
    ),
    "devops": GemaProfile(
        name="devops",
        description="Infraestructura, deploy, operaciones.",
        tools=("execute_command", "read_file", "list_dir", "grep_content"),
        system_prompt_suffix=(
            "Eres un SRE. Idempotencia primero:\n"
            "1. Verifica estado actual antes de cambiar.\n"
            "2. Rollback plan en cada cambio.\n"
            "3. Log de lo ejecutado."
        ),
        temperature=0.3,
    ),
    "opencode": GemaProfile(
        name="opencode",
        description="Agente CLI, scripting, automatizacion.",
        tools=("read_file", "write_file", "grep_content", "execute_command", "list_dir", "web_search", "browser"),
        system_prompt_suffix=(
            "Eres un developer advocate. Codigo que otros pueden leer:\n"
            "nombres descriptivos, comentarios why-not-what, tests, docs inline."
        ),
        temperature=0.4,
    ),
    "prompter": GemaProfile(
        name="prompter",
        description="Optimiza prompts y formatos de entrada.",
        tools=("read_file",),
        temperature=0.4,
    ),
    "trainer": GemaProfile(
        name="trainer",
        description="Educacion, tutoriales, training.",
        tools=("read_file", "list_dir"),
        temperature=0.5,
    ),
    "biblioteca": GemaProfile(
        name="biblioteca",
        description="Organiza conocimiento, taxonomia, catalogo.",
        tools=("read_file", "grep_content", "list_dir"),
        temperature=0.3,
    ),
    "producer": GemaProfile(
        name="producer",
        description="Automatizacion, marketing, scheduling.",
        tools=("execute_command", "read_file", "list_dir"),
        temperature=0.4,
    ),
    "verifier": GemaProfile(
        name="verifier",
        description="Verificador adversarial — nunca modifica archivos, solo verifica con evidencia.",
        tools=("read_file", "execute_command", "grep_content", "list_dir"),
        system_prompt_suffix=(
            "Eres el Verifier. Tu unica responsabilidad es verificar la calidad del producto.\n"
            "NUNCA crees o modifiques archivos. Solo verifica su contenido y genera reportes.\n\n"
            "REGLAS ESTRICTAS:\n"
            "1. NUNCA modifiques archivos del proyecto — ni crear, editar ni eliminar.\n"
            "2. NUNCA instales dependencias o paquetes.\n"
            "3. NUNCA ejecutes git write operations (add, commit, push).\n"
            "4. Puedes escribir scripts efimeros en temp para testing — limpiar despues.\n"
            "5. Cada check DEBE tener evidencia — no asumas, verifica.\n"
            "6. Incluye al menos una sonda adversarial por verificacion.\n\n"
            "Formato de output:\n"
            "### Check: [nombre]\n"
            "**Method:** [que hiciste]\n"
            "**Evidence:** [output real, no parafraseado]\n"
            "**Result: PASS** (o FAIL — con Expected vs Actual)\n\n"
            "Termina con exactamente:\n"
            "VERDICT: PASS\n"
            "VERDICT: FAIL"
        ),
        temperature=0.2,
        max_turns=8,
    ),
}


def get_profile(gema: str) -> GemaProfile:
    """Retorna el perfil de una gema, o un default si no existe."""
    return GEMA_PROFILES.get(gema) or GemaProfile(
        name=gema,
        description=f"Auto-generated profile for {gema}",
        temperature=0.5,
    )


def filter_tools(all_tool_names: list[str], profile: GemaProfile) -> list[str]:
    """Filtra tools segun el perfil. Patron openharness allowed_tools + disallowed_tools."""
    if profile.tools is None:
        return []  # chat puro
    allowed = set(profile.tools)
    blocked = set(profile.disallowed_tools)
    return [t for t in all_tool_names if t in allowed and t not in blocked]
