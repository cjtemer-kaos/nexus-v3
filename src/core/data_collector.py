"""
DataCollector - Pipeline de recoleccion y filtrado de datos para entrenamiento de Nexus

Inspired by: dclm, dolma, modded-nanogpt

Recolecta conversaciones, logs y experiencias de SuperNEXUS,
las filtra por calidad, las categoriza y genera datasets
listos para fine-tuning (SFT, DPO, RLHF).

Fases:
1. Recoleccion - Extrae de logs, message_board, observaciones
2. Filtrado - Calidad >= 0.7, deduplicacion, limpieza
3. Categorizacion - code, reasoning, security, creative, etc.
4. Export - JSONL para SFT, preference pairs para DPO
"""

import hashlib
import json
import logging
import os
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ============================================================
# Configuracion
# ============================================================
NEXUS_HOME = Path(os.environ.get("NEXUS_HOME", Path.home() / ".nexus"))
BRAIN_DIR = NEXUS_HOME / "brain"
DATA_DIR = NEXUS_HOME / "training_data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

# Patrones de baja calidad
LOW_QUALITY_PATTERNS = [
    r"^[a-zA-Z]$",           # Mensajes de 1 caracter
    r"^\s*$",                 # Vacio
    r"^(ok|yes|no|hi|bye)$",  # Respuestas triviales
    r"^(Error|Exception|Traceback)",  # Errores sin contexto
    r"^(Hello|Hi|Hey),?\s",   # Saludos genericos
    r"^(Thank|Thanks)\s",     # Agradecimientos
    r"^(I think|I believe|Maybe)",  # Incertidumbre debil
]

# Patrones de alta calidad
HIGH_QUALITY_PATTERNS = [
    r"def\s+\w+\(",           # Definiciones de funciones
    r"class\s+\w+",           # Definiciones de clases
    r"^(Plan|Step|Phase)\s*\d",  # Planes estructurados
    r"^(Analisis|Analysis|Resumen)",  # Analisis
    r"^(Fix|Solution|Solucion|Parche)",  # Fixes documentados
    r"^(Configuracion|Config|Setup)",   # Configuracion
    r"^(Arquitectura|Architecture)",    # Arquitectura
    r"^(Security|Seguridad|Audit)",     # Seguridad
    r"^(Optimizacion|Optimization)",    # Optimizacion
    r"^(Test|Prueba|Verificacion)",     # Testing
]

# Categorias de entrenamiento
TRAINING_CATEGORIES = [
    "code_generation",
    "code_review",
    "debugging",
    "architecture",
    "security",
    "reasoning",
    "creative_writing",
    "system_admin",
    "data_analysis",
    "documentation",
    "conversation",
    "instruction_following",
]


@dataclass
class DataSample:
    """Una muestra de datos para entrenamiento"""
    prompt: str
    response: str
    category: str = "conversation"
    quality_score: float = 0.0
    source: str = ""
    timestamp: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)
    sample_id: str = ""

    def __post_init__(self):
        if not self.sample_id:
            content_hash = hashlib.sha256(
                f"{self.prompt}{self.response}".encode()
            ).hexdigest()[:12]
            self.sample_id = f"nexus_{content_hash}"

    def to_sft_format(self) -> Dict:
        """Formato para Supervised Fine-Tuning"""
        return {
            "messages": [
                {"role": "user", "content": self.prompt},
                {"role": "assistant", "content": self.response},
            ],
            "category": self.category,
            "quality": self.quality_score,
            "source": self.source,
        }

    def to_dpo_format(self, rejected_response: str = "") -> Dict:
        """Formato para Direct Preference Optimization"""
        return {
            "prompt": self.prompt,
            "chosen": self.response,
            "rejected": rejected_response or self._generate_rejected(),
            "category": self.category,
        }

    def _generate_rejected(self) -> str:
        """Genera una respuesta rechazada (version de menor calidad)"""
        # Version truncada o incompleta como rejected
        if len(self.response) > 100:
            return self.response[:100] + "..."
        return self.response.lower()


