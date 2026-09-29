"""
Topic Analyzer - Analisis de topics en conversaciones para SuperNEXUS v2.0

Inspirado en Odysseus:
- Deteccion de topics por keywords
- Frecuencia de topics
- Ejemplos de conversaciones por topic
- Estadisticas de sesiones
"""

from __future__ import annotations

import re
from typing import Dict, List, Any
from collections import defaultdict


# Keywords por topic (espanol + ingles)
TOPIC_KEYWORDS: Dict[str, List[str]] = {
    "Technology": [
        "ai", "machine learning", "python", "code", "programming", "computer",
        "software", "hardware", "algorithm", "api", "database", "server",
        "inteligencia artificial", "programacion", "algoritmo", "servidor",
    ],
    "Science": [
        "science", "physics", "chemistry", "biology", "math", "research",
        "experiment", "ciencia", "fisica", "quimica", "biologia", "matematicas",
        "investigacion", "experimento",
    ],
    "Work": [
        "work", "job", "career", "project", "task", "deadline", "meeting",
        "trabajo", "proyecto", "tarea", "reunion", "jefe", "colega",
    ],
    "Personal": [
        "personal", "family", "friend", "relationship", "health", "wellness",
        "personal", "familia", "amigo", "relacion", "salud", "bienestar",
    ],
    "Learning": [
        "learn", "study", "education", "course", "tutorial", "guide",
        "aprender", "estudiar", "educacion", "curso", "tutorial", "guia",
    ],
    "Creativity": [
        "write", "story", "create", "design", "art", "music", "draw",
        "escribir", "historia", "crear", "diseñar", "arte", "musica",
    ],
    "Planning": [
        "plan", "schedule", "organize", "arrange", "timeline", "calendar",
        "planificar", "programar", "organizar", "calendario",
    ],
    "Troubleshooting": [
        "error", "bug", "fix", "problem", "issue", "debug", "troubleshoot",
        "error", "problema", "solucionar", "depurar", "arreglar",
    ],
    "Security": [
        "security", "hack", "vulnerability", "encrypt", "password", "auth",
        "seguridad", "vulnerabilidad", "encriptar", "contraseña", "autenticacion",
    ],
    "AI/ML": [
        "model", "training", "inference", "neural", "deep learning", "llm",
        "modelo", "entrenamiento", "inferencia", "neural", "aprendizaje profundo",
    ],
}


def analyze_topics(
    messages: List[Dict[str, Any]],
    min_frequency: int = 2,
) -> Dict[str, Any]:
    """
    Analizar topics en una lista de mensajes.
    
    Args:
        messages: Lista de mensajes [{"role": str, "content": str}, ...]
        min_frequency: Frecuencia minima para incluir un topic
        
    Returns:
        Dict con topics, frecuencias y ejemplos
    """
    topic_counts: Dict[str, int] = defaultdict(int)
    topic_examples: Dict[str, List[Dict]] = defaultdict(list)
    topic_sessions: Dict[str, set] = defaultdict(set)
    
    for msg in messages:
        content = msg.get("content", "")
        role = msg.get("role", "unknown")
        session_id = msg.get("session_id", "unknown")
        
        if not content:
            continue
        
        content_lower = str(content).lower()
        
        for topic, keywords in TOPIC_KEYWORDS.items():
            for kw in keywords:
                if re.search(rf"\b{re.escape(kw)}\b", content_lower):
                    topic_counts[topic] += 1
                    topic_sessions[topic].add(session_id)
                    
                    # Guardar ejemplo (max 5 por topic)
                    if len(topic_examples[topic]) < 5:
                        # Buscar la oracion que contiene el keyword
                        sentences = re.split(r'[.!?]', str(content))
                        for sentence in sentences:
                            if re.search(rf"\b{re.escape(kw)}\b", sentence.lower()):
                                topic_examples[topic].append({
                                    "role": role,
                                    "snippet": sentence.strip()[:200],
                                    "keyword": kw,
                                    "session_id": session_id,
                                })
                                break
                    break  # Solo contar una vez por mensaje
    
    # Filtrar por frecuencia minima
    results = []
    for topic, count in sorted(topic_counts.items(), key=lambda x: x[1], reverse=True):
        if count >= min_frequency:
            # Deduplicar ejemplos
            seen = set()
            unique_examples = []
            for ex in topic_examples[topic]:
                key = f"{ex['session_id']}-{ex['snippet'][:50]}"
                if key not in seen:
                    seen.add(key)
                    unique_examples.append(ex)
            
            results.append({
                "topic": topic,
                "frequency": count,
                "session_count": len(topic_sessions[topic]),
                "examples": unique_examples[:5],
            })
    
    return {
        "topics": results,
        "total_topics": len(results),
        "total_messages": len(messages),
    }


def get_topic_suggestions(topic: str) -> List[str]:
    """
    Obtener sugerencias de contenido basadas en un topic.
    """
    suggestions = {
        "Technology": [
            "Ultimas tendencias en AI/ML",
            "Mejores practicas de desarrollo",
            "Arquitectura de software",
        ],
        "Science": [
            "Investigacion reciente",
            "Metodos cientificos",
            "Descubrimientos",
        ],
        "Work": [
            "Productividad",
            "Gestion de proyectos",
            "Habilidades blandas",
        ],
        "Security": [
            "Amenazas actuales",
            "Mejores practicas",
            "Herramientas de seguridad",
        ],
        "AI/ML": [
            "Modelos recientes",
            "Techniques de entrenamiento",
            "Aplicaciones practicas",
        ],
    }
    
    return suggestions.get(topic, ["Explorar topic", "Ver ejemplos", "Profundizar"])


def format_topic_report(analysis: Dict[str, Any]) -> str:
    """
    Formatear analisis de topics como reporte legible.
    """
    lines = ["# Analisis de Topics\n"]
    
    if not analysis.get("topics"):
        lines.append("No se encontraron topics significativos.\n")
        return "\n".join(lines)
    
    lines.append(f"Total de mensajes: {analysis['total_messages']}")
    lines.append(f"Topics encontrados: {analysis['total_topics']}\n")
    
    lines.append("## Topics por Frecuencia\n")
    for topic_data in analysis["topics"]:
        topic = topic_data["topic"]
        freq = topic_data["frequency"]
        sessions = topic_data["session_count"]
        
        lines.append(f"### {topic}")
        lines.append(f"- Frecuencia: {freq}")
        lines.append(f"- Sesiones: {sessions}")
        
        if topic_data["examples"]:
            lines.append("- Ejemplos:")
            for ex in topic_data["examples"][:3]:
                lines.append(f"  - [{ex['role']}] {ex['snippet'][:100]}...")
        
        lines.append("")
    
    return "\n".join(lines)
