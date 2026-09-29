"""
Deep Research Engine - Investigacion iterativa para SuperNEXUS v2.0

Inspirado en Odysseus/IterResearch:
- Loop Think->Search->Extract->Synthesize (max 8 rondas)
- LLM crea estrategia antes de buscar
- LLM decide cuando parar (stop criteria)
- Formateo especifico por categoria
- Extraccion concurrente con backpressure
- Progreso en tiempo real
"""

import asyncio
import json
import logging
import re
import time
from typing import Callable, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------
RESEARCH_PLAN_PROMPT = """\
Eres un estratega de investigacion. Antes de buscar, analiza esta pregunta y crea un plan.

**Pregunta:** {question}

Desglosa esta pregunta:
1. ¿Cuales son los subtemas clave que deben cubrirse para una respuesta completa?
2. ¿Que datos, hechos o perspectivas especificas debemos buscar?
3. ¿Que incluiria una respuesta completa y de alta calidad?

Retorna un objeto JSON con:
- "sub_questions": Array de 3-6 sub-preguntas especificas a investigar
- "key_topics": Array de topics/angulos clave a cubrir
- "success_criteria": Una oracion describiendo que se ve una respuesta completa

Ejemplo:
{{
  "sub_questions": ["¿Cual es el costo de vida en X?", "¿Como es el sistema de salud?"],
  "key_topics": ["economia", "salud", "seguridad", "cultura"],
  "success_criteria": "Una comparacion balanceada cubriendo costo, calidad de vida y consideraciones practicas."
}}
"""

QUERY_GEN_PROMPT = """\
Eres un asistente de investigacion planificando busquedas web.

**Pregunta original:** {question}

**Plan de investigacion:**
{research_plan}

**Lo que sabemos hasta ahora:**
{report}

**Ronda:** {round_num}

Genera {num_queries} consultas de busqueda enfocadas que ayuden a responder la pregunta.
{round_instruction}

Retorna SOLO un array JSON de strings de consulta, nada mas.
Ejemplo: ["consulta uno", "consulta dos", "consulta tres"]
"""

SYNTHESIZE_PROMPT = """\
Estas actualizando un reporte de investigacion evolutivo.

**Pregunta original:** {question}

**Reporte actual:**
{report}

**Nuevos hallazgos de esta ronda:**
{new_findings}

Integra los nuevos hallazgos en el reporte existente. Produce un reporte actualizado y bien organizado \
que responda la pregunta original tan completa como posible dada toda la evidencia disponible. \
Elimina redundancia, resuelve contradicciones y mantén flujo logico. \
Mantén URLs de fuentes como citaciones inline cuando sea relevante.

Escribe solo el reporte actualizado -- sin preambulo o meta-comentario.
"""

STOP_PROMPT = """\
Estas decidiendo si un reporte de investigacion es lo suficientemente completo.

**Pregunta original:** {question}

**Reporte actual:**
{report}

**Rondas completadas:** {round_num} de {max_rounds}

Basado en el reporte hasta ahora, ¿tenemos suficiente informacion para responder la pregunta \
de manera completa? Considera:
- ¿Se abordan los aspectos clave de la pregunta?
- ¿Hay vacios evidentes o sub-preguntas sin respuesta?
- ¿Es la evidencia suficiente y de multiples fuentes?

Si las rondas completadas estan bien por debajo del objetivo, prefiere continuar a menos que el \
reporte ya sea exhaustivo.

Responde con SOLO "YES" o "NO" seguido de una breve razon de una oracion.
Ejemplo: "YES -- El reporte cubre todos los aspectos principales con evidencia de multiples fuentes."
Ejemplo: "NO -- Todavia falta informacion sobre el impacto economico."
"""

FINAL_REPORT_PROMPT = """\
Escribe un reporte de investigacion **largo, detallado y completo** respondiendo esta pregunta:

**Pregunta:** {question}

**Toda la evidencia y analisis recopilado:**
{report}

Requisitos:
- Escribe MINIMO 1500 palabras -- debe ser un articulo completo y de calidad de revista
- Usa encabezados ## y ### claros para organizar en secciones logicas
- Cada seccion debe tener multiples parrafos detallados, no solo listas de puntos
- Sintetiza y analiza la informacion -- explica POR QUE las cosas importan, haz comparaciones, provee contexto
- Incluye puntos de datos especificos, numeros y estadisticas de la evidencia
- Incluye URLs de fuentes como citaciones inline [asi](url)
- Nota donde las fuentes estan de acuerdo y donde no
- Agrega un resumen ejecutivo breve al inicio
- Termina con una conclusion clara que responda directamente la pregunta
- Escribe en un estilo atractivo e informativo -- no seco o robotico
"""