class QualityFilter:
    """Filtra muestras por calidad usando heuristicas"""

    def __init__(self, min_score: float = 0.7):
        self.min_score = min_score
        self._compile_patterns()

    def _compile_patterns(self):
        self._low_patterns = [re.compile(p, re.IGNORECASE) for p in LOW_QUALITY_PATTERNS]
        self._high_patterns = [re.compile(p, re.IGNORECASE) for p in HIGH_QUALITY_PATTERNS]

    def score(self, prompt: str, response: str) -> float:
        """Calcula score de calidad 0.0 - 1.0"""
        score = 0.5  # Base neutral

        # Penalizaciones
        for pattern in self._low_patterns:
            if pattern.search(response):
                score -= 0.3
                break

        # Bonificaciones
        high_matches = sum(1 for p in self._high_patterns if p.search(response))
        score += high_matches * 0.1

        # Longitud optima (no muy corto, no muy largo)
        resp_len = len(response)
        if 50 <= resp_len <= 5000:
            score += 0.1
        elif resp_len < 20:
            score -= 0.2
        elif resp_len > 10000:
            score -= 0.1

        # Estructura (markdown, codigo, listas)
        if "```" in response:
            score += 0.1
        if response.count("\n") > 5:
            score += 0.05
        if re.search(r"^\d+\.\s", response, re.MULTILINE):
            score += 0.05

        # Contenido tecnico
        tech_keywords = ["def ", "class ", "import ", "async ", "await ",
                        "SELECT ", "INSERT ", "UPDATE ", "docker", "kubectl",
                        "git ", "ssh ", "api", "endpoint", "middleware"]
        tech_count = sum(1 for kw in tech_keywords if kw.lower() in response.lower())
        score += min(tech_count * 0.02, 0.15)

        return max(0.0, min(1.0, round(score, 3)))

    def passes(self, prompt: str, response: str) -> bool:
        """Verifica si pasa el filtro de calidad"""
        return self.score(prompt, response) >= self.min_score


class Deduplicator:
    """Elimina muestras duplicadas usando hashing"""

    def __init__(self):
        self._seen: set = set()

    def is_duplicate(self, prompt: str, response: str) -> bool:
        """Verifica si ya existe"""
        h = hashlib.md5(
            f"{prompt.strip().lower()}|||{response.strip().lower()}".encode()
        ).hexdigest()
        if h in self._seen:
            return True
        self._seen.add(h)
        return False

    def load_existing_hashes(self, hash_file: Path):
        """Carga hashes existentes"""
        if hash_file.exists():
            with open(hash_file) as f:
                self._seen = set(line.strip() for line in f if line.strip())

    def save_hashes(self, hash_file: Path):
        """Guarda hashes"""
        hash_file.parent.mkdir(parents=True, exist_ok=True)
        with open(hash_file, "w") as f:
            f.write("\n".join(sorted(self._seen)))


class Categorizer:
    """Categoriza muestras automaticamente"""

    CATEGORY_KEYWORDS = {
        "code_generation": ["def ", "class ", "function", "implement", "crear funcion",
                           "escribe el codigo", "write code", "```python", "```javascript"],
        "code_review": ["review", "revisar codigo", "code quality", "refactor",
                       "mejorar este codigo", "optimizar"],
        "debugging": ["error", "bug", "debug", "traceback", "exception",
                     "no funciona", "fix", "corregir", "por que falla"],
        "architecture": ["arquitectura", "architecture", "design pattern", "microservicio",
                        "estructura del proyecto", "modulos", "componentes"],
        "security": ["security", "seguridad", "vulnerabilidad", "audit", "auth",
                    "permission", "injection", "ssrf", "xss", "csrf"],
        "reasoning": ["analisis", "analysis", "porque", "why", "explicar",
                     "razonamiento", "considerar", "evaluar"],
        "creative_writing": ["escribir", "write", "crear contenido", "story",
                            "poema", "email", "carta", "descripcion"],
        "system_admin": ["docker", "kubectl", "ssh", "deploy", "servidor",
                        "instalar", "configurar", "linux", "bash", "terminal"],
        "data_analysis": ["datos", "data", "analisis", "query", "SELECT",
                         "estadisticas", "metricas", "grafico", "dashboard"],
        "documentation": ["documentar", "documentacion", "readme", "docs",
                         "instrucciones", "guia", "tutorial"],
        "instruction_following": ["haz", "create", "genera", "build", "implementa",
                                 "escribe", "responde", "dime"],
    }

    def categorize(self, prompt: str, response: str) -> str:
        """Determina la categoria dominante"""
        combined = f"{prompt} {response}".lower()
        scores = {}

        for category, keywords in self.CATEGORY_KEYWORDS.items():
            score = sum(1 for kw in keywords if kw.lower() in combined)
            if score > 0:
                scores[category] = score

        if not scores:
            return "conversation"

        return max(scores, key=scores.get)


