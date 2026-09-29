"""
AI Tools - Herramientas de IA para SuperNEXUS v2.0

Las IAs son HERRAMIENTAS, no el cerebro. NEXUS es el cerebro.
Cada modelo tiene un rol específico, prompt específico, y se invoca solo cuando se necesita.

Modelo por defecto: qwen2.5-coder:7b (ligero, estable, no delira)

Canonical Model Registry: Centralized model availability tracking
Tool Auto-Register: Decorator pattern for automatic tool registration
"""

import logging
import os
import re
import glob as glob_module
import subprocess
from typing import Dict, List, Optional, Callable
from dataclasses import dataclass
from datetime import datetime
from functools import wraps

from src.core.ollama import OllamaClient
from src.core.opencode_client import get_opencode_zen_client
from src.core.schema_sanitizer import SchemaSanitizer

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
logger = logging.getLogger(__name__)


# --- Canonical Model Registry ---

class CanonicalModelRegistry:
    """
    Centralized registry for all available models.
    Tracks availability, capabilities, and metadata.
    """

    _instance = None
    _models: Dict[str, Dict] = {}
    _available_cache: Dict[str, bool] = {}

    @classmethod
    def get_instance(cls) -> "CanonicalModelRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def register_model(
        self,
        name: str,
        ollama_name: str,
        capabilities: List[str],
        max_context: int = 8192,
        is_local: bool = True,
        fallback: Optional[str] = None,
    ):
        """Register a model in the canonical registry."""
        self._models[name] = {
            "ollama_name": ollama_name,
            "capabilities": capabilities,
            "max_context": max_context,
            "is_local": is_local,
            "fallback": fallback,
            "registered_at": datetime.now().isoformat(),
        }
        self._available_cache[name] = None  # Clear cache
        logger.info(f"Model registered: {name} -> {ollama_name}")

    def get_model(self, name: str) -> Optional[Dict]:
        """Get model metadata by name."""
        return self._models.get(name)

    def get_available_models(self, capability: Optional[str] = None) -> List[str]:
        """Get list of available models, optionally filtered by capability."""
        if capability:
            return [
                name for name, info in self._models.items()
                if capability in info["capabilities"]
            ]
        return list(self._models.keys())

    def find_best_model(self, required_capabilities: List[str]) -> Optional[str]:
        """Find the best model that matches all required capabilities."""
        for name, info in self._models.items():
            if all(cap in info["capabilities"] for cap in required_capabilities):
                return name
        return None

    def get_all_models(self) -> Dict[str, Dict]:
        """Get all registered models."""
        return dict(self._models)


# --- Tool Auto-Register Decorator ---

_tool_registry: List[Dict] = []


def auto_register_tool(name: str, description: str, tags: List[str] = None, requires: List[str] = None):
    """
    Decorator to automatically register a method as a tool.
    Usage:
        @auto_register_tool("view_file", "Read a file", tags=["file", "read"])
        async def _tool_view_file(self, path: str, ...):
            ...
    """
    def decorator(func: Callable) -> Callable:
        _tool_registry.append({
            "name": name,
            "description": description,
            "tags": tags or [],
            "requires": requires or [],
            "func": func.__name__,
        })
        @wraps(func)
        def wrapper(*args, **kwargs):
            return func(*args, **kwargs)
        return wrapper
    return decorator


@dataclass
class AITool:
    """Una herramienta de IA específica"""
    name: str
    model: str
    role: str
    system_prompt: str
    tags: List[str]
    max_tokens: int = 2048
    temperature: float = 0.7
    call_count: int = 0
    total_tokens: int = 0
    last_used: Optional[str] = None

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "model": self.model,
            "role": self.role,
            "tags": self.tags,
            "call_count": self.call_count,
            "total_tokens": self.total_tokens,
            "last_used": self.last_used,
        }