CATEGORY_PROMPTS = {
    "product": """FORMATO ESPECIAL -- Este es un reporte de PRODUCTOS:
- Estructura como una LISTA ORDENADA de productos/opciones (mejor primero)
- Para CADA producto incluye: nombre como ### heading, precio aproximado, resumen de 2-3 oraciones, **Pros:** lista de puntos, **Cons:** lista de puntos, **Donde comprar:** URLs como links
- Empieza con una tabla rapida de comparacion de las mejores opciones (columnas: Nombre, Precio, Mejor Para, Rating)
- Termina con una seccion ## Verdicto eligiendo Mejor General y Mejor Valor
- Incluye citaciones de fuentes inline""",

    "comparison": """FORMATO ESPECIAL -- Este es un reporte de COMPARACION:
- Crea una ## Tabla de Comparacion como tabla markdown comparando TODAS las opciones en criterios clave (filas = criterios, columnas = opciones)
- Usa checkmarks, ratings o valores cortos en celdas
- Escribe una seccion por opcion con sus fortalezas, debilidades y caso de uso ideal
- Termina con ## Mejor Para veredictos (ej: "**Equipos pequenos:** Opcion A porque...")
- Incluye una seccion ## Consideraciones Comunes para cosas que aplican a todas las opciones""",

    "howto": """FORMATO ESPECIAL -- Este es un reporte COMO HACERLO:
- Empieza con ## Guia Rapida -- una lista numerada super concisa (una linea por paso, sin detalles, solo la accion). Ejemplo: 1. Instala X  2. Ejecuta Y  3. Configura Z
- Luego ## Prerequisitos listando que se necesita antes de empezar
- Luego los pasos detallados: ## Paso 1: ..., ## Paso 2: ...
- Cada paso debe tener un encabezado claro e instrucciones detalladas
- Usa blockquotes (> ) para tips y advertencias: > **Tip:** ... o > **Advertencia:** ...
- Termina con ## Errores Comunes
- Agrega tiempo estimado y nivel de dificultad cerca del inicio""",

    "factcheck": """FORMATO ESPECIAL -- Este es un reporte de VERIFICACION:
- Empieza con ## La Afirmacion redeclarando que se esta verificando
- Crea secciones ## Evidencia A Favor y ## Evidencia En Contra
- Cada pieza de evidencia debe ser un ### con nombre de fuente, que encontro, y que tan fuerte es la evidencia
- Incluye una seccion ## Veredicto con uno de: **Soportado**, **Evidencia Mixta**, o **No Soportado**
- Termina con ## Matices y Advertencias para contexto importante y limitaciones
- Se balanceado y cita fuentes para cada afirmacion""",
}


