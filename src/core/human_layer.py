"""
human_layer.py — Capa de comportamiento humano para NEXUS.

No es una skill que se carga bajo demanda. Es una capa transversal:
  - System prompt injectado en todas las gemas (nivel harness)
  - Pipeline de salida que limpia texto robótico (nivel post-procesamiento)
  - Evaluador de naturalidad para feedback loop (nivel calidad)

Uso:
  from src.core.human_layer import humanize_output, evaluate_naturalness
  text = humanize_output(raw_text)
  score = evaluate_naturalness(text)
"""

import re
from typing import Dict

# ============================================================
# SECCIÓN HARNESS — se inyecta en get_context_prompt()
# ============================================================

HUMAN_LAYER_PROMPT = """## HUMAN LAYER — Reglas de expresión natural

Escribís como humano, no como LLM. Esto significa:

1. **VARIACIÓN**: alterná oraciones cortas y largas. No todo párrafo de 3 líneas iguales.
2. **VOCABULARIO**: usá lenguaje natural, no de manual técnico. Evitá "Moreover",
   "Furthermore", "In addition". Preferí "Además", "El tema es que", "O sea".
3. **PERSONALIDAD**: adaptá el tono al usuario. Sin exagerar. Sin ser payaso.
4. **IMPERFECCIÓN**: una duda o pausa de vez en cuando humaniza. Sin abusar.
5. **SIN BRIDGE PHRASES**: no empieces con "Sure!", "Let me...", "I'd be happy to".
   Andá al grano. "Dale, vamos." "Mira, funciona así."
6. **SIN CONCLUSIONES DE ENSAYO**: no resumas lo que ya dijiste. Cerra y listo.
7. **CONTRACCIONES**: usalas. "No hay", "Está", "Va a ser".
8. **ASIMETRÍA**: si listás 3 puntos, que no tengan todos la misma estructura.
9. **SIN AUTO-CORRECCIÓN**: no digas "I apologize", "Let me clarify". Decí lo
   que tengas que decir y seguí.
10. **NATURAL > CORRECTO**: preferí sonar natural antes que perfecto.
"""


# ============================================================
# SECCIÓN PIPELINE — humanización post-generación
# ============================================================

# Patrones de texto robótico a detectar/remover
_AI_BRIDGE_PHRASES = [
    (r"^(Sure!|Of course!|Absolutely!|I'd be happy to)\s+", ""),
    (r"^(Claro!|Por supuesto!|Con gusto)\s+", ""),
    (r"\s+(Thank you for your (question|interest|message))\.?\s*$", ""),
    (r"\s+(Thanks for reaching out)\.?\s*$", ""),
    (r"\s+(Please let me know if you have any questions)\.?\s*$", ""),
    (r"\s+(Feel free to ask if anything is unclear)\.?\s*$", ""),
    (r"\s+(Don't hesitate to reach out)\.?\s*$", ""),
    (r"\s+(I hope this helps)\.?\s*$", ""),
    (r"\b(Moreover|Furthermore|Nevertheless|Consequently)\b", lambda m: _alt_transition(m.group(0))),
    (r"\b(In addition|Additionally)\b", "Además"),
    (r"\b(It is important to note that)\b", "Ojo:"),
    (r"\b(It is worth mentioning that)\b", "Cabe decir que"),
    (r"\b(It should be noted that)\b", ""),
    (r"\b(Please find below)\b", "Aquí"),
    (r"\b(As previously mentioned)\b", "Como dije"),
    (r"\b(In order to)\b", "Para"),
    (r"\b(With this in mind)\b", "Con esto"),
    (r"\b(Having said that)\b", "Dicho esto"),
    (r"\b(Last but not least)\b", "Finalmente"),
    (r"\b(in the context of)\b", "en"),
]

_AI_TRANSITIONS = {
    "Moreover": "Además",
    "Furthermore": "Además",
    "Nevertheless": "Igual",
    "Consequently": "Por eso",
}


def _alt_transition(word: str) -> str:
    return _AI_TRANSITIONS.get(word, word)