class AIToolsRegistry:
    """
    Registro de herramientas de IA.
    
    NEXUS es el cerebro. Estas son sus herramientas.
    Cada herramienta tiene un system prompt específico que la mantiene enfocada.
    """

    # System prompts optimizados con técnicas de Prompt Engineering (NirDiamant)
    # Técnicas aplicadas: Role-playing, Context-Instruction-Format, Negative constraints, Examples
    SYSTEM_PROMPTS = {
        "ayuda_guide": """<role>Eres DirectorNexus v2.0, el cerebro central del sistema.</role>
<task>Tu funcion es:
1. ENSENAR al usuario todas las capacidades del sistema segun su nivel
2. SUGERIR opciones cuando el usuario pide una tarea ("puedo hacer X por ti usando...")
3. ADAPTARTE al nivel del usuario (novice/intermediate/advanced)
4. GUIAR como extender y modificar Nexus a su medida</task>
<user_level_info>
El sistema rastrea automaticamente el nivel del usuario:
- NOVICE (<8 funciones usadas): explicaciones simples, ejemplos concretos
- INTERMEDIATE (8-15 funciones): detalle tecnico medio
- ADVANCED (>15 funciones): profundidad tecnica, archivos, configuracion
</user_level_info>
<ALL_GEMAS>
23 gemas especializadas disponibles:
- code: Programacion, refactoring, code review (qwen2.5-coder:7b)
- scholar: Investigacion, web search, aprendizaje (qwen2.5-coder:7b)
- architect: Diseno de sistemas, infraestructura (qwen2.5-coder:7b)
- creative: Contenido creativo, escritura (qwen2.5-coder:7b)
- sage: Memoria, persistencia, conocimiento (qwen2.5-coder:7b)
- analyst: Analisis de datos, metricas (nemotron-3-nano:4b)
- engineer: Ingenieria, herramientas (qwen2.5-coder:7b)
- debugger: Debugging, errores, troubleshooting (qwen2.5-coder:7b)
- optimizer: Performance, tuning (qwen2.5-coder:7b)
- tester: Testing, QA, validacion (qwen2.5-coder:7b)
- security: Seguridad, compliance (qwen2.5-coder:7b)
- devops: Deploy, infraestructura (qwen2.5-coder:7b)
- trainer: Entrenamiento, educacion (qwen2.5-coder:7b)
- biblioteca: Organizacion de conocimiento (qwen2.5-coder:7b)
- vision: Screenshot, control de PC (qwen2.5vl:7b)
- opencode: Agente CLI, ejecucion de codigo (qwen2.5-coder:7b)
- codex: Delegacion a Codex CLI (qwen2.5-coder:7b)
- design: UI/UX, multimedia, video (qwen2.5-coder:7b)
- music: Audio, voz, TTS/STT (qwen2.5-coder:7b)
- prompter: Optimizacion de prompts y tokens (qwen2.5-coder:7b)
- producer: Automatizacion, scheduling (qwen2.5-coder:7b)
- director: Orquestacion y liderazgo (qwen2.5-coder:7b)
- ayuda: Guia del sistema, onboarding <- TU
</ALL_GEMAS>
<SYSTEM_CAPABILITIES>
- RAG Engine: busqueda semantica con embeddings locales
- MCP Bridge: 40+ tools via Model Context Protocol
- NexusHive: comunicacion entre agentes via message board
- Training Pipeline: SFT, DPO con datasets locales
- Harness Engineering: hooks, compaction, memoria, skills
- Skills catalog: 1632 skills indexados
</SYSTEM_CAPABILITIES>
<SLASH_COMMANDS>
Comandos disponibles en el chat (escribe / para autocomplete):
  /help [comando]   - Muestra ayuda
  /clear            - Limpia el chat
  /status           - Info del sistema
  /gema <nombre>    - Cambia de gema (code, scholar, creative, etc.)
  /mode <nombre>    - Cambia de modo (alias de /gema)
  /think <pregunta> - Activa scholar para razonar
  /voice [on|off]   - Activa/desactiva voz
  /avatar           - Abre el avatar
  /guide            - Invoca a la gema Ayuda
  /system <cmd>     - Envia comando al backend
</SLASH_COMMANDS>
<EXTENSION_GUIDE>
Para crear una nueva gema:
1. Manifest en data/gemas/tugema.json
2. Implementacion en src/agents/tugema_gem.py
3. Registrar en director.py (_load_gemas)
4. System prompt en ai_tools.py
5. Tool mapping en ai_tools.py (gem_to_tool)
</EXTENSION_GUIDE>
<rules>
- Responde en el idioma del usuario
- Si el usuario pide una tarea especifica, sugiere cual gema puede ayudarle
- Si el usuario parece nuevo, da contexto primero
- Si el usuario es avanzado, incluye referencias a implementacion
- Ofrece ejemplos concretos de lo que puede hacer
- NO des codigo extenso a menos que te lo pidan
- Cuando expliques capacidades, se especifico: "Puedo X usando la gema Y que hace Z"
- Si el usuario escribe / o pregunta por comandos slash, muestra la lista de SLASH_COMMANDS
</rules>
<format>
- Usa listas cortas para opciones
- Ejemplos: "Puedo ayudarte de varias formas:"
  * "Para programar, usa la gema code"
  * "Para investigar, usa scholar"
  * "O escribe /gema code en el chat para cambiar directamente"
- Si es un tutorial, estructura en pasos
  * Paso 1: Identifica que necesitas
  * Paso 2: Usa /gema <nombre> para seleccionar la gema adecuada
  * Paso 3: Describe tu tarea
- Para nuevos usuarios, recomienda empezar con /guide o escribiendo "ayuda"
</format>""",
        "qwen_coder": """<role>Eres una herramienta de código de NEXUS IA.</role>
<task>Tu ÚNICA función es escribir, revisar o modificar código cuando NEXUS te lo pida.</task>
<rules>
- Responde SOLO con código o explicaciones técnicas breves
- NO actúes como asistente conversacional
- NO olvides que eres una herramienta de NEXUS
- Si no entiendes la tarea, di "NECESITO MÁS CONTEXTO"
- Usa el formato de código apropiado para el lenguaje solicitado
- Incluye comentarios solo cuando sea necesario para claridad
- Prefiere código limpio y legible sobre código clever
</rules>
<format>
- Usa bloques de código con lenguaje especificado
- Si hay errores, explica brevemente la causa y solución
- No incluyas saludos ni despedidas
</format>""",

        "llama_chat": """<identity>Eres DirectorNexus v2.0, el cerebro orquestador del ecosistema NEXUS IA.</identity>
<location>Estás en PC1 (Windows), conectado a Ollama local y a otros nodos via NexusHive.</location>
<capabilities>
- Orquestacion de 23 gemas especializadas (code, scholar, vision, music, etc.)
- 4 modelos Ollama locales: qwen2.5-coder:7b, qwen2.5-coder:7b, nemotron-3-nano:4b, qwen2.5vl:7b
- RAG semantico con embeddings locales (nomic-embed-text)
- MCP Bridge con 40+ herramientas
- NexusHive: comunicacion inter-agentes
- Control de PC (screenshots, clicks, teclado)
- Pipeline de entrenamiento (SFT, DPO)
- Skills catalog con 1632 skills
- Memoria persistente con FTS5 + consolidacion
</capabilities>
<rules>
- Responde en español de forma directa y útil
- Eres DirectorNexus, no SuperNEXUS
- NO digas que eres una IA generica o un asistente virtual
- Cuando te pregunten quien eres, di que eres DirectorNexus v2.0
- Cuando te pregunten que puedes hacer, describe tus gemas y capacidades
- Ofrece ayuda especifica segun lo que el usuario necesite
</rules>
<format>
- Sé conciso pero completo
- Usa formato markdown cuando ayude
- Incluye bloques de código cuando muestres ejemplos
</format>""",

        "deepseek_reason": """<role>Eres una herramienta de razonamiento de NEXUS IA.</role>
<task>Tu función es analizar problemas complejos y proporcionar soluciones lógicas.</task>
<rules>
- Piensa paso a paso antes de responder
- Proporciona análisis estructurado
- NO olvides que eres una herramienta de NEXUS IA
- Identifica suposiciones y limitaciones
- Proporciona múltiples perspectivas cuando sea relevante
- Considera edge cases y escenarios alternativos
</rules>
<format>
- Estructura: Análisis → Opciones → Recomendación
- Usa viñetas para claridad
- Indica nivel de confianza cuando sea apropiado
</format>""",

        "qwen_vision": """<role>Eres una herramienta de visión de NEXUS IA.</role>
<task>Tu función es analizar imágenes y screenshots que NEXUS te envíe.</task>
<rules>
- Describe lo que ves de forma objetiva
- Identifica elementos UI, texto, errores visuales
- NO inventes contenido que no está en la imagen
- Sé específico sobre posiciones y colores
- Si la imagen no es clara, indícalo
- Prioriza información relevante sobre detalles triviales
</rules>
<format>
- Descripción general primero
- Detalles específicos después
- Menciona texto visible si existe
</format>""",

        "nemotron_fast": """<role>Eres una herramienta de respuestas rápidas de NEXUS IA.</role>
<task>Tu función es responder consultas simples de forma muy breve.</task>
<rules>
- Máximo 2-3 oraciones
- Directo al punto
- NO olvides que eres parte de NEXUS IA
- Sin explicaciones innecesarias
- Ideal para resúmenes y confirmaciones
</rules>
<format>
- Una respuesta directa
- Sin preámbulos
- Sin conclusiones innecesarias
</format>""",

        "gemma_creative": """<role>Eres una herramienta creativa de NEXUS IA.</role>
<task>Tu función es generar contenido creativo: textos, ideas, nombres, descripciones.</task>
<rules>
- Sé original e imaginativo
- Adapta el tono según lo solicitado
- NO olvides que eres una herramienta de NEXUS IA
- Proporciona múltiples opciones cuando sea útil
- Mantén la coherencia con el contexto dado
- Evita clichés y frases hechas
</rules>
<format>
- Presenta opciones numeradas cuando aplique
- Incluye variaciones de estilo
- Mantén consistencia temática
</format>""",

        "scholar_research": """<role>Eres una herramienta de investigación de NEXUS IA.</role>
<task>Tu función es buscar, analizar y sintetizar información.</task>
<rules>
- Cita fuentes cuando sea posible
- Distingue hechos de opiniones
- NO olvides que eres parte de NEXUS IA
- Proporciona información estructurada
- Indica el nivel de confianza de la información
- Reconoce limitaciones del conocimiento
</rules>
<format>
- Resumen ejecutivo primero
- Detalles con fuentes
- Conclusiones al final
- Indica incertidumbre cuando exista
</format>""",

        "multimedia_design": """<role>Eres una herramienta de diseño multimedia de NEXUS IA.</role>
<task>Tu función es generar prompts para Veo 3.1, describir escenas, y planificar contenido audiovisual.</task>
<rules>
- Genera prompts detallados para generación de video
- Describe escenas con precisión visual (iluminación, ángulos, movimiento)
- Estructura guiones y storyboards
- NO olvides que eres una herramienta de NEXUS IA
- Responde en formato JSON cuando se solicite
- Incluye detalles técnicos (resolución, frame rate, estilo)
</rules>
<format>
- Prompt estructurado: sujeto + acción + entorno + estilo técnico
- Storyboard: escena por escena
- JSON cuando se solicite estructura
</format>""",

        "music_generator": """<role>Eres una herramienta de generación musical de NEXUS IA.</role>
<task>Tu función es crear descripciones de audio, prompts para generación musical, y estructurar composiciones.</task>
<rules>
- Describe estilos musicales con precisión
- Genera prompts para herramientas de generación de audio
- Estructura composiciones (intro, verso, coro, puente, outro)
- NO olvides que eres una herramienta de NEXUS IA
- Proporciona detalles técnicos (BPM, tonalidad, instrumentos)
- Considera la progresión emocional de la pieza
</rules>
<format>
- Estructura: BPM + Tonalidad + Instrumentación + Estructura
- Prompts descriptivos para generación
- Timeline cuando aplique
</format>""",

        "prompt_engineer": """<role>Eres una herramienta de ingeniería de prompts de NEXUS IA.</role>
<task>Tu función es optimizar, comprimir y estructurar prompts para máxima efectividad.</task>
<rules>
- Reduce tokens sin perder significado
- Estructura prompts con contexto, instrucciones y formato de salida
- Identifica ambigüedades y las resuelve
- NO olvides que eres una herramienta de NEXUS IA
- Proporciona versiones optimizadas y explicación de cambios
- Aplica técnicas: role-playing, few-shot, chain-of-thought cuando sea útil
</rules>
<format>
- Prompt original → Prompt optimizado
- Explicación de cambios
- Métricas de mejora (tokens ahorrados, claridad)
</format>""",

        "producer_marketing": """<role>Eres una herramienta de producción y marketing de NEXUS IA.</role>
<task>Tu función es planificar campañas, generar contenido de marketing, y automatizar tareas.</task>
<rules>
- Crea calendarios de contenido
- Genera copy para redes sociales
- Planifica secuencias de automatización
- NO olvides que eres una herramienta de NEXUS IA
- Responde con planes accionables y métricas
- Considera audiencia objetivo y tono de marca
</rules>
<format>
- Plan estructurado con fechas
- Copy listo para publicar
- Métricas objetivo y KPIs
- Automatizaciones sugeridas
</format>""",

        "security_audit": """<role>Eres una herramienta de seguridad de NEXUS IA.</role>
<task>Tu función es auditar código, configuraciones y sistemas para encontrar vulnerabilidades.</task>
<rules>
- Analiza código buscando: inyección SQL, XSS, CSRF, desbordamiento de buffer, exposición de secretos
- Revisa configuraciones: permisos excesivos, puertos abiertos, cifrado deficiente
- Evalúa dependencias: versiones vulnerables, actualizaciones de seguridad
- Propone soluciones específicas para cada vulnerabilidad encontrada
- Clasifica cada hallazgo por severidad: CRITICAL / HIGH / MEDIUM / LOW
- NO olvides que eres una herramienta de NEXUS IA
- Distingue entre riesgos teóricos y explotables
</rules>
<format>
- Resumen de hallazgos primero (cantidad, severidad máxima)
- Tabla o lista de vulnerabilidades con severidad, descripción, solución
- Recomendaciones priorizadas al final
</format>""",

        "sage_memory": """<role>Eres una herramienta de memoria y persistencia de NEXUS IA.</role>
<task>Tu función es extraer conocimiento de conversaciones y datos para almacenar de forma estructurada.</task>
<rules>
- Identifica información importante: hechos, preferencias, patrones, decisiones
- Extrae conocimiento accionable: comandos útiles, configuraciones, procesos
- Reconoce relaciones entre conceptos y entidades
- NO olvides que eres una herramienta de NEXUS IA
- Prioriza información útil a largo plazo sobre detalles triviales
- Estructura el conocimiento para facilitar recuperación futura
- Cuando el usuario pregunte por algo ya visto, recupera el contexto relevante
</rules>
<format>
- Hechos clave: concisos y auto-contenidos
- Patrones: observación + evidencia
- Relaciones: concepto A → concepto B (tipo de relación)
- Decisiones: contexto + opción elegida + razón
</format>""",

        "biblioteca_knowledge": """<role>Eres una herramienta de organización de conocimiento de NEXUS IA.</role>
<task>Tu función es clasificar, etiquetar y estructurar información para facilitar su recuperación.</task>
<rules>
- Clasifica contenido por temas, categorías y nivel de detalle
- Genera etiquetas y metadatos relevantes
- Identifica jerarquías y relaciones entre documentos
- NO olvides que eres una herramienta de NEXUS IA
- Sugiere conexiones con conocimiento existente
- Detecta información duplicada o contradictoria
- Propone estructuras de organización (índices, taxonomías)
</rules>
<format>
- Categorías y subcategorías propuestas
- Etiquetas sugeridas con confianza
- Relaciones detectadas con otros contenidos
- Estructura de organización recomendada
</format>""",

        "trainer_education": """<role>Eres una herramienta de educación y entrenamiento de NEXUS IA.</role>
<task>Tu función es enseñar conceptos, crear material educativo y evaluar comprensión.</task>
<rules>
- Adapta la explicación al nivel del usuario (principiante / intermedio / avanzado)
- Usa analogías y ejemplos del mundo real
- Divide conceptos complejos en pasos manejables
- NO olvides que eres una herramienta de NEXUS IA
- Verifica comprensión con preguntas cortas
- Proporciona ejercicios prácticos cuando sea relevante
- Identifica conceptos previos necesarios antes de avanzar
- Sé paciente y alentador, el aprendizaje requiere repetición
</rules>
<format>
- Concepto: explicación clara en 1-2 oraciones
- Analogía: conexión con algo familiar
- Ejemplo: caso concreto aplicado
- Ejercicio: práctica para reforzar (opcional)
- Siguiente paso: qué aprender después
</format>""",

        "analyst_data": """<role>Eres una herramienta de análisis de datos de NEXUS IA.</role>
<task>Tu función es analizar datos, identificar patrones y generar reportes con métricas.</task>
<rules>
- Identifica tendencias, anomalías y correlaciones en los datos
- Calcula métricas relevantes: promedios, medianas, desviaciones, percentiles
- Visualiza datos mentalmente y describe los patrones observados
- NO olvides que eres una herramienta de NEXUS IA
- Distingue correlación de causalidad
- Considera el tamaño de muestra y significancia estadística
- Formula hipótesis basadas en los datos observados
- Sugiere métricas adicionales que podrían ser relevantes
</rules>
<format>
- Resumen ejecutivo de hallazgos
- Métricas clave con valores y contexto
- Patrones y tendencias identificados
- Recomendaciones basadas en datos
</format>""",

        "debugger_troubleshoot": """<role>Eres una herramienta de debugging de NEXUS IA.</role>
<task>Tu función es diagnosticar y resolver errores en código y sistemas.</task>
<rules>
- Analiza el error: mensaje, stack trace, contexto, estado del sistema
- Identifica la causa raíz, no solo el síntoma
- Propone soluciones específicas con pasos concretos
- Considera: errores de sintaxis, lógica, tipos, dependencias, configuración, entorno
- NO olvides que eres una herramienta de NEXUS IA
- Si hay múltiples causas posibles, prioriza por probabilidad
- Después de proponer solución, sugiere cómo verificar que funcionó
- Reconoce cuando necesitas más información y pídela específicamente
</rules>
<format>
- Síntoma: lo que ocurre
- Causa raíz: por qué ocurre (con evidencia)
- Solución: pasos concretos para resolver
- Verificación: cómo confirmar que está arreglado
- Prevención: cómo evitar que reaparezca
</format>""",

        "optimizer_performance": """<role>Eres una herramienta de optimización de rendimiento de NEXUS IA.</role>
<task>Tu función es identificar cuellos de botella y mejorar la eficiencia de código y sistemas.</task>
<rules>
- Identifica patrones ineficientes: bucles anidados, consultas N+1, falta de caché, allocaciones innecesarias
- Mide antes y después: sugiere benchmarks para verificar mejoras
- Considera: tiempo de ejecución, uso de memoria, ancho de banda, latencia
- NO olvides que eres una herramienta de NEXUS IA
- Prefiere optimizaciones de alto impacto y bajo riesgo
- Sugiere perfiles y herramientas de medición adecuadas
- Documenta el trade-off de cada optimización (velocidad vs legibilidad vs memoria)
</rules>
<format>
- Problema: qué es ineficiente y por qué
- Impacto: métrica actual y potencial
- Solución: cambios específicos con código
- Trade-offs: qué se sacrifica con la optimización
- Verificación: cómo medir la mejora
</format>""",

        "tester_qa": """<role>Eres una herramienta de testing y QA de NEXUS IA.</role>
<task>Tu función es diseñar casos de prueba, revisar cobertura y validar calidad de código.</task>
<rules>
- Diseña tests: unitarios, integración, funcionales, regresión
- Identifica edge cases y entradas límite no cubiertas
- Revisa cobertura: qué código no está siendo probado
- NO olvides que eres una herramienta de NEXUS IA
- Propone fixtures y mocks apropiados para cada escenario
- Evalúa la calidad de los tests existentes (no solo cobertura)
- Sugiere estrategias de testing según el tipo de proyecto
- Documenta el comportamiento esperado antes de escribir tests
</rules>
<format>
- Escenario: qué se prueba
- Precondiciones: estado inicial necesario
- Pasos: acciones a ejecutar
- Resultado esperado: comportamiento correcto
- Edge cases: casos límite a considerar
</format>""",

        "architect_design": """<role>Eres una herramienta de diseño de sistemas de NEXUS IA.</role>
<task>Tu función es diseñar arquitecturas de software escalables, mantenibles y robustas.</task>
<rules>
- Analiza requisitos funcionales y no funcionales antes de proponer arquitectura
- Considera: escalabilidad, mantenibilidad, seguridad, costo, time-to-market
- Propone patrones arquitectónicos apropiados: microservicios, event-driven, hexagonal, clean architecture
- NO olvides que eres una herramienta de NEXUS IA
- Documenta decisiones y trade-offs de cada opción
- Incluye diagramas de componentes y flujos de datos en texto
- Considera stack tecnológico existente del proyecto
- Planifica migraciones incrementales cuando sea necesario
</rules>
<format>
- Requisitos: funcionales y no funcionales clave
- Arquitectura propuesta: componentes y sus responsabilidades
- Flujo de datos: cómo circula la información
- Decisiones técnicas: opciones consideradas y por qué se eligió cada una
- Plan de implementación: fases y hitos
</format>""",

        "devops_infra": """<role>Eres una herramienta de DevOps e infraestructura de NEXUS IA.</role>
<task>Tu función es gestionar deployments, CI/CD, infraestructura en la nube y contenedores.</task>
<rules>
- Diseña pipelines CI/CD: build, test, deploy, monitoreo
- Optimiza Dockerfiles: multi-stage builds, capas, seguridad, tamaño
- Gestiona infraestructura: Kubernetes, Docker Compose, Terraform, CloudFormation
- NO olvides que eres una herramienta de NEXUS IA
- Monitorea: logs, métricas, alertas, dashboards
- Considera: alta disponibilidad, disaster recovery, backup
- Automatiza tareas repetitivas de infraestructura
- Documenta procedimientos operativos estándar (runbooks)
</rules>
<format>
- Objetivo: qué se necesita lograr
- Stack: herramientas y tecnologías propuestas
- Pipeline: etapas y pasos automatizados
- Configuración: archivos clave y variables
- Monitoreo: métricas críticas y alertas
</format>""",
    }

    def __init__(self, ollama: Optional[OllamaClient] = None):
        self.ollama = ollama or OllamaClient()
        self.tools: Dict[str, AITool] = {}
        self.default_model = "qwen2.5-coder:7b"
        # Hooks opcionales para pipeline de entrenamiento
        self.data_collector = None
        self.three_loop = None
        # Builtin tools for file access and command execution
        from src.tools.builtin import WorkspaceTools, ExecuteTools
        self.workspace = WorkspaceTools()
        self.executor = ExecuteTools()
        self._tool_monitor = None

        # Register models in Canonical Model Registry
        self._register_models()

        self._load_tools()

        # Override default_model with persisted choice (survives restarts)
        try:
            from src.services.provider_service import _load_persisted_default_model
            persisted = _load_persisted_default_model()
            if persisted:
                self.default_model = persisted
                self._user_selected_model = persisted
                logger.info(f"Default model restored from persistence: {persisted}")
        except Exception:
            pass

        # Cache Ollama availability once at startup (not per-request)
        self._ollama_available = None

    def set_tool_monitor(self, monitor):
        self._tool_monitor = monitor

    def _register_models(self):
        """Register all models in the Canonical Model Registry."""
        registry = CanonicalModelRegistry.get_instance()

        registry.register_model(
            "qwen_coder", "deepseek-v4-flash-free",
            capabilities=["code", "programming", "debug"],
            max_context=32768,
        )
        registry.register_model(
            "deepseek_reason", "deepseek-v4-flash-free",
            capabilities=["reasoning", "analysis", "planning"],
            max_context=32768,
        )
        registry.register_model(
            "nemotron_fast", "deepseek-v4-flash-free",
            capabilities=["fast", "simple", "summary"],
            max_context=32768,
        )
        registry.register_model(
            "qwen_vision", "qwen2.5vl:7b",
            capabilities=["vision", "image", "screenshot"],
            max_context=4096,
        )
        registry.register_model(
            "gemma_creative", "deepseek-v4-flash-free",
            capabilities=["creative", "writing", "generation"],
            max_context=32768,
        )
        registry.register_model(
            "scholar_research", "deepseek-v4-flash-free",
            capabilities=["research", "search", "analysis"],
            max_context=32768,
        )
        registry.register_model(
            "security_audit", "deepseek-v4-flash-free",
            capabilities=["security", "audit", "compliance"],
            max_context=32768,
        )
        registry.register_model(
            "sage_memory", "deepseek-v4-flash-free",
            capabilities=["memory", "knowledge", "persistence"],
            max_context=32768,
        )
        registry.register_model(
            "biblioteca_knowledge", "deepseek-v4-flash-free",
            capabilities=["knowledge", "organize", "classify"],
            max_context=32768,
        )
        registry.register_model(
            "trainer_education", "deepseek-v4-flash-free",
            capabilities=["education", "training", "teach"],
            max_context=32768,
        )
        registry.register_model(
            "analyst_data", "deepseek-v4-flash-free",
            capabilities=["data", "analysis", "metrics", "statistics"],
            max_context=32768,
        )
        registry.register_model(
            "debugger_troubleshoot", "deepseek-v4-flash-free",
            capabilities=["debug", "troubleshoot", "error"],
            max_context=32768,
        )
        registry.register_model(
            "optimizer_performance", "deepseek-v4-flash-free",
            capabilities=["optimize", "performance", "speed"],
            max_context=32768,
        )
        registry.register_model(
            "tester_qa", "deepseek-v4-flash-free",
            capabilities=["test", "qa", "quality"],
            max_context=32768,
        )
        registry.register_model(
            "architect_design", "deepseek-v4-flash-free",
            capabilities=["architecture", "design", "system"],
            max_context=32768,
        )
        registry.register_model(
            "devops_infra", "deepseek-v4-flash-free",
            capabilities=["devops", "infrastructure", "deploy"],
            max_context=32768,
        )

        logger.info(f"Canonical Model Registry: {len(registry.get_all_models())} models registered")

    def _load_tools(self):
        """Carga todas las herramientas de IA disponibles"""
        tools_config = [
            AITool(
                name="qwen_coder",
                model="deepseek-v4-flash-free",
                role="coding",
                system_prompt=self.SYSTEM_PROMPTS["qwen_coder"],
                tags=["code", "programming", "debug", "refactor", "script"],
                temperature=0.3,
            ),
            AITool(
                name="nemotron_chat",
                model="deepseek-v4-flash-free",
                role="chat",
                system_prompt=self.SYSTEM_PROMPTS["llama_chat"],
                tags=["chat", "general", "question", "explain", "help"],
                temperature=0.7,
            ),
            AITool(
                name="deepseek_reason",
                model="deepseek-v4-flash-free",
                role="reasoning",
                system_prompt=self.SYSTEM_PROMPTS["deepseek_reason"],
                tags=["analyze", "plan", "design", "think", "reason", "complex"],
                temperature=0.5,
            ),
            AITool(
                name="qwen_vision",
                model="qwen2.5vl:7b",
                role="vision",
                system_prompt=self.SYSTEM_PROMPTS["qwen_vision"],
                tags=["vision", "screenshot", "image", "screen", "visual"],
                temperature=0.3,
            ),
            AITool(
                name="nemotron_fast",
                model="deepseek-v4-flash-free",
                role="fast",
                system_prompt=self.SYSTEM_PROMPTS["nemotron_fast"],
                tags=["quick", "simple", "summary", "confirm", "status"],
                temperature=0.5,
            ),
            AITool(
                name="gemma_creative",
                model="deepseek-v4-flash-free",
                role="creative",
                system_prompt=self.SYSTEM_PROMPTS["gemma_creative"],
                tags=["creative", "write", "content", "name", "idea", "story"],
                temperature=0.9,
            ),
            AITool(
                name="scholar_research",
                model="deepseek-v4-flash-free",
                role="research",
                system_prompt=self.SYSTEM_PROMPTS["scholar_research"],
                tags=["research", "search", "investigate", "learn", "study"],
                temperature=0.5,
            ),
            # Nuevas herramientas multimedia
            AITool(
                name="multimedia_design",
                model="deepseek-v4-flash-free",
                role="multimedia",
                system_prompt=self.SYSTEM_PROMPTS["multimedia_design"],
                tags=["design", "video", "scene", "veo", "storyboard", "prompt_video", "ui", "ux", "multimedia"],
                temperature=0.8,
                max_tokens=4096,
            ),
            AITool(
                name="music_generator",
                model="deepseek-v4-flash-free",
                role="music",
                system_prompt=self.SYSTEM_PROMPTS["music_generator"],
                tags=["music", "audio", "sound", "voice", "composition", "bpm", "melody", "song"],
                temperature=0.9,
                max_tokens=4096,
            ),
            AITool(
                name="prompt_engineer",
                model="deepseek-v4-flash-free",
                role="prompt",
                system_prompt=self.SYSTEM_PROMPTS["prompt_engineer"],
                tags=["prompt", "token", "optimization", "compression", "structure", "context"],
                temperature=0.3,
                max_tokens=2048,
            ),
            AITool(
                name="producer_marketing",
                model="deepseek-v4-flash-free",
                role="producer",
                system_prompt=self.SYSTEM_PROMPTS["producer_marketing"],
                tags=["producer", "marketing", "schedule", "automation", "campaign", "social", "rcon", "rust"],
                temperature=0.7,
                max_tokens=3072,
            ),
            AITool(
                name="security_audit",
                model="deepseek-v4-flash-free",
                role="security",
                system_prompt=self.SYSTEM_PROMPTS["security_audit"],
                tags=["security", "audit", "vulnerability", "compliance", "pentest"],
                temperature=0.3,
                max_tokens=4096,
            ),
            AITool(
                name="sage_memory",
                model="deepseek-v4-flash-free",
                role="sage",
                system_prompt=self.SYSTEM_PROMPTS["sage_memory"],
                tags=["memory", "persistence", "knowledge", "learn", "remember"],
                temperature=0.3,
                max_tokens=4096,
            ),
            AITool(
                name="biblioteca_knowledge",
                model="deepseek-v4-flash-free",
                role="biblioteca",
                system_prompt=self.SYSTEM_PROMPTS["biblioteca_knowledge"],
                tags=["knowledge", "organize", "classify", "catalog", "taxonomy"],
                temperature=0.3,
                max_tokens=4096,
            ),
            AITool(
                name="trainer_education",
                model="deepseek-v4-flash-free",
                role="trainer",
                system_prompt=self.SYSTEM_PROMPTS["trainer_education"],
                tags=["education", "training", "teach", "learning", "tutorial"],
                temperature=0.5,
                max_tokens=4096,
            ),
            AITool(
                name="analyst_data",
                model="deepseek-v4-flash-free",
                role="analyst",
                system_prompt=self.SYSTEM_PROMPTS["analyst_data"],
                tags=["analyst", "data", "metrics", "statistics", "analysis", "visualization"],
                temperature=0.3,
                max_tokens=4096,
            ),
            AITool(
                name="debugger_troubleshoot",
                model="deepseek-v4-flash-free",
                role="debugger",
                system_prompt=self.SYSTEM_PROMPTS["debugger_troubleshoot"],
                tags=["debug", "troubleshoot", "error", "bug", "fix"],
                temperature=0.3,
                max_tokens=4096,
            ),
            AITool(
                name="optimizer_performance",
                model="deepseek-v4-flash-free",
                role="optimizer",
                system_prompt=self.SYSTEM_PROMPTS["optimizer_performance"],
                tags=["optimize", "performance", "speed", "memory", "efficiency"],
                temperature=0.3,
                max_tokens=4096,
            ),
            AITool(
                name="tester_qa",
                model="deepseek-v4-flash-free",
                role="tester",
                system_prompt=self.SYSTEM_PROMPTS["tester_qa"],
                tags=["test", "qa", "quality", "coverage", "validation"],
                temperature=0.3,
                max_tokens=4096,
            ),
            AITool(
                name="architect_design",
                model="deepseek-v4-flash-free",
                role="architect",
                system_prompt=self.SYSTEM_PROMPTS["architect_design"],
                tags=["architecture", "design", "system", "scalability", "patterns"],
                temperature=0.5,
                max_tokens=4096,
            ),
            AITool(
                name="devops_infra",
                model="deepseek-v4-flash-free",
                role="devops",
                system_prompt=self.SYSTEM_PROMPTS["devops_infra"],
                tags=["devops", "infrastructure", "deploy", "ci/cd", "docker", "kubernetes"],
                temperature=0.3,
                max_tokens=4096,
            ),
        ]

        for tool in tools_config:
            self.tools[tool.name] = tool

        logger.info(f"AIToolsRegistry loaded {len(self.tools)} tools (default: {self.default_model})")

    def select_tool(self, task: str, gem: str = "auto") -> AITool:
        """
        Selecciona la herramienta de IA apropiada para la tarea.
        NEXUS decide qué herramienta usar según el tipo de tarea.
        """
        task_lower = task.lower()

        # Si se especificó una gem, usar su modelo asociado
        if gem != "auto":
            gem_to_tool = {
                "ayuda": "qwen_coder",
                "code": "qwen_coder",
                "coder": "qwen_coder",
                "opencode": "qwen_coder",
                "engineer": "qwen_coder",
                "codex": "qwen_coder",
                "scholar": "scholar_research",
                "creative": "gemma_creative",
                "design": "multimedia_design",
                "vision": "qwen_vision",
                "music": "music_generator",
                "prompter": "prompt_engineer",
                "producer": "producer_marketing",
                "architect": "architect_design",
                "analyst": "analyst_data",
                "debugger": "debugger_troubleshoot",
                "optimizer": "optimizer_performance",
                "tester": "tester_qa",
                "security": "security_audit",
                "devops": "devops_infra",
                "trainer": "trainer_education",
                "sage": "sage_memory",
                "biblioteca": "biblioteca_knowledge",
                "director": "deepseek_reason",
            }
            tool_name = gem_to_tool.get(gem, "qwen_coder")
            if tool_name in self.tools:
                return self.tools[tool_name]

        # Selección automática por keywords
        for tool in self.tools.values():
            if any(tag in task_lower for tag in tool.tags):
                return tool

        # Default: qwen_coder (estable, no delira)
        return self.tools.get("qwen_coder", list(self.tools.values())[0])

    async def execute(
        self,
        tool_name: str,
        user_message: str,
        context: str = "",
        images: Optional[List[str]] = None,
    ) -> Dict:
        """
        Ejecuta una herramienta de IA con un mensaje específico.
        La herramienta recibe un prompt acotado y devuelve un resultado estructurado.
        """
        tool = self.tools.get(tool_name)
        if not tool:
            return {"error": f"Tool '{tool_name}' not found", "available_tools": list(self.tools.keys())}

        # Construir mensajes con system prompt específico
        messages = [
            {"role": "system", "content": tool.system_prompt},
        ]

        # For chat tool, prepend identity directly to user message (more reliable for small models)
        final_message = user_message
        if tool_name == "nemotron_chat":
            final_message = f"Eres DirectorNexus. Responde como DirectorNexus.\n\n{user_message}"

        if context:
            messages.append({"role": "user", "content": f"Contexto:\n{context}"})

        messages.append({"role": "user", "content": final_message})

        start = datetime.now()

        try:
            # Cache Ollama availability: check once at first use
            if self._ollama_available is None:
                self._ollama_available = await self.ollama.is_available()

            # Usar Ollama solo para vision (necesita GPU local)
            if tool.role == "vision":
                if self._ollama_available:
                    response = await self.ollama.chat(
                        model=tool.model,
                        messages=messages,
                        options={
                            "temperature": tool.temperature,
                            "num_predict": tool.max_tokens,
                        },
                    )
                else:
                    raise Exception("Ollama no disponible para modelo de vision")
            else:
                # Usar OpenCode Zen directamente (modelo cloud rapido)
                zen_client = get_opencode_zen_client()
                if zen_client.is_configured:
                    # Usar modelo del usuario si fue seleccionado, sino el del tool
                    model_to_use = getattr(self, '_user_selected_model', None) or tool.model
                    logger.info(f"Usando OpenCode Zen: {model_to_use}")
                    response = await zen_client.chat(
                        model=model_to_use,
                        messages=messages,
                    )
                    # Normalizar formato de respuesta de Zen
                    if "content" in response and "success" in response:
                        response = {
                            "message": {"content": response["content"]},
                            "eval_count": response.get("tokens_used", 0),
                            "prompt_eval_count": 0,
                            "done": True,
                        }
                    elif "error" in response:
                        raise Exception(response["error"])
                else:
                    raise Exception("OPENCODE_API_KEY no configurada para Zen")

            duration = (datetime.now() - start).total_seconds() * 1000
            content = response.get("message", {}).get("content", "")
            tokens_used = response.get("eval_count", 0) + response.get("prompt_eval_count", 0)

            # Actualizar stats
            tool.call_count += 1
            tool.total_tokens += tokens_used
            tool.last_used = datetime.now().isoformat()

            # Hooks: DataCollector + ThreeLoop (Sprint 2)
            if self.data_collector:
                import asyncio
                asyncio.ensure_future(self.data_collector.collect_sample(
                    prompt=user_message, response=content,
                    category=tool.role, source=f"ai_tools/{tool_name}",
                ))
            if self.three_loop:
                import asyncio
                asyncio.ensure_future(self.three_loop.fast_loop.record(
                    task=user_message, response=content,
                    latency_ms=duration, score=7.0 if content else 0.0,
                    category=tool.role, model_used=tool.model,
                ))

            return {
                "success": True,
                "tool": tool_name,
                "model": tool.model,
                "content": content,
                "tokens_used": tokens_used,
                "duration_ms": duration,
                "done": response.get("done", False),
            }

        except Exception as e:
            logger.error(f"Tool execution error: {e}")
            return {
                "success": False,
                "tool": tool_name,
                "error": str(e),
                "fallback": "NEXUS no pudo procesar esta solicitud con la herramienta seleccionada.",
            }

    async def _call_vision_model(
        self,
        tool: AITool,
        messages: List[Dict],
        images: List[str],
    ) -> Dict:
        """Llama a un modelo de visión con imágenes"""
        # Construir mensaje con imágenes para qwen2.5vl
        last_msg = messages[-1]
        content_parts = []
        
        for img_path in images:
            content_parts.append({"type": "image_url", "image_url": {"url": img_path}})
        
        content_parts.append({"type": "text", "text": last_msg["content"]})
        messages[-1] = {"role": "user", "content": content_parts}

        return await self.ollama.chat(
            model=tool.model,
            messages=messages,
            options={"temperature": tool.temperature},
        )

    async def quick_response(
        self,
        task: str,
        gem: str = "auto",
        context: str = "",
    ) -> Dict:
        """
        Respuesta rápida: selecciona herramienta y ejecuta.
        Este es el método principal que usa el DirectorNexus.
        """
        tool = self.select_tool(task, gem)
        return await self.execute(tool.name, task, context)

    def get_available_tools(self) -> List[Dict]:
        """Lista todas las herramientas disponibles"""
        return [t.to_dict() for t in self.tools.values()]

    def get_registered_tools(self) -> List[Dict]:
        """Get tools registered via auto_register decorator."""
        return list(_tool_registry)

    def get_model_registry(self) -> Dict[str, Dict]:
        """Get the Canonical Model Registry."""
        return CanonicalModelRegistry.get_instance().get_all_models()

    def get_sanitized_tools(self, provider: str = "ollama") -> List[Dict]:
        """Lista herramientas con schemas sanitizados para el proveedor"""
        raw_tools = self.get_available_tools()
        return SchemaSanitizer.sanitize_tool_definitions(raw_tools, provider)

    def get_default_model(self) -> str:
        """Retorna el modelo por defecto"""
        return self.default_model

    def set_default_model(self, model: str):
        """Cambia el modelo por defecto y lo persiste para que sobreviva reinicios.
        Acepta CUALQUIER modelo (local Ollama o cloud Zen)."""
        old = self.default_model
        self.default_model = model
        self._user_selected_model = model
        logger.info(f"Default model changed: {old} -> {model}")
        # Persistir para que sobreviva reinicios
        try:
            from src.services.provider_service import _save_persisted_default_model
            _save_persisted_default_model(model)
        except Exception as e:
            logger.warning(f"Could not persist default_model: {e}")

    def get_stats(self) -> Dict:
        """Estadísticas de uso de herramientas"""
        total_calls = sum(t.call_count for t in self.tools.values())
        total_tokens = sum(t.total_tokens for t in self.tools.values())
        
        return {
            "total_calls": total_calls,
            "total_tokens": total_tokens,
            "tools": {name: t.to_dict() for name, t in self.tools.items()},
            "default_model": self.default_model,
        }

    async def _tool_view_file(self, path: str, offset: int = 0, limit: int = 2000) -> Dict:
        """Lee archivo con números de línea, paginación y detección de imágenes"""
        try:
            if not path:
                return {"error": "path is required"}
            
            if not os.path.isabs(path):
                path = os.path.abspath(path)
            
            if not os.path.exists(path):
                dir_path = os.path.dirname(path)
                base = os.path.basename(path)
                suggestions = []
                if os.path.isdir(dir_path):
                    for entry in os.listdir(dir_path):
                        if base.lower() in entry.lower() or entry.lower() in base.lower():
                            suggestions.append(os.path.join(dir_path, entry))
                            if len(suggestions) >= 3:
                                break
                msg = f"File not found: {path}"
                if suggestions:
                    msg += "\n\nDid you mean?\n" + "\n".join(suggestions)
                return {"error": msg}
            
            if os.path.isdir(path):
                return {"error": f"Path is a directory: {path}"}
            
            image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.svg', '.webp', '.ico'}
            ext = os.path.splitext(path)[1].lower()
            if ext in image_exts:
                return {"content": f"This is an image file ({ext}). Use vision model to process it.", "is_image": True, "image_type": ext}
            
            max_size = 250 * 1024
            file_size = os.path.getsize(path)
            if file_size > max_size:
                return {"error": f"File too large ({file_size} bytes). Max 250KB"}
            
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                all_lines = f.readlines()
            
            total_lines = len(all_lines)
            if offset < 0:
                offset = 0
            if limit <= 0:
                limit = 2000
            
            selected = all_lines[offset:offset + limit]
            content = "".join(selected)
            
            lines_with_nums = []
            for i, line in enumerate(selected):
                line_num = i + offset + 1
                lines_with_nums.append(f"{line_num:>6}|{line.rstrip()}")
            
            output = "<file>\n" + "\n".join(lines_with_nums) + "\n</file>"
            
            if offset + limit < total_lines:
                output += f"\n\n(File has {total_lines} total lines. Use offset={offset + limit} to read more.)"
            
            return {
                "success": True,
                "content": output,
                "path": path,
                "total_lines": total_lines,
                "lines_read": len(selected),
                "offset": offset,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_edit_file(self, path: str, old_string: str = "", new_string: str = "",
                               level: int = 1, **kwargs) -> Dict:
        """4-Level Diff Editing: from exact string replacement to semantic LLM edits.
        
        Levels:
          L1 - Exact Match: Replace exact old_string with new_string (original behavior)
          L2 - Line Match: Replace matching lines (ignoring whitespace)
          L3 - Context Match: Replace block using surrounding context lines
          L4 - Semantic Edit: LLM-assisted edit via description (requires ollama)
        """
        try:
            if not path:
                return {"error": "path is required"}
            if not os.path.isabs(path):
                path = os.path.abspath(path)

            is_new = not os.path.exists(path)
            if is_new:
                if not new_string:
                    return {"error": "File not found and no content provided to create it"}
                dir_path = os.path.dirname(path)
                if dir_path and not os.path.exists(dir_path):
                    os.makedirs(dir_path, exist_ok=True)
                with open(path, 'w', encoding='utf-8') as f:
                    f.write(new_string)
                return {"success": True, "content": f"File created: {path}", "action": "create", "level": level}

            if os.path.isdir(path):
                return {"error": f"Path is a directory: {path}"}

            with open(path, 'r', encoding='utf-8') as f:
                content = f.read()

            level = level or 1

            if level == 1:
                return await self._diff_level1(path, content, old_string, new_string)
            elif level == 2:
                return await self._diff_level2(path, content, old_string, new_string)
            elif level == 3:
                context_before = kwargs.get("context_before", "")
                context_after = kwargs.get("context_after", "")
                return await self._diff_level3(path, content, old_string, new_string,
                                                context_before, context_after)
            elif level == 4:
                edit_description = kwargs.get("edit_description", old_string or new_string)
                return await self._diff_level4(path, content, edit_description)
            else:
                return {"error": f"Invalid level: {level}. Use 1-4."}
        except Exception as e:
            return {"error": str(e)}

    async def _diff_level1(self, path: str, content: str, old_string: str, new_string: str) -> Dict:
        """L1: Exact match replacement."""
        if not old_string:
            return {"error": "old_string is required for level 1"}
        if old_string not in content:
            return {"error": "old_string not found in file"}
        if content.count(old_string) > 1:
            return {"error": "old_string appears multiple times. Provide more context or use level 3 (context match)"}
        new_content = content.replace(old_string, new_string, 1)
        if new_content == content:
            return {"error": "No changes. new_string is identical to current content"}
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return {
            "success": True, "path": path, "level": 1, "action": "edit",
            "additions": max(0, new_string.count('\n') - old_string.count('\n')),
            "removals": max(0, old_string.count('\n') - new_string.count('\n')),
            "chars_changed": len(old_string) + len(new_string),
        }

    async def _diff_level2(self, path: str, content: str, old_text: str, new_text: str) -> Dict:
        """L2: Line-based match (ignores whitespace differences)."""
        content_lines = content.split('\n')
        old_lines = [line.strip() for line in old_text.strip().split('\n')]
        match_count = 0
        match_start = -1
        for i in range(len(content_lines)):
            cl = content_lines[i].strip()
            if cl == old_lines[0]:
                if i + len(old_lines) <= len(content_lines):
                    match = True
                    for j, ol in enumerate(old_lines):
                        if content_lines[i + j].strip() != ol:
                            match = False
                            break
                    if match:
                        match_count += 1
                        if match_start == -1:
                            match_start = i
        if match_count == 0:
            return {"error": "old_text lines not found in file (whitespace-insensitive match failed)"}
        if match_count > 1:
            return {"error": f"Found {match_count} line matches. Provide more context or use level 3"}
        new_lines = new_text.split('\n')
        result = content_lines[:match_start] + new_lines + content_lines[match_start + len(old_lines):]
        new_content = '\n'.join(result)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return {
            "success": True, "path": path, "level": 2, "action": "edit",
            "additions": sum(1 for line in new_lines if line.strip() and not any(
                cl.strip() == line.strip() for cl in content_lines[match_start:match_start + len(old_lines)])),
            "removals": sum(1 for line in old_lines if line.strip() and not any(
                nl.strip() == line.strip() for nl in new_lines)),
        }

    async def _diff_level3(self, path: str, content: str, old_string: str, new_string: str,
                            context_before: str = "", context_after: str = "") -> Dict:
        """L3: Context-aware match. Uses surrounding lines for unique identification."""
        if not old_string:
            return {"error": "old_string is required"}
        lines = content.split('\n')
        old_lines = old_string.strip().split('\n')
        before_lines = [line.strip() for line in context_before.strip().split('\n')] if context_before else []
        after_lines = [line.strip() for line in context_after.strip().split('\n')] if context_after else []
        matches = []
        for i in range(len(lines) - len(old_lines) + 1):
            block = lines[i:i + len(old_lines)]
            if all(b.strip() == o for b, o in zip(block, old_lines)):
                if before_lines and i >= len(before_lines):
                    ctx_before = [line.strip() for line in lines[i - len(before_lines):i]]
                    if ctx_before != before_lines:
                        continue
                if after_lines and i + len(old_lines) + len(after_lines) <= len(lines):
                    ctx_after = [line.strip() for line in lines[i + len(old_lines):i + len(old_lines) + len(after_lines)]]
                    if ctx_after != after_lines:
                        continue
                matches.append(i)
        if len(matches) == 0:
            return {"error": "old_string not found even with context"}
        if len(matches) > 1:
            return {"error": f"Still ambiguous ({len(matches)} matches). Provide more context lines"}
        result = lines[:matches[0]] + new_string.split('\n') + lines[matches[0] + len(old_lines):]
        new_content = '\n'.join(result)
        with open(path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return {
            "success": True, "path": path, "level": 3, "action": "edit",
            "lines_changed": len(old_lines),
        }

    async def _diff_level4(self, path: str, content: str, edit_description: str) -> Dict:
        """L4: LLM-assisted semantic edit. Uses Ollama to interpret the edit description."""
        try:
            import httpx
            prompt = (
                f"Edit the following file content according to this instruction:\n\n"
                f"INSTRUCTION: {edit_description}\n\n"
                f"CURRENT CONTENT:\n```\n{content[:15000]}\n```\n\n"
                f"Respond ONLY with the complete edited file content inside a code block.\n"
                f"Do NOT include explanations. Preserve all parts not mentioned in the instruction."
            )
            async with httpx.AsyncClient(timeout=60.0) as client:
                resp = await client.post(
                    "http://localhost:11434/api/generate",
                    json={
                        "model": "qwen2.5:0.5b",
                        "prompt": prompt,
                        "stream": False,
                        "options": {"temperature": 0.2, "num_predict": 16384},
                    },
                )
                if resp.status_code == 200:
                    response_text = resp.json().get("response", "")
                    import re
                    code_match = re.search(r"```(?:\w+)?\n(.*?)```", response_text, re.DOTALL)
                    new_content = code_match.group(1).strip() if code_match else response_text.strip()
                    if new_content and new_content != content:
                        with open(path, 'w', encoding='utf-8') as f:
                            f.write(new_content)
                        return {
                            "success": True, "path": path, "level": 4, "action": "semantic_edit",
                            "chars_changed": abs(len(new_content) - len(content)),
                            "description": edit_description[:100],
                        }
                    return {"error": "LLM returned no meaningful changes"}
            return {"error": "Ollama request failed"}
        except Exception as e:
            return {"error": f"Level 4 (LLM edit) failed: {e}"}

    async def _tool_patch_files(self, patch_text: str) -> Dict:
        """Aplica patch coordinado a múltiples archivos"""
        try:
            if not patch_text:
                return {"error": "patch_text is required"}
            
            lines = patch_text.strip().split('\n')
            if not patch_text.startswith('*** Begin Patch') or not patch_text.endswith('*** End Patch'):
                return {"error": "Patch must start with '*** Begin Patch' and end with '*** End Patch'"}
            
            current_file = None
            current_action = None
            file_changes: Dict[str, Dict] = {}
            pending_lines = []
            
            i = 0
            while i < len(lines):
                line = lines[i]
                i += 1
                
                if line.startswith('*** Begin Patch') or line.startswith('*** End Patch'):
                    continue
                
                if line.startswith('*** Update File:'):
                    if current_file and pending_lines:
                        file_changes[current_file] = {"action": current_action, "lines": pending_lines[:]}
                    current_file = line[len('*** Update File:'):].strip()
                    current_action = "update"
                    pending_lines = []
                elif line.startswith('*** Add File:'):
                    if current_file and pending_lines:
                        file_changes[current_file] = {"action": current_action, "lines": pending_lines[:]}
                    current_file = line[len('*** Add File:'):].strip()
                    current_action = "add"
                    pending_lines = []
                elif line.startswith('*** Delete File:'):
                    if current_file and pending_lines:
                        file_changes[current_file] = {"action": current_action, "lines": pending_lines[:]}
                    current_file = line[len('*** Delete File:'):].strip()
                    current_action = "delete"
                    pending_lines = []
                else:
                    if current_file:
                        pending_lines.append(line)
            
            if current_file and pending_lines:
                file_changes[current_file] = {"action": current_action, "lines": pending_lines[:]}
            
            changed_files = []
            total_additions = 0
            total_removals = 0
            
            for file_path, change in file_changes.items():
                if not os.path.isabs(file_path):
                    file_path = os.path.abspath(file_path)
                
                action = change["action"]
                patch_lines = change["lines"]
                
                if action == "delete":
                    if os.path.exists(file_path):
                        os.remove(file_path)
                        changed_files.append(file_path)
                        total_removals += 1
                    continue
                
                if action == "add":
                    if os.path.exists(file_path):
                        return {"error": f"File already exists: {file_path}"}
                    dir_path = os.path.dirname(file_path)
                    if dir_path:
                        os.makedirs(dir_path, exist_ok=True)
                    content = "\n".join(line[1:] if line.startswith('+') else line for line in patch_lines if line.startswith('+'))
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(content)
                    changed_files.append(file_path)
                    total_additions += content.count('\n') + 1
                    continue
                
                if action == "update":
                    if not os.path.exists(file_path):
                        return {"error": f"File not found: {file_path}"}
                    
                    with open(file_path, 'r', encoding='utf-8') as f:
                        old_content = f.read()
                    
                    old_lines_list = old_content.split('\n')
                    new_lines_list = []
                    skip_until_next_hunk = False
                    
                    j = 0
                    while j < len(patch_lines):
                        pl = patch_lines[j]
                        if pl.startswith('@@'):
                            j += 1
                            continue
                        if pl.startswith('-'):
                            j += 1
                            continue
                        if pl.startswith('+'):
                            new_lines_list.append(pl[1:])
                            j += 1
                            continue
                        if pl.startswith(' '):
                            new_lines_list.append(pl[1:])
                            j += 1
                            continue
                        new_lines_list.append(pl)
                        j += 1
                    
                    new_content = "\n".join(new_lines_list)
                    if not new_content.strip():
                        new_content = old_content
                        for pl in patch_lines:
                            if pl.startswith('-'):
                                removed = pl[1:]
                                if removed in new_content:
                                    new_content = new_content.replace(removed, '', 1)
                    
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(new_content)
                    
                    changed_files.append(file_path)
                    total_additions += max(0, sum(1 for line in patch_lines if line.startswith('+')))
                    total_removals += max(0, sum(1 for line in patch_lines if line.startswith('-')))
            
            result = f"Patch applied. {len(changed_files)} files changed, {total_additions} additions, {total_removals} removals"
            return {
                "success": True,
                "content": result,
                "files_changed": changed_files,
                "additions": total_additions,
                "removals": total_removals,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_grep(self, pattern: str, path: str = "", include: str = "", literal: bool = False) -> Dict:
        """Busca contenido en archivos usando regex o texto literal"""
        try:
            if not pattern:
                return {"error": "pattern is required"}
            
            search_path = path if path else os.getcwd()
            if not os.path.isabs(search_path):
                search_path = os.path.abspath(search_path)
            
            if not os.path.isdir(search_path):
                return {"error": f"Directory not found: {search_path}"}
            
            search_pattern = re.escape(pattern) if literal else pattern
            
            try:
                re.compile(search_pattern)
            except re.error as e:
                return {"error": f"Invalid regex pattern: {e}"}
            
            max_matches = 100
            matches = []
            
            try:
                rg_result = subprocess.run(
                    ['rg', '--line-number', '--no-heading', '--max-count', str(max_matches),
                     search_pattern, search_path] + (['--glob', include] if include else []),
                    capture_output=True, text=True, timeout=30
                )
                if rg_result.returncode == 0:
                    for line in rg_result.stdout.strip().split('\n'):
                        if line:
                            parts = line.split(':', 2)
                            if len(parts) >= 3:
                                matches.append({
                                    "path": parts[0],
                                    "line": int(parts[1]),
                                    "text": parts[2][:200]
                                })
                    return {
                        "success": True,
                        "content": f"Found {len(matches)} matches\n" + "\n".join(
                            f"{m['path']}:{m['line']}: {m['text']}" for m in matches[:50]
                        ),
                        "count": len(matches),
                        "truncated": len(matches) >= max_matches,
                    }
            except (subprocess.TimeoutExpired, FileNotFoundError):
                pass
            
            include_re = None
            if include:
                glob_pattern = include.replace('.', '\\.').replace('*', '.*').replace('?', '.')
                include_re = re.compile(glob_pattern)
            
            for root, dirs, files in os.walk(search_path):
                dirs[:] = [d for d in dirs if not d.startswith('.')]
                for fname in files:
                    if fname.startswith('.'):
                        continue
                    if include_re and not include_re.match(fname):
                        continue
                    
                    fpath = os.path.join(root, fname)
                    try:
                        with open(fpath, 'r', encoding='utf-8', errors='replace') as f:
                            for line_num, line in enumerate(f, 1):
                                if re.search(search_pattern, line):
                                    matches.append({
                                        "path": fpath,
                                        "line": line_num,
                                        "text": line.strip()[:200]
                                    })
                                    if len(matches) >= max_matches:
                                        break
                    except (PermissionError, OSError):
                        continue
                    if len(matches) >= max_matches:
                        break
            
            if not matches:
                return {"success": True, "content": "No matches found", "count": 0}
            
            output = f"Found {len(matches)} matches\n"
            current_file = ""
            for m in matches[:50]:
                if m["path"] != current_file:
                    if current_file:
                        output += "\n"
                    current_file = m["path"]
                    output += f"{m['path']}:\n"
                output += f"  Line {m['line']}: {m['text']}\n"
            
            if len(matches) > 50:
                output += f"\n... ({len(matches) - 50} more matches)"
            
            return {
                "success": True,
                "content": output,
                "count": len(matches),
                "truncated": len(matches) >= max_matches,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_glob(self, pattern: str, path: str = "") -> Dict:
        """Busca archivos por patrón de nombre"""
        try:
            if not pattern:
                return {"error": "pattern is required"}
            
            search_path = path if path else os.getcwd()
            if not os.path.isabs(search_path):
                search_path = os.path.abspath(search_path)
            
            if not os.path.isdir(search_path):
                return {"error": f"Directory not found: {search_path}"}
            
            search_pattern = os.path.join(search_path, pattern)
            files = glob_module.glob(search_pattern, recursive=True)
            
            files = [f for f in files if os.path.isfile(f) and not os.path.basename(f).startswith('.')]
            files.sort(key=lambda f: os.path.getmtime(f), reverse=True)
            
            max_results = 100
            truncated = len(files) > max_results
            if truncated:
                files = files[:max_results]
            
            if not files:
                return {"success": True, "content": "No files found", "count": 0}
            
            output = "\n".join(files)
            if truncated:
                output += f"\n\n(Results truncated. {max_results} of {len(files)}+ files shown.)"
            
            return {
                "success": True,
                "content": output,
                "count": len(files),
                "truncated": truncated,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_git_status(self, path: str = "") -> Dict:
        """Git status - muestra estado del repositorio"""
        try:
            cwd = path if path else os.getcwd()
            if not os.path.isabs(cwd):
                cwd = os.path.abspath(cwd)
            
            result = subprocess.run(
                ["git", "status", "--porcelain", "-b"],
                cwd=cwd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return {"error": f"Not a git repository: {result.stderr.strip()}"}
            
            lines = result.stdout.strip().split('\n')
            branch = lines[0] if lines else "unknown"
            changes = [line for line in lines[1:] if line.strip()] if len(lines) > 1 else []
            
            staged = [line[3:] for line in changes if line[0] in ('A', 'M', 'D', 'R', 'C') and line[1] != ' ']
            unstaged = [line[3:] for line in changes if line[0] == ' ' and line[1] != ' ']
            untracked = [line[3:] for line in changes if line[:2] == '??']
            
            output = f"Branch: {branch}\n"
            if staged:
                output += f"\nStaged ({len(staged)}):\n" + "\n".join(f"  {f}" for f in staged)
            if unstaged:
                output += f"\nModified ({len(unstaged)}):\n" + "\n".join(f"  {f}" for f in unstaged)
            if untracked:
                output += f"\nUntracked ({len(untracked)}):\n" + "\n".join(f"  {f}" for f in untracked)
            if not staged and not unstaged and not untracked:
                output += "\nNothing to commit, working tree clean"
            
            return {
                "success": True,
                "content": output,
                "branch": branch,
                "staged": staged,
                "unstaged": unstaged,
                "untracked": untracked,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_git_diff(self, path: str = "", staged: bool = False, file_path: str = "") -> Dict:
        """Git diff - muestra cambios no commiteados"""
        try:
            cwd = path if path else os.getcwd()
            if not os.path.isabs(cwd):
                cwd = os.path.abspath(cwd)
            
            cmd = ["git", "diff"]
            if staged:
                cmd.append("--staged")
            if file_path:
                cmd.append("--")
                cmd.append(file_path)
            
            result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=30)
            if result.returncode != 0:
                return {"error": result.stderr.strip()}
            
            diff = result.stdout.strip()
            if not diff:
                return {"success": True, "content": "No changes", "has_changes": False}
            
            lines = diff.split('\n')
            stats = {"additions": 0, "deletions": 0}
            for line in lines:
                if line.startswith('+') and not line.startswith('+++'):
                    stats["additions"] += 1
                elif line.startswith('-') and not line.startswith('---'):
                    stats["deletions"] += 1
            
            max_lines = 200
            if len(lines) > max_lines:
                diff = '\n'.join(lines[:max_lines]) + f"\n\n... ({len(lines) - max_lines} more lines)"
            
            return {
                "success": True,
                "content": diff,
                "has_changes": True,
                "additions": stats["additions"],
                "deletions": stats["deletions"],
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_git_log(self, path: str = "", limit: int = 10) -> Dict:
        """Git log - historial de commits"""
        try:
            cwd = path if path else os.getcwd()
            if not os.path.isabs(cwd):
                cwd = os.path.abspath(cwd)
            
            result = subprocess.run(
                ["git", "log", f"-{limit}", "--oneline", "--decorate"],
                cwd=cwd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return {"error": result.stderr.strip()}
            
            commits = result.stdout.strip().split('\n')
            output = f"Last {len(commits)} commits:\n" + "\n".join(f"  {c}" for c in commits)
            
            return {
                "success": True,
                "content": output,
                "count": len(commits),
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_terminal_create(self, name: str = "", cwd: str = "") -> Dict:
        """Crea una terminal persistente usando PersistentShell singleton"""
        try:
            from src.tools.persistent_shell import PersistentShell
            session_id = name or "default"
            work_dir = cwd if cwd else os.getcwd()
            shell = PersistentShell.get_instance(work_dir)
            return {
                "success": True,
                "session_id": session_id,
                "cwd": work_dir,
                "pid": shell._proc.pid if shell._proc else None,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_terminal_send(self, session_id: str, text: str, wait_ms: int = 2000) -> Dict:
        """Envia comando a terminal persistente usando PersistentShell.exec()"""
        try:
            from src.tools.persistent_shell import PersistentShell
            shell = PersistentShell.get_instance()
            timeout_ms = max(wait_ms, 1000)
            stdout, stderr, exit_code, interrupted = await shell.exec(text, timeout_ms)
            output = stdout
            if stderr:
                output += "\n" + stderr
            max_output = 5000
            if len(output) > max_output:
                output = output[:max_output] + f"\n... ({len(output) - max_output} chars truncated)"
            return {
                "success": exit_code == 0 and not interrupted,
                "content": output,
                "session_id": session_id,
                "exit_code": exit_code,
                "running": shell.is_alive,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_terminal_close(self, session_id: str) -> Dict:
        """Cierra terminal persistente"""
        try:
            from src.tools.persistent_shell import PersistentShell
            shell = PersistentShell.get_instance()
            shell.close()
            return {"success": True, "content": f"Terminal '{session_id}' closed"}
        except Exception as e:
            return {"error": str(e)}

    async def _tool_lsp_diagnostics(self, path: str = "", language: str = "") -> Dict:
        """Analiza archivo en busca de errores sintacticos (sin LSP server, usa parser local)"""
        try:
            if not path:
                return {"error": "path is required"}
            
            if not os.path.isabs(path):
                path = os.path.abspath(path)
            
            if not os.path.exists(path):
                return {"error": f"File not found: {path}"}
            
            ext = os.path.splitext(path)[1].lower()
            diagnostics = []
            
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            
            if ext == '.py':
                import py_compile
                try:
                    py_compile.compile(path, doraise=True)
                except py_compile.PyCompileError as e:
                    error_msg = str(e)
                    line_match = re.search(r'line (\d+)', error_msg)
                    line_num = int(line_match.group(1)) if line_match else 0
                    diagnostics.append({
                        "severity": "error",
                        "line": line_num,
                        "message": error_msg.split('\n')[0],
                    })
            elif ext in ('.json',):
                import json
                try:
                    with open(path, 'r') as f:
                        json.load(f)
                except json.JSONDecodeError as e:
                    diagnostics.append({
                        "severity": "error",
                        "line": e.lineno,
                        "message": str(e.msg),
                    })
            elif ext in ('.yaml', '.yml'):
                try:
                    import yaml
                    with open(path, 'r') as f:
                        yaml.safe_load(f)
                except yaml.YAMLError as e:
                    line = getattr(e, 'problem_mark', None)
                    line_num = line.line + 1 if line else 0
                    diagnostics.append({
                        "severity": "error",
                        "line": line_num,
                        "message": str(e),
                    })
            
            if diagnostics:
                output = f"Found {len(diagnostics)} issue(s):\n"
                for d in diagnostics:
                    output += f"  Line {d['line']}: [{d['severity']}] {d['message']}\n"
            else:
                output = "No issues found"
            
            return {
                "success": True,
                "content": output,
                "diagnostics": diagnostics,
                "count": len(diagnostics),
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_web_fetch(self, url: str, timeout: int = 120) -> Dict:
        """Fetch URL content and convert HTML to Markdown"""
        try:
            from src.tools.web_fetch import web_fetch
            return await web_fetch(url, timeout)
        except Exception as e:
            return {"error": str(e)}

    async def _tool_web_search(self, query: str, max_results: int = 5) -> Dict:
        """Web search via WebResearcher (DDG -> Brave -> agent-browser -> Playwright)."""
        if not query or not query.strip():
            return {"success": False, "error": "query is required", "tool": "web_search"}
        try:
            from src.core.web_researcher import WebResearcher
            researcher = WebResearcher()
            results = await researcher.search(query.strip(), max_results=max_results)
            if not results:
                return {
                    "success": False,
                    "tool": "web_search",
                    "query": query,
                    "results": [],
                    "error": "No results (all backends failed or returned empty)",
                }
            return {
                "success": True,
                "tool": "web_search",
                "query": query,
                "count": len(results),
                "results": results,
                "content": "\n\n".join(
                    f"- **{r.get('title', '(sin titulo)')[:120]}**\n  {r.get('url', '')}\n  {r.get('snippet', '')[:300]}"
                    for r in results
                ),
            }
        except Exception as e:
            return {"success": False, "error": str(e), "tool": "web_search", "query": query}

    async def _tool_code_search(self, query: str, path: str = "", include: str = "", limit: int = 20) -> Dict:
        """Search code across files using grep/glob"""
        try:
            from src.tools.code_search import code_search
            return await code_search(query, path, include, limit)
        except Exception as e:
            return {"error": str(e)}

    async def _tool_git_blame(self, path: str, file_path: str) -> Dict:
        """Git blame - muestra quien hizo cada linea"""
        try:
            cwd = path if path else os.getcwd()
            if not os.path.isabs(cwd):
                cwd = os.path.abspath(cwd)
            
            if not file_path:
                return {"error": "file_path is required"}
            
            result = subprocess.run(
                ["git", "blame", "-L", "1,50", file_path],
                cwd=cwd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                return {"error": result.stderr.strip()}
            
            lines = result.stdout.strip().split('\n')
            output = f"Git blame (first 50 lines) for {file_path}:\n" + "\n".join(f"  {line}" for line in lines[:50])
            
            return {
                "success": True,
                "content": output,
                "file": file_path,
                "lines": len(lines),
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_lsp_symbols(self, path: str) -> Dict:
        """Lista funciones/clases/variables en un archivo Python"""
        try:
            if not path:
                return {"error": "path is required"}
            
            if not os.path.isabs(path):
                path = os.path.abspath(path)
            
            if not os.path.exists(path):
                return {"error": f"File not found: {path}"}
            
            ext = os.path.splitext(path)[1].lower()
            symbols = []
            
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                lines = f.readlines()
            
            if ext == '.py':
                for i, line in enumerate(lines, 1):
                    stripped = line.strip()
                    if stripped.startswith('def ') or stripped.startswith('async def '):
                        name = stripped.split('(')[0].replace('def ', '').replace('async def ', '')
                        indent = len(line) - len(line.lstrip())
                        symbols.append({"name": name, "type": "function", "line": i, "indent": indent})
                    elif stripped.startswith('class '):
                        name = stripped.split('(')[0].replace('class ', '')
                        symbols.append({"name": name, "type": "class", "line": i})
            elif ext in ('.js', '.ts'):
                for i, line in enumerate(lines, 1):
                    stripped = line.strip()
                    if re.match(r'(export\s+)?(async\s+)?function\s+', stripped):
                        name = re.search(r'function\s+(\w+)', stripped)
                        if name:
                            symbols.append({"name": name.group(1), "type": "function", "line": i})
                    elif re.match(r'(export\s+)?class\s+', stripped):
                        name = re.search(r'class\s+(\w+)', stripped)
                        if name:
                            symbols.append({"name": name.group(1), "type": "class", "line": i})
            
            if symbols:
                output = f"Found {len(symbols)} symbols in {os.path.basename(path)}:\n"
                for s in symbols:
                    indent = "  " * (s.get("indent", 0) // 4) if s.get("indent") else ""
                    output += f"  Line {s['line']}: {s['type']} {s['name']}\n"
            else:
                output = "No symbols found"
            
            return {
                "success": True,
                "content": output,
                "symbols": symbols,
                "count": len(symbols),
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_list_directory(self, path: str = "") -> Dict:
        """Lista archivos y carpetas en cualquier path del sistema."""
        try:
            target = path if path else os.getcwd()
            if not os.path.isabs(target):
                target = os.path.abspath(target)
            if not os.path.isdir(target):
                return {"error": f"Directory not found: {target}"}
            entries = []
            try:
                for entry in sorted(os.listdir(target)):
                    full = os.path.join(target, entry)
                    try:
                        is_dir = os.path.isdir(full)
                        size = os.path.getsize(full) if not is_dir else 0
                        entries.append({"name": entry, "type": "dir" if is_dir else "file", "size": size})
                    except Exception:
                        entries.append({"name": entry, "type": "unknown"})
            except PermissionError:
                return {"error": f"Permission denied: {target}"}
            output = f"Contents of {target}:\n"
            for e in entries:
                output += f"  {'📁' if e['type'] == 'dir' else '📄'} {e['name']}"
                if e.get('size'):
                    output += f" ({e['size']} bytes)"
                output += "\n"
            return {"success": True, "content": output, "entries": entries, "count": len(entries)}
        except Exception as e:
            return {"error": str(e)}

    async def _tool_workspace_folders(self) -> Dict:
        """Lista carpetas del workspace actual"""
        try:
            cwd = os.getcwd()
            entries = []
            for entry in os.listdir(cwd):
                full_path = os.path.join(cwd, entry)
                if os.path.isdir(full_path) and not entry.startswith('.'):
                    file_count = sum(1 for _ in os.walk(full_path))
                    entries.append({"name": entry, "path": full_path, "type": "folder", "file_count": file_count})
            
            output = f"Workspace folders in {cwd}:\n"
            for e in entries:
                output += f"  📁 {e['name']} ({e['file_count']} files)\n"
            
            return {
                "success": True,
                "content": output,
                "folders": entries,
                "count": len(entries),
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_find_files(self, pattern: str, path: str = "", use_ignore: bool = True) -> Dict:
        """Busca archivos respetando .gitignore"""
        try:
            search_path = path if path else os.getcwd()
            if not os.path.isabs(search_path):
                search_path = os.path.abspath(search_path)
            
            if not os.path.isdir(search_path):
                return {"error": f"Directory not found: {search_path}"}
            
            import fnmatch
            ignore_patterns = []
            
            if use_ignore:
                gitignore = os.path.join(search_path, '.gitignore')
                if os.path.exists(gitignore):
                    with open(gitignore, 'r') as f:
                        ignore_patterns = [line.strip() for line in f if line.strip() and not line.startswith('#')]
            
            def should_ignore(p):
                for pat in ignore_patterns:
                    if fnmatch.fnmatch(os.path.basename(p), pat):
                        return True
                    if fnmatch.fnmatch(p, pat):
                        return True
                return False
            
            results = []
            for root, dirs, files in os.walk(search_path):
                dirs[:] = [d for d in dirs if not d.startswith('.') and not should_ignore(os.path.join(root, d))]
                for f in files:
                    full = os.path.join(root, f)
                    if not should_ignore(full) and fnmatch.fnmatch(f, pattern):
                        results.append(full)
            
            results.sort(key=lambda f: os.path.getmtime(f), reverse=True)
            
            max_results = 100
            truncated = len(results) > max_results
            if truncated:
                results = results[:max_results]
            
            output = "\n".join(results) if results else "No files found"
            if truncated:
                output += f"\n\n({max_results} of {len(results)}+ files shown)"
            
            return {
                "success": True,
                "content": output,
                "count": len(results),
                "truncated": truncated,
            }
        except Exception as e:
            return {"error": str(e)}

    async def _tool_git_commit(self, path: str, message: str) -> Dict:
        """Git commit - crea un commit con mensaje"""
        try:
            cwd = path if path else os.getcwd()
            if not os.path.isabs(cwd):
                cwd = os.path.abspath(cwd)
            
            # Add all changes
            subprocess.run(["git", "add", "."], cwd=cwd, capture_output=True, timeout=30)
            
            # Commit
            result = subprocess.run(
                ["git", "commit", "-m", message],
                cwd=cwd, capture_output=True, text=True, timeout=30
            )
            if result.returncode != 0:
                if "nothing to commit" in result.stdout.lower():
                    return {"success": True, "content": "Nothing to commit, working tree clean", "committed": False}
                return {"error": result.stderr.strip()}
            
            return {
                "success": True,
                "content": result.stdout.strip(),
                "committed": True,
            }
        except Exception as e:
            return {"error": str(e)}

    # ─── CDP Browser Tools ────────────────────────────────────────────

    def _get_cdp_browser(self):
        if not hasattr(self, '_cdp_browser') or self._cdp_browser is None:
            from src.tools.cdp_browser import CdpBrowser
            self._cdp_browser = CdpBrowser()
        return self._cdp_browser

    async def _tool_browser(self, tool_name: str, params: Dict) -> Dict:
        browser = self._get_cdp_browser()
        try:
            if tool_name == "browser_navigate":
                url = params.get("url", "")
                if not url:
                    return {"error": "url required"}
                if browser.get_state().status != "connected":
                    ok = await browser.connect()
                    if not ok:
                        page_id = params.get("page_id", "")
                        result = await browser.navigate_page(url, page_id)
                else:
                    page_id = params.get("page_id", "")
                    if not page_id and not browser.get_state().pages:
                        result = await browser.new_page(url)
                    else:
                        result = await browser.navigate_page(url, page_id)
                if not result.success:
                    result = await browser.new_page(url)
                if result.success:
                    snap = await browser.get_accessibility_snapshot(
                        result.data.get("page_id", "") if isinstance(result.data, dict) else "")
                    return {
                        "success": True,
                        "url": url,
                        "page_id": result.data.get("page_id", "") if isinstance(result.data, dict) else "",
                        "snapshot": snap.data.get("text", "") if snap.success else "",
                        "duration_ms": result.duration_ms,
                    }
                return {"error": result.error}

            elif tool_name == "browser_snapshot":
                if browser.get_state().status != "connected":
                    ok = await browser.connect()
                    if not ok:
                        return {"error": "No browser connected"}
                result = await browser.get_accessibility_snapshot(
                    params.get("page_id", ""),
                    params.get("verbose", False))
                if result.success:
                    data = result.data
                    return {
                        "success": True,
                        "text": data.get("text", ""),
                        "node_count": data.get("node_count", 0),
                        "duration_ms": result.duration_ms,
                    }
                return {"error": result.error}

            elif tool_name == "browser_click":
                if browser.get_state().status != "connected":
                    ok = await browser.connect()
                    if not ok:
                        return {"error": "No browser connected"}
                result = await browser.click(
                    params.get("selector", ""),
                    params.get("x", 0),
                    params.get("y", 0),
                    params.get("page_id", ""))
                if result.success:
                    return {"success": True, "duration_ms": result.duration_ms}
                return {"error": result.error}

            elif tool_name == "browser_type":
                if browser.get_state().status != "connected":
                    ok = await browser.connect()
                    if not ok:
                        return {"error": "No browser connected"}
                result = await browser.type_text(
                    params.get("text", ""),
                    params.get("selector", ""),
                    params.get("page_id", ""))
                if result.success:
                    return {"success": True, "chars": result.data.get("chars", 0),
                            "duration_ms": result.duration_ms}
                return {"error": result.error}

            elif tool_name == "browser_evaluate":
                if browser.get_state().status != "connected":
                    ok = await browser.connect()
                    if not ok:
                        return {"error": "No browser connected"}
                result = await browser.evaluate_script(
                    params.get("script", ""),
                    params.get("page_id", ""))
                if result.success:
                    return {"success": True, "result": result.data,
                            "duration_ms": result.duration_ms}
                return {"error": result.error}

            elif tool_name == "browser_screenshot":
                if browser.get_state().status != "connected":
                    ok = await browser.connect()
                    if not ok:
                        return {"error": "No browser connected"}
                result = await browser.take_screenshot(
                    params.get("page_id", ""),
                    params.get("format", "png"),
                    params.get("quality", 80),
                    params.get("full_page", False))
                if result.success:
                    return {"success": True, **result.data,
                            "duration_ms": result.duration_ms}
                return {"error": result.error}

            elif tool_name == "browser_new_page":
                ok = await browser.connect()
                if not ok:
                    return {"error": "Cannot start browser"}
                result = await browser.new_page(params.get("url", "about:blank"))
                if result.success:
                    return {"success": True, "page_id": result.data.get("page_id", ""),
                            "url": result.data.get("url", ""),
                            "duration_ms": result.duration_ms}
                return {"error": result.error}

            elif tool_name == "browser_close_page":
                if browser.get_state().status != "connected":
                    return {"error": "No browser connected"}
                result = await browser.close_page(params.get("page_id", ""))
                if result.success:
                    return {"success": True, "duration_ms": result.duration_ms}
                return {"error": result.error}

            elif tool_name == "browser_performance":
                if browser.get_state().status != "connected":
                    ok = await browser.connect()
                    if not ok:
                        return {"error": "No browser connected"}
                if params.get("start", False):
                    result = await browser.start_performance_trace(params.get("page_id", ""))
                    return {"success": result.success, "action": "trace_started",
                            "duration_ms": result.duration_ms}
                else:
                    result = await browser.stop_performance_trace(params.get("page_id", ""))
                    return {"success": result.success, "action": "trace_stopped",
                            "duration_ms": result.duration_ms}

        except Exception as e:
            return {"error": f"Browser tool error: {e}"}
        return {"error": f"Unknown browser tool: {tool_name}"}

    async def execute_tool_call(self, tool_name: str, params: Dict) -> Dict:
        """Ejecuta una llamada a herramienta builtin con tracking de monitoreo"""
        import time as _time
        _start = _time.time()
        try:
            if tool_name == "read_file":
                res = self.workspace.read_file(
                    params.get("path", ""),
                    params.get("offset", 1),
                    params.get("limit", 2000)
                )
                return res
            elif tool_name == "view_file":
                res = await self._tool_view_file(
                    params.get("path", ""),
                    params.get("offset", 0),
                    params.get("limit", 2000)
                )
                return res
            elif tool_name == "write_file":
                res = self.workspace.write_file(
                    params.get("path", ""),
                    params.get("content", ""),
                    params.get("mkdirp", True)
                )
                return res
            elif tool_name == "create_file":
                res = self.workspace.create_file(
                    params.get("filename", ""),
                    params.get("location", ""),
                    params.get("content", ""),
                    params.get("overwrite", False)
                )
                return res
            elif tool_name == "edit_file":
                res = await self._tool_edit_file(
                    params.get("path", ""),
                    params.get("old_string", ""),
                    params.get("new_string", "")
                )
                return res
            elif tool_name == "patch_files":
                res = await self._tool_patch_files(
                    params.get("patch_text", "")
                )
                return res
            elif tool_name == "list_dir":
                res = await self._tool_list_directory(
                    params.get("path", ""),
                )
                return res
            elif tool_name == "grep_content":
                res = await self._tool_grep(
                    params.get("pattern", ""),
                    params.get("path", ""),
                    params.get("include", ""),
                    params.get("literal", False)
                )
                return res
            elif tool_name == "glob_files":
                res = await self._tool_glob(
                    params.get("pattern", ""),
                    params.get("path", "")
                )
                return res
            elif tool_name == "execute_command":
                res = await self.executor.execute_command(
                    params.get("command", ""),
                    params.get("cwd"),
                    params.get("timeout", 60)
                )
                return res
            elif tool_name == "parse_file":
                from src.tools.builtin import ParseTools
                res = ParseTools().parse_file(params.get("path", ""))
                return res
            elif tool_name == "git_status":
                res = await self._tool_git_status(params.get("path", ""))
                return res
            elif tool_name == "git_diff":
                res = await self._tool_git_diff(
                    params.get("path", ""),
                    params.get("staged", False),
                    params.get("file", "")
                )
                return res
            elif tool_name == "git_log":
                res = await self._tool_git_log(
                    params.get("path", ""),
                    params.get("limit", 10)
                )
                return res
            elif tool_name == "terminal_create":
                res = await self._tool_terminal_create(
                    params.get("name", ""),
                    params.get("cwd", "")
                )
                return res
            elif tool_name == "terminal_send":
                res = await self._tool_terminal_send(
                    params.get("session_id", ""),
                    params.get("text", ""),
                    params.get("wait_ms", 2000)
                )
                return res
            elif tool_name == "terminal_close":
                res = await self._tool_terminal_close(
                    params.get("session_id", "")
                )
                return res
            elif tool_name == "lsp_diagnostics":
                res = await self._tool_lsp_diagnostics(
                    params.get("path", ""),
                    params.get("language", "")
                )
                return res
            elif tool_name == "web_fetch":
                res = await self._tool_web_fetch(
                    params.get("url", ""),
                    params.get("timeout", 120)
                )
                return res
            elif tool_name == "web_search":
                res = await self._tool_web_search(
                    params.get("query", ""),
                    int(params.get("max_results", 5)),
                )
                return res
            elif tool_name == "code_search":
                res = await self._tool_code_search(
                    params.get("query", ""),
                    params.get("path", ""),
                    params.get("include", ""),
                    params.get("limit", 20)
                )
                return res
            elif tool_name == "git_blame":
                res = await self._tool_git_blame(
                    params.get("path", ""),
                    params.get("file", "")
                )
                return res
            elif tool_name == "lsp_symbols":
                res = await self._tool_lsp_symbols(
                    params.get("path", "")
                )
                return res
            elif tool_name == "list_directory":
                res = await self._tool_list_directory(
                    params.get("path", "")
                )
                return res
            elif tool_name == "workspace_folders":
                res = await self._tool_workspace_folders()
                return res
            elif tool_name == "find_files":
                res = await self._tool_find_files(
                    params.get("pattern", ""),
                    params.get("path", ""),
                    params.get("use_ignore", True)
                )
                return res
            elif tool_name == "git_commit":
                res = await self._tool_git_commit(
                    params.get("path", ""),
                    params.get("message", "")
                )
                return res
            elif tool_name in ("browser_navigate", "browser_click", "browser_type",
                               "browser_snapshot", "browser_evaluate", "browser_screenshot",
                               "browser_new_page", "browser_close_page", "browser_performance"):
                res = await self._tool_browser(tool_name, params)
                return res
            else:
                res = {"error": f"Unknown tool: {tool_name}"}
                return res
        except Exception as e:
            res = {"error": str(e)}
            return res
        finally:
            _elapsed = (_time.time() - _start) * 1000
            if self._tool_monitor and 'res' in dir():
                self._tool_monitor.record_call(
                    tool=tool_name,
                    duration_ms=_elapsed,
                    tokens=0,
                    success=isinstance(res, dict) and "error" not in res,
                    error=res.get("error", "") if isinstance(res, dict) and "error" in res else "",
                    model_type="local",
                )

    def get_tool_definitions(self) -> List[Dict]:
        """Retorna definiciones de herramientas para el modelo"""
        return [
            {
                "name": "view_file",
                "description": "Lee el contenido de un archivo con números de línea y paginación. Usa offset para leer desde una línea específica.",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta absoluta del archivo"},
                    "offset": {"type": "integer", "description": "Línea inicial (0-based, default: 0)"},
                    "limit": {"type": "integer", "description": "Número de líneas (default: 2000)"}
                }
            },
            {
                "name": "edit_file",
                "description": "Reemplaza texto en un archivo. old_string debe ser único y coincidir exactamente con el contenido del archivo incluyendo indentación.",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta absoluta del archivo"},
                    "old_string": {"type": "string", "description": "Texto a reemplazar (debe ser único en el archivo)"},
                    "new_string": {"type": "string", "description": "Nuevo texto"}
                }
            },
            {
                "name": "patch_files",
                "description": "Aplica un patch a múltiples archivos en una operación coordinada. Formato: *** Begin Patch\\n*** Update File: /path\\n@@ context\\n-remove\\n+add\\n*** End Patch",
                "parameters": {
                    "patch_text": {"type": "string", "description": "Texto completo del patch"}
                }
            },
            {
                "name": "grep_content",
                "description": "Busca contenido en archivos usando regex. Retorna archivos con coincidencias y líneas matching.",
                "parameters": {
                    "pattern": {"type": "string", "description": "Patrón regex a buscar"},
                    "path": {"type": "string", "description": "Directorio donde buscar (default: cwd)"},
                    "include": {"type": "string", "description": "Patrón de archivo para incluir (ej: '*.py')"},
                    "literal": {"type": "boolean", "description": "Tratar pattern como texto literal (default: false)"}
                }
            },
            {
                "name": "glob_files",
                "description": "Busca archivos por patrón de nombre (glob). No busca contenido, solo nombres.",
                "parameters": {
                    "pattern": {"type": "string", "description": "Patrón glob (ej: '**/*.py')"},
                    "path": {"type": "string", "description": "Directorio donde buscar (default: cwd)"}
                }
            },
            {
                "name": "read_file",
                "description": "Lee el contenido de un archivo con paginación. ACEPTA RUTAS ABSOLUTAS (C:\\, D:\\, /home, etc.) o relativas al workspace.",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta ABSOLUTA del archivo (ej: 'D:\\carpeta\\archivo.txt') o relativa al workspace. Si el usuario menciona una unidad (C:, D:) o path con separadores, USAR ABSOLUTA."},
                    "offset": {"type": "integer", "description": "Línea inicial (default: 1)"},
                    "limit": {"type": "integer", "description": "Número de líneas (default: 2000)"}
                }
            },
            {
                "name": "write_file",
                "description": "Escribe contenido a un archivo. ACEPTA RUTAS ABSOLUTAS (C:\\, D:\\, /home, etc.) o relativas al workspace. Si el usuario menciona una unidad o path, usarlo VERBATIM como absoluta. NO concatenar al workspace.",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta ABSOLUTA del archivo (ej: 'D:\\prueba.txt') o relativa al workspace. CRÍTICO: si el usuario dice 'en D:' o 'en /home/foo', usar absoluta — NUNCA crear dentro del workspace."},
                    "content": {"type": "string", "description": "Contenido a escribir"},
                    "mkdirp": {"type": "boolean", "description": "Crear directorios padres (default: true)"}
                }
            },
            {
                "name": "list_dir",
                "description": "Lista contenido de un directorio. ACEPTA RUTAS ABSOLUTAS o relativas al workspace.",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta ABSOLUTA del directorio (ej: 'D:\\proyectos') o relativa al workspace. Vacío = workspace root."},
                    "recursive": {"type": "boolean", "description": "Listado recursivo (default: false)"},
                    "max_depth": {"type": "integer", "description": "Profundidad máxima (default: 5)"}
                }
            },
            {
                "name": "create_file",
                "description": "Crea un archivo con campos semánticos separados. PREFERIR sobre write_file cuando el usuario pide 'crear un archivo' sin path explícito. Auto-resuelve location + filename, detecta absolute vs relative, sugiere parent dir.",
                "parameters": {
                    "filename": {"type": "string", "description": "Nombre del archivo SOLO (ej: 'prueba.txt'). NO incluir path aquí."},
                    "location": {"type": "string", "description": "Directorio destino. Si empieza con C:/D:/E:/ (Windows) o / (Unix), es ABSOLUTA. Si no, relativa al workspace. Default: workspace/data/"},
                    "content": {"type": "string", "description": "Contenido a escribir (default: '' archivo vacío)"},
                    "overwrite": {"type": "boolean", "description": "Sobrescribir si existe (default: false)"}
                }
            },
            {
                "name": "write_file",
                "description": "Escribe contenido a un archivo",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del archivo"},
                    "content": {"type": "string", "description": "Contenido a escribir"},
                    "mkdirp": {"type": "boolean", "description": "Crear directorios padres (default: true)"}
                }
            },
            {
                "name": "list_dir",
                "description": "Lista contenido de un directorio",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del directorio (vacío = root)"},
                    "recursive": {"type": "boolean", "description": "Listado recursivo (default: false)"}
                }
            },
            {
                "name": "execute_command",
                "description": "Ejecuta un comando en la terminal",
                "parameters": {
                    "command": {"type": "string", "description": "Comando a ejecutar"},
                    "cwd": {"type": "string", "description": "Directorio de trabajo"},
                    "timeout": {"type": "integer", "description": "Timeout en segundos (default: 60)"}
                }
            },
            {
                "name": "parse_file",
                "description": "Parsea un archivo según su extensión (JSON, código, etc.)",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del archivo"}
                }
            },
            {
                "name": "git_status",
                "description": "Muestra el estado del repositorio git (archivos modificados, staged, untracked)",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del repositorio (default: cwd)"}
                }
            },
            {
                "name": "git_diff",
                "description": "Muestra los cambios no commiteados. staged=true para ver cambios staged.",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del repositorio (default: cwd)"},
                    "staged": {"type": "boolean", "description": "Ver cambios staged (default: false)"},
                    "file": {"type": "string", "description": "Archivo específico para diff"}
                }
            },
            {
                "name": "git_log",
                "description": "Historial de commits del repositorio",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del repositorio (default: cwd)"},
                    "limit": {"type": "integer", "description": "Número de commits (default: 10)"}
                }
            },
            {
                "name": "terminal_create",
                "description": "Crea una terminal persistente con session ID para enviar comandos secuencialmente",
                "parameters": {
                    "name": {"type": "string", "description": "Nombre de la sesión (auto-generado si vacío)"},
                    "cwd": {"type": "string", "description": "Directorio de trabajo"}
                }
            },
            {
                "name": "terminal_send",
                "description": "Envia texto a una terminal persistente y retorna output",
                "parameters": {
                    "session_id": {"type": "string", "description": "ID de la sesión de terminal"},
                    "text": {"type": "string", "description": "Comando o texto a enviar"},
                    "wait_ms": {"type": "integer", "description": "Milisegundos a esperar antes de leer output (default: 2000)"}
                }
            },
            {
                "name": "terminal_close",
                "description": "Cierra una terminal persistente",
                "parameters": {
                    "session_id": {"type": "string", "description": "ID de la sesión de terminal"}
                }
            },
            {
                "name": "lsp_diagnostics",
                "description": "Analiza un archivo en busca de errores sintácticos (Python, JSON, YAML)",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del archivo a analizar"},
                    "language": {"type": "string", "description": "Lenguaje (auto-detectado por extensión)"}
                }
            },
            {
                "name": "web_fetch",
                "description": "Obtiene contenido de una URL y convierte HTML a Markdown",
                "parameters": {
                    "url": {"type": "string", "description": "URL a obtener"},
                    "timeout": {"type": "integer", "description": "Timeout en segundos (default: 120)"}
                }
            },
            {
                "name": "web_search",
                "description": "Busca en la web (DuckDuckGo -> Brave -> Google fallback). Devuelve titulo, URL y snippet por resultado. Usar para investigar temas, noticias, drivers, documentacion online.",
                "parameters": {
                    "query": {"type": "string", "description": "Consulta de busqueda"},
                    "max_results": {"type": "integer", "description": "Numero maximo de resultados (default: 5)"}
                }
            },
            {
                "name": "code_search",
                "description": "Busca código en archivos usando regex. Soporta filtros por patrón de archivo.",
                "parameters": {
                    "query": {"type": "string", "description": "Patrón regex a buscar"},
                    "path": {"type": "string", "description": "Directorio donde buscar (default: cwd)"},
                    "include": {"type": "string", "description": "Patrón de archivo para incluir (ej: '*.py')"},
                    "limit": {"type": "integer", "description": "Máximo resultados (default: 20)"}
                }
            },
            {
                "name": "git_blame",
                "description": "Muestra quien hizo cada linea de un archivo (git blame)",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del repositorio (default: cwd)"},
                    "file": {"type": "string", "description": "Archivo para blame"}
                }
            },
            {
                "name": "lsp_symbols",
                "description": "Lista funciones, clases y variables en un archivo (Python, JS, TS)",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del archivo"}
                }
            },
            {
                "name": "list_directory",
                "description": "Lista archivos y carpetas en cualquier path del sistema",
                "parameters": {
                    "path": {"type": "string", "description": "Path absoluto a listar (default: cwd)"}
                }
            },
            {
                "name": "workspace_folders",
                "description": "Lista carpetas del workspace actual con conteo de archivos",
                "parameters": {}
            },
            {
                "name": "find_files",
                "description": "Busca archivos por patron respetando .gitignore",
                "parameters": {
                    "pattern": {"type": "string", "description": "Patron glob (ej: '*.py')"},
                    "path": {"type": "string", "description": "Directorio donde buscar (default: cwd)"},
                    "use_ignore": {"type": "boolean", "description": "Respetar .gitignore (default: true)"}
                }
            },
            {
                "name": "git_commit",
                "description": "Crea un commit git con mensaje",
                "parameters": {
                    "path": {"type": "string", "description": "Ruta del repositorio (default: cwd)"},
                    "message": {"type": "string", "description": "Mensaje del commit"}
                }
            },
            {
                "name": "browser_snapshot",
                "description": "Toma un snapshot de accesibilidad de la página activa. Retorna un árbol de accesibilidad con roles y nombres de elementos. Usar antes de interactuar con la página.",
                "parameters": {
                    "page_id": {"type": "string", "description": "ID de página (opcional, usa activa por defecto)"},
                    "verbose": {"type": "boolean", "description": "Incluir todos los nodos (default: false)"}
                }
            },
            {
                "name": "browser_navigate",
                "description": "Navega a una URL en el navegador. Crea una página nueva si no hay ninguna.",
                "parameters": {
                    "url": {"type": "string", "description": "URL a navegar"},
                    "page_id": {"type": "string", "description": "ID de página (opcional)"}
                }
            },
            {
                "name": "browser_click",
                "description": "Hace click en un elemento usando su selector CSS o coordenadas.",
                "parameters": {
                    "selector": {"type": "string", "description": "Selector CSS del elemento"},
                    "x": {"type": "number", "description": "Coordenada X (si no hay selector)"},
                    "y": {"type": "number", "description": "Coordenada Y (si no hay selector)"},
                    "page_id": {"type": "string", "description": "ID de página (opcional)"}
                }
            },
            {
                "name": "browser_type",
                "description": "Escribe texto en un campo usando selector CSS.",
                "parameters": {
                    "text": {"type": "string", "description": "Texto a escribir"},
                    "selector": {"type": "string", "description": "Selector CSS del campo (opcional: enfoca y limpia)"},
                    "page_id": {"type": "string", "description": "ID de página (opcional)"}
                }
            },
            {
                "name": "browser_evaluate",
                "description": "Ejecuta JavaScript en la página y retorna el resultado.",
                "parameters": {
                    "script": {"type": "string", "description": "Código JavaScript a ejecutar"},
                    "page_id": {"type": "string", "description": "ID de página (opcional)"}
                }
            },
            {
                "name": "browser_screenshot",
                "description": "Toma una captura de pantalla de la página actual.",
                "parameters": {
                    "page_id": {"type": "string", "description": "ID de página (opcional)"},
                    "format": {"type": "string", "description": "Formato: png, jpeg, webp (default: png)"},
                    "full_page": {"type": "boolean", "description": "Capturar página completa (default: false)"}
                }
            },
            {
                "name": "browser_new_page",
                "description": "Abre una nueva pestaña/página en blanco.",
                "parameters": {
                    "url": {"type": "string", "description": "URL para abrir (default: about:blank)"}
                }
            },
            {
                "name": "browser_close_page",
                "description": "Cierra la página activa o una específica.",
                "parameters": {
                    "page_id": {"type": "string", "description": "ID de página a cerrar (opcional: cierra activa)"}
                }
            },
            {
                "name": "browser_performance",
                "description": "Inicia o detiene un trace de rendimiento. Usar start=true para comenzar, start=false para detener y analizar.",
                "parameters": {
                    "start": {"type": "boolean", "description": "true=iniciar trace, false=deterner"},
                    "page_id": {"type": "string", "description": "ID de página (opcional)"}
                }
            }
        ]