class DeepResearcher:
    """
    Motor de investigacion iterativa inspirado en Odysseus/IterResearch.
    
    Cada ronda: LLM genera queries -> busqueda web -> LLM extrae de paginas -> 
    LLM sintetiza en reporte evolutivo -> LLM decide continuar/parar.
    """

    def __init__(
        self,
        llm_caller: Callable,
        max_rounds: int = 6,
        max_time: int = 300,
        max_urls_per_round: int = 3,
        max_content_chars: int = 15000,
        max_report_tokens: int = 8192,
        extraction_concurrency: int = 3,
        min_rounds: int = 2,
        max_empty_rounds: int = 2,
        synthesis_window: int = 10,
        progress_callback: Optional[Callable] = None,
        category: Optional[str] = None,
    ):
        """
        Args:
            llm_caller: Async function(prompt, temperature, max_tokens) -> str
            max_rounds: Maximo de rondas de investigacion
            max_time: Tiempo maximo en segundos
            max_urls_per_round: URLs a fetchear por query
            max_content_chars: Maximo de caracteres por pagina
            max_report_tokens: Maximo de tokens para el reporte
            extraction_concurrency: Paginas concurrentes
            min_rounds: Minimo de rondas antes de poder parar
            max_empty_rounds: Rondas vacias consecutivas antes de abortar
            synthesis_window: Cuantos hallazgos usar en sintesis
            progress_callback: Funcion(phase, **kwargs) para progreso
            category: Categoria forzada (product, comparison, howto, factcheck, general)
        """
        self.llm = llm_caller
        self.max_rounds = max_rounds
        self.max_time = max_time
        self.max_urls_per_round = max_urls_per_round
        self.max_content_chars = max_content_chars
        self.max_report_tokens = max_report_tokens
        self.extraction_concurrency = extraction_concurrency
        self.min_rounds = min_rounds
        self.max_empty_rounds = max_empty_rounds
        self.synthesis_window = synthesis_window
        self._progress = progress_callback
        self._cancelled = False
        self._start_time: float = 0
        self.queries_used: Set[str] = set()
        self.urls_fetched: Set[str] = set()
        self.round_count: int = 0
        self.findings: List[Dict] = []
        self.evolving_report: str = ""
        self.research_plan: str = ""
        self.category = category

    def cancel(self):
        self._cancelled = True

    async def research(self, question: str) -> str:
        """Ejecuta investigacion iterativa y retorna reporte final."""
        self._start_time = time.time()
        self.findings = []
        report = ""

        # PLAN
        self._emit(phase="planning")
        self.research_plan = await self._create_plan(question)
        if not self.category:
            self.category = await self._classify_category(question)

        consecutive_empty = 0

        for round_num in range(1, self.max_rounds + 1):
            self.round_count = round_num
            if self._cancelled or self._time_exceeded():
                break

            # THINK: generar queries
            queries = await self._generate_queries(question, report, round_num)
            if not queries:
                break

            self._emit(phase="searching", round=round_num, queries=len(queries))

            # SEARCH + EXTRACT
            round_findings = await self._search_and_extract(queries, question)
            if round_findings:
                self.findings.extend(round_findings)
                consecutive_empty = 0
            else:
                consecutive_empty += 1
                if consecutive_empty >= self.max_empty_rounds:
                    break

            # SYNTHESIZE
            if self.findings:
                self._emit(phase="analyzing", round=round_num)
                report = await self._synthesize(question, self.findings, report)

            # DECIDE
            if round_num >= self.min_rounds:
                if await self._should_stop(question, report, round_num):
                    break

        # FINAL REPORT
        self._emit(phase="writing")
        if not report and self.findings:
            return self._fallback_report(question, self.findings)
        if not report:
            return "No se pudo encontrar informacion para esta pregunta."

        self.evolving_report = report
        return await self._final_report(question, report)

    # ------------------------------------------------------------------
    # LLM helper
    # ------------------------------------------------------------------
    async def _llm(self, prompt: str, temperature: float = 0.3,
                   max_tokens: int = 4096) -> str:
        return await self.llm(prompt, temperature=temperature, max_tokens=max_tokens)

    # ------------------------------------------------------------------
    # PLAN
    # ------------------------------------------------------------------
    async def _create_plan(self, question: str) -> str:
        prompt = RESEARCH_PLAN_PROMPT.format(question=question)
        try:
            response = await self._llm(prompt, temperature=0.3, max_tokens=1024)
            parsed = self._parse_json(response)
            if parsed:
                parts = []
                if parsed.get("sub_questions"):
                    parts.append("Sub-preguntas: " + "; ".join(parsed["sub_questions"]))
                if parsed.get("key_topics"):
                    parts.append("Topics clave: " + ", ".join(parsed["key_topics"]))
                if parsed.get("success_criteria"):
                    parts.append("Exito: " + parsed["success_criteria"])
                return "\n".join(parts) if parts else response
            return response
        except Exception as e:
            logger.warning(f"Planificacion falló: {e}")
            return ""

    async def _classify_category(self, question: str) -> Optional[str]:
        valid = ", ".join(CATEGORY_PROMPTS.keys())
        prompt = (
            f"Clasifica esta pregunta de investigacion en UNA categoria.\n"
            f"Categorias: {valid}\n"
            f"Si ninguna encaja bien, responde con: general\n\n"
            f"Pregunta: {question}\n\n"
            f"Responde SOLO con el nombre de la categoria, nada mas."
        )
        try:
            result = await self._llm(prompt, temperature=0, max_tokens=20)
            cat = (result or "").strip().lower().split()[0].strip(".,\"'*:")
            if cat in CATEGORY_PROMPTS:
                return cat
            return None
        except Exception:
            return None

    # ------------------------------------------------------------------
    # THINK: generar queries
    # ------------------------------------------------------------------
    async def _generate_queries(self, question: str, report: str, round_num: int) -> List[str]:
        if round_num == 1:
            num_queries = 4
            instruction = "Primera ronda -- genera queries amplias y diversas que exploren los facets clave."
        else:
            num_queries = 3
            instruction = "Ya tenemos hallazgos parciales. Genera queries de seguimiento para llenar vacios."

        prompt = QUERY_GEN_PROMPT.format(
            question=question,
            research_plan=self.research_plan or "(Sin plan -- buscar ampliamente.)",
            report=report or "(Sin hallazgos aun.)",
            round_num=round_num,
            num_queries=num_queries,
            round_instruction=instruction,
        )
        try:
            response = await self._llm(prompt, temperature=0.5, max_tokens=4096)
            queries = self._parse_json_array(response)
            new = [q for q in queries if q not in self.queries_used]
            self.queries_used.update(new)
            return new
        except Exception:
            return []

    # ------------------------------------------------------------------
    # SEARCH + EXTRACT
    # ------------------------------------------------------------------
    async def _search_and_extract(self, queries: List[str], question: str) -> List[Dict]:
        # Buscar todas las queries en paralelo
        search_tasks = [self._search(q) for q in queries]
        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        urls_to_fetch = []
        for result in search_results:
            if isinstance(result, Exception) or not result:
                continue
            for r in result:
                url = r.get("url", "")
                if url and url not in self.urls_fetched:
                    urls_to_fetch.append(r)
                    self.urls_fetched.add(url)
                if len(urls_to_fetch) >= self.max_urls_per_round * len(queries):
                    break

        if self._cancelled or self._time_exceeded():
            return []

        # Fetch y extraer con backpressure
        semaphore = asyncio.Semaphore(self.extraction_concurrency)
        all_findings = []

        async def _bounded(result: Dict) -> Optional[Dict]:
            async with semaphore:
                return await self._fetch_and_extract(result["url"], question, result.get("title", ""))

        tasks = [_bounded(r) for r in urls_to_fetch]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for r in results:
            if isinstance(r, Exception) or not r:
                continue
            all_findings.append(r)

        return all_findings

    async def _search(self, query: str) -> List[Dict]:
        """Busqueda web multi-backend."""
        # Intentar WebResearcher primero
        try:
            from src.core.web_researcher import WebResearcher
            researcher = WebResearcher()
            results = await researcher.search(query, 10)
            if results:
                return results
        except Exception as e:
            logger.warning(f"WebResearcher falló: {e}")

        # Fallback: DDGS
        try:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS
            with DDGS() as ddgs:
                results = []
                for r in ddgs.text(query, max_results=10):
                    results.append({
                        "url": r.get("href", ""),
                        "title": r.get("title", ""),
                        "snippet": r.get("body", ""),
                        "source": "ddgs",
                    })
            return results
        except Exception as e:
            logger.warning(f"DDGS falló: {e}")
            return []

    async def _fetch_and_extract(self, url: str, question: str, title: str) -> Optional[Dict]:
        """Fetch URL y extraer info relevante con LLM."""
        self._emit(phase="reading", url=url, title=title or url)
        
        try:
            import httpx
            from bs4 import BeautifulSoup
            async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as c:
                r = await c.get(url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code != 200:
                    return None
                soup = BeautifulSoup(r.text, "html.parser")
                for tag in soup(["script", "style", "nav", "footer", "header", "noscript", "aside"]):
                    tag.decompose()
                content = soup.get_text(separator="\n", strip=True)
                if not content:
                    return None
                # Truncar
                if len(content) > self.max_content_chars:
                    content = content[:self.max_content_chars]
        except Exception:
            return None

        # Extraer con LLM
        prompt = (
            f"De esta pagina web, extrae informacion relevante para responder: {question}\n\n"
            f"Contenido:\n{content[:15000]}\n\n"
            f"Retorna JSON con: {{\"summary\": \"resumen de 2-3 oraciones\", "
            f"\"evidence\": \"evidencia especifica con datos\", "
            f"\"relevance\": \"alta/media/baja\"}}"
        )
        try:
            response = await self._llm(prompt, temperature=0.2, max_tokens=2048)
            parsed = self._parse_json(response)
            if parsed:
                parsed["url"] = url
                parsed["title"] = title
                return parsed
            return {
                "url": url,
                "title": title,
                "summary": response[:500],
                "evidence": response[:3000],
            }
        except Exception:
            return None

    # ------------------------------------------------------------------
    # SYNTHESIZE
    # ------------------------------------------------------------------
    async def _synthesize(self, question: str, findings: List[Dict], current_report: str) -> str:
        window = findings[-self.synthesis_window:]
        findings_text = "\n\n".join(
            f"**{f.get('title', 'Fuente')}** ({f.get('url', '')})\n"
            f"Resumen: {f.get('summary', '')}\n"
            f"Evidencia: {f.get('evidence', '')[:1000]}"
            for f in window
        )

        prompt = SYNTHESIZE_PROMPT.format(
            question=question,
            report=current_report or "(Primera ronda -- sin reporte aun.)",
            new_findings=findings_text,
        )
        try:
            return await self._llm(prompt, temperature=0.3, max_tokens=self.max_report_tokens)
        except Exception:
            return current_report

    # ------------------------------------------------------------------
    # DECIDE
    # ------------------------------------------------------------------
    async def _should_stop(self, question: str, report: str, round_num: int) -> bool:
        prompt = STOP_PROMPT.format(
            question=question,
            report=report,
            round_num=round_num,
            max_rounds=self.max_rounds,
        )
        try:
            response = await self._llm(prompt, temperature=0.1, max_tokens=128)
            answer = re.sub(r'^[\s*_`"\'>#\-]+', '', response.strip()).upper()
            return answer.startswith("YES")
        except Exception:
            return False

    # ------------------------------------------------------------------
    # FINAL REPORT
    # ------------------------------------------------------------------
    async def _final_report(self, question: str, report: str) -> str:
        prompt = FINAL_REPORT_PROMPT.format(question=question, report=report)
        cat_extra = CATEGORY_PROMPTS.get(self.category or "", "")
        if cat_extra:
            prompt += "\n\n" + cat_extra

        try:
            result = await self._llm(prompt, temperature=0.3, max_tokens=self.max_report_tokens)
            if len(result) < 500 and self.findings:
                logger.warning("Reporte muy corto, usando reporte evolutivo")
                return report
            return result
        except Exception:
            return report

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _emit(self, phase: str, **kwargs):
        if self._progress:
            try:
                self._progress({"phase": phase, **kwargs})
            except Exception:
                pass

    def _time_exceeded(self) -> bool:
        return (time.time() - self._start_time) > self.max_time

    def _parse_json(self, text: str) -> Optional[Dict]:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```\w*\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\{[\s\S]*\}', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return None

    def _parse_json_array(self, text: str) -> List[str]:
        text = text.strip()
        if text.startswith("```"):
            text = re.sub(r'^```\w*\n?', '', text)
            text = re.sub(r'\n?```$', '', text)
        match = re.search(r'\[[\s\S]*\]', text)
        if match:
            try:
                return json.loads(match.group())
            except json.JSONDecodeError:
                pass
        return []

    def _fallback_report(self, question: str, findings: List[Dict]) -> str:
        parts = [f"## Investigacion: {question}\n"]
        for i, f in enumerate(findings, 1):
            parts.append(f"### {i}. {f.get('title', 'Fuente')}")
            parts.append(f"URL: {f.get('url', 'N/A')}")
            parts.append(f"{f.get('summary', 'Sin resumen')}\n")
        return "\n".join(parts)

    def get_stats(self) -> Dict:
        return {
            "Rounds": self.round_count,
            "Findings": len(self.findings),
            "URLs": len(self.urls_fetched),
            "Queries": len(self.queries_used),
        }