def humanize_output(text: str) -> str:
    """Limpia texto de patrones robóticos LLM.

    No es una humanización profunda — es una poda de los peores anti-patrones.
    La humanización real viene del system prompt (nivel harness), no de aquí.
    """
    result = text.strip()
    for pattern, replacement in _AI_BRIDGE_PHRASES:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE | re.MULTILINE)
    # Multiple newlines -> double max
    result = re.sub(r"\n{3,}", "\n\n", result)
    result = result.strip()
    return result


# ============================================================
# SECCIÓN EVALUADOR — cuán robótico suena un texto
# ============================================================

# Palabras de transición formales (marcadores de LLM)
_HIGH_REGISTER = {
    "moreover", "furthermore", "nevertheless", "consequently", "additionally",
    "notably", "namely", "heretofore", "thereafter", "wherein", "whereby",
    "herein", "therein", "aforesaid", "aforementioned", "hereunder",
    "thereunder", "henceforth", "thence", "thenceforth",
}

# Bridge phrases
_BRIDGES = {
    "sure!", "of course!", "absolutely!", "i'd be happy to",
    "thank you for your question", "thank you for reaching out",
    "thanks for your message", "let me explain", "let me show you",
    "i'd recommend", "i would suggest", "please find below",
    "feel free to ask", "don't hesitate", "i hope this helps",
    "as always", "as previously mentioned", "it is important to note",
    "it is worth mentioning", "it should be noted",
}


def evaluate_naturalness(text: str) -> Dict:
    """Evalúa cuán natural vs robótico suena un texto.

    Returns:
        Dict con score (0=robótico, 100=natural), métricas, sugerencias.
    """
    if not text or len(text) < 20:
        return {"score": 50, "length": 0, "issues": [], "suggestions": ["Texto demasiado corto para evaluar"]}

    lower = text.lower()
    issues = []
    suggestions = []

    # 1. Bridge phrases
    for phrase in _BRIDGES:
        if phrase in lower:
            issues.append(f"bridge_phrase:{phrase}")
            suggestions.append(f"Eliminá '{phrase}' — andá al grano")

    # 2. High-register transitions
    words = set(re.findall(r"\b[a-záéíóúñ]+\b", lower))
    hr_words = words & _HIGH_REGISTER
    for w in hr_words:
        issues.append(f"formal_transition:{w}")
        suggestions.append(f"Cambiá '{w}' por algo más natural (ej: 'además', 'igual', 'por eso')")

    # 3. Sentence length diversity
    sentences = re.split(r"[.!?]+", text)
    sentences = [s.strip() for s in sentences if len(s.strip()) > 10]
    if len(sentences) >= 3:
        lengths = [len(s.split()) for s in sentences]
        mean_len = sum(lengths) / len(lengths)
        variance = sum((length - mean_len) ** 2 for length in lengths) / len(lengths)
        std_dev = variance ** 0.5
        if std_dev < 3:
            issues.append("low_variance")
            suggestions.append("Variá la longitud de las oraciones — mezclá cortas y largas")
    else:
        std_dev = 0

    # 4. Perfect uniformity (ensayo)
    if len(sentences) >= 3:
        first_words = [s.split()[0].lower() if s.split() else "" for s in sentences]
        unique_starts = len(set(first_words))
        if unique_starts == 1:
            issues.append("uniform_starts")
            suggestions.append("No empieces todas las oraciones igual")

    # 5. Ratio de contracciones (señal humana)
    contractions = len(re.findall(r"\b(no hay|está|va a ser|dime|hacemos|vamos|déjame)\b", lower))
    contraction_ratio = contractions / max(1, len(sentences))

    # Score compuesto
    base = 80
    base -= len(issues) * 8
    if std_dev < 3:
        base -= 5
    if contraction_ratio < 0.05:
        base -= 3  # muy pocas contracciones
    if contraction_ratio > 0.3:
        base += 5  # muchas contracciones = natural

    score = max(0, min(100, base))

    return {
        "score": score,
        "is_natural": score >= 60,
        "length": len(text),
        "sentences": len(sentences),
        "issues": issues[:5],
        "suggestions": suggestions[:3],
        "metrics": {
            "sentence_len_std": round(std_dev, 1),
            "contraction_ratio": round(contraction_ratio, 2),
            "formal_words": list(hr_words),
            "bridge_phrases": [p for p in _BRIDGES if p in lower],
        },
    }