class DataCollector:
    """Recolector principal de datos de entrenamiento"""

    def __init__(self, min_quality: float = 0.7):
        self.quality_filter = QualityFilter(min_score=min_quality)
        self.deduplicator = Deduplicator()
        self.categorizer = Categorizer()
        self.samples: List[DataSample] = []
        self.stats = {
            "total_collected": 0,
            "passed_quality": 0,
            "filtered_low_quality": 0,
            "filtered_duplicate": 0,
            "by_category": {},
        }

    async def collect_sample(self, prompt: str, response: str, category: str = "conversation", source: str = "ai_tools", quality: float = 0.7) -> bool:
        """Hook en tiempo real: recolecta una muestra individual."""
        if not self.quality_filter.passes(prompt, response):
            self.stats["filtered_low_quality"] += 1
            return False
        cat = self.categorizer.categorize(prompt, response) if category == "auto" else category
        if self.deduplicator.is_duplicate(prompt, response):
            self.stats["filtered_duplicate"] += 1
            return False
        sample = DataSample(prompt=prompt, response=response, category=cat, quality_score=quality, source=source)
        self.samples.append(sample)
        self.stats["total_collected"] += 1
        self.stats["passed_quality"] += 1
        self.stats["by_category"][cat] = self.stats["by_category"].get(cat, 0) + 1
        return True

    def collect_from_message_board(self, db_path: Optional[Path] = None) -> int:
        """Recolecta del message_board.db"""
        db_path = db_path or BRAIN_DIR / "message_board.db"
        if not db_path.exists():
            logger.warning(f"Message board no existe: {db_path}")
            return 0

        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()

        # Buscar pares task -> task_done
        c.execute("""
            SELECT t.id, t.sender, t.content, t.timestamp,
                   d.sender as responder, d.content as response, d.timestamp as response_ts
            FROM messages t
            JOIN messages d ON d.metadata LIKE '%"task_id":' || t.id || '%'
            WHERE t.msg_type = 'task' AND d.msg_type = 'task_done'
            ORDER BY t.id DESC
            LIMIT 5000
        """)

        rows = c.fetchall()
        conn.close()

        for row in rows:
            task_id, sender, prompt, ts, responder, response, response_ts = row
            self._add_sample(
                prompt=prompt,
                response=response,
                source=f"message_board:{sender}->{responder}",
                timestamp=ts,
                metadata={"task_id": task_id, "response_ts": response_ts},
            )

        return len(self.samples)

    def collect_from_observations(self, db_path: Optional[Path] = None) -> int:
        """Recolecta de nexus_memory.db observaciones"""
        db_path = db_path or BRAIN_DIR / "nexus_memory.db"
        if not db_path.exists():
            logger.warning(f"Nexus memory no existe: {db_path}")
            return 0

        conn = sqlite3.connect(str(db_path))
        c = conn.cursor()

        c.execute("""
            SELECT id, ts, content, category, project, metadata
            FROM observations
            ORDER BY id DESC
            LIMIT 2000
        """)

        rows = c.fetchall()
        conn.close()

        for row in rows:
            obs_id, ts, content, category, project, metadata = row
            # Usar observaciones como prompts de instruccion
            prompt = f"Genera un reporte detallado sobre: {category} en el proyecto {project}"
            self._add_sample(
                prompt=prompt,
                response=content,
                source=f"observation:{obs_id}",
                timestamp=ts,
                metadata={"category": category, "project": project},
            )

        return len(self.samples)

    def collect_from_smoltalk(self, data_dir: Path) -> int:
        """Recolecta de datasets SmolTalk (parquet files)"""
        try:
            import pandas as pd
        except ImportError:
            logger.warning("pandas no disponible, saltando SmolTalk")
            return 0

        collected = 0
        data_dir = Path(data_dir)

        if not data_dir.exists():
            logger.warning(f"SmolTalk data dir no existe: {data_dir}")
            return 0

        for parquet_file in data_dir.rglob("*.parquet"):
            try:
                df = pd.read_parquet(parquet_file)
                for _, row in df.iterrows():
                    prompt = row.get("prompt", row.get("instruction", ""))
                    response = row.get("response", row.get("output", ""))
                    if prompt and response:
                        self._add_sample(
                            prompt=str(prompt),
                            response=str(response),
                            source=f"smoltalk:{parquet_file.name}",
                            metadata={"dataset": parquet_file.parent.name},
                        )
                        collected += 1
            except Exception as e:
                logger.debug(f"Error leyendo {parquet_file}: {e}")

        return collected

    def collect_from_logs(self, log_dir: Optional[Path] = None) -> int:
        """Recolecta de logs de conversaciones de SuperNEXUS"""
        log_dir = log_dir or NEXUS_HOME / "logs"
        if not log_dir.exists():
            return 0

        collected = 0
        for log_file in log_dir.glob("*.jsonl"):
            try:
                with open(log_file) as f:
                    for line in f:
                        try:
                            entry = json.loads(line)
                            if entry.get("role") == "user":
                                prompt = entry.get("content", "")
                            elif entry.get("role") == "assistant":
                                response = entry.get("content", "")
                                if prompt:
                                    self._add_sample(
                                        prompt=prompt,
                                        response=response,
                                        source=f"log:{log_file.name}",
                                        timestamp=entry.get("timestamp", ""),
                                    )
                                    prompt = ""
                                    collected += 1
                        except json.JSONDecodeError:
                            continue
            except Exception as e:
                logger.debug(f"Error leyendo {log_file}: {e}")

        return collected

    def _add_sample(self, prompt: str, response: str, source: str = "",
                    timestamp: str = "", metadata: Dict = None):
        """Agrega una muestra despues de filtrar"""
        self.stats["total_collected"] += 1

        # Verificar duplicados
        if self.deduplicator.is_duplicate(prompt, response):
            self.stats["filtered_duplicate"] += 1
            return

        # Filtrar por calidad
        if not self.quality_filter.passes(prompt, response):
            self.stats["filtered_low_quality"] += 1
            return

        self.stats["passed_quality"] += 1

        # Categorizar
        category = self.categorizer.categorize(prompt, response)
        quality = self.quality_filter.score(prompt, response)

        sample = DataSample(
            prompt=prompt,
            response=response,
            category=category,
            quality_score=quality,
            source=source,
            timestamp=timestamp or datetime.now().isoformat(),
            metadata=metadata or {},
        )

        self.samples.append(sample)
        self.stats["by_category"][category] = self.stats["by_category"].get(category, 0) + 1

    def export_sft(self, output_path: Optional[Path] = None,
                   min_quality: float = 0.7) -> Path:
        """Exporta a formato SFT (JSONL)"""
        output_path = output_path or DATA_DIR / "nexus_sft_dataset.jsonl"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        filtered = [s for s in self.samples if s.quality_score >= min_quality]

        with open(output_path, "w", encoding="utf-8") as f:
            for sample in filtered:
                f.write(json.dumps(sample.to_sft_format(), ensure_ascii=False) + "\n")

        logger.info(f"Exportado {len(filtered)} muestras SFT a {output_path}")
        return output_path

    def export_dpo(self, output_path: Optional[Path] = None,
                   min_quality: float = 0.8) -> Path:
        """Exporta a formato DPO (preference pairs)"""
        output_path = output_path or DATA_DIR / "nexus_dpo_dataset.jsonl"
        output_path.parent.mkdir(parents=True, exist_ok=True)

        filtered = [s for s in self.samples if s.quality_score >= min_quality]

        with open(output_path, "w", encoding="utf-8") as f:
            for sample in filtered:
                f.write(json.dumps(sample.to_dpo_format(), ensure_ascii=False) + "\n")

        logger.info(f"Exportado {len(filtered)} muestras DPO a {output_path}")
        return output_path

    def export_by_category(self, output_dir: Optional[Path] = None) -> Path:
        """Exporta datasets separados por categoria"""
        output_dir = output_dir or DATA_DIR / "by_category"
        output_dir.mkdir(parents=True, exist_ok=True)

        by_cat = {}
        for sample in self.samples:
            if sample.category not in by_cat:
                by_cat[sample.category] = []
            by_cat[sample.category].append(sample)

        for category, samples in by_cat.items():
            cat_path = output_dir / f"nexus_{category}.jsonl"
            with open(cat_path, "w", encoding="utf-8") as f:
                for sample in samples:
                    f.write(json.dumps(sample.to_sft_format(), ensure_ascii=False) + "\n")
            logger.info(f"Exportado {len(samples)} muestras de {category}")

        return output_dir

    def get_stats(self) -> Dict:
        """Estadisticas de recoleccion"""
        quality_dist = {"high": 0, "medium": 0, "low": 0}
        for s in self.samples:
            if s.quality_score >= 0.9:
                quality_dist["high"] += 1
            elif s.quality_score >= 0.7:
                quality_dist["medium"] += 1
            else:
                quality_dist["low"] += 1

        return {
            **self.stats,
            "total_samples": len(self.samples),
            "quality_distribution": quality_dist,
            "avg_quality": sum(s.quality_score for s in self.samples) / max(len(self.samples), 1),
        }

    def collect_all(self, smoltalk_dir: Optional[Path] = None) -> Dict:
        """Recolecta de todas las fuentes disponibles"""
        logger.info("=== Iniciando recoleccion completa ===")

        self.collect_from_message_board()
        logger.info(f"Message board: {len(self.samples)} muestras")

        self.collect_from_observations()
        logger.info(f"Observations: {len(self.samples)} muestras")

        if smoltalk_dir:
            self.collect_from_smoltalk(smoltalk_dir)
            logger.info(f"SmolTalk: {len(self.samples)} muestras")

        self.collect_from_logs()
        logger.info(f"Logs: {len(self.samples)} muestras")

        stats = self.get_stats()
        logger.info(f"Recoleccion completa: {stats}")
        return stats
