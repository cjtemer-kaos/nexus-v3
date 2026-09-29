"""
ReflectionPattern - Patrón de reflexión para SuperNEXUS v2.0

El agente revisa y mejora su propio trabajo antes de entregarlo.
Basado en el patrón #9 del curso de Google: "Reflection"
"""

import logging
from typing import Dict, List, Any
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass
class ReflectionResult:
    """Resultado de reflexión"""
    original_output: str
    improved_output: str
    changes_made: List[str]
    confidence_before: float
    confidence_after: float
    reflection_rounds: int


class ReflectionPattern:
    """
    Patrón de reflexión: el agente revisa su propio trabajo.
    
    Flujo:
    1. Genera respuesta inicial
    2. Revisa errores, ambigüedades, mejoras
    3. Genera versión mejorada
    4. Repite hasta que confidence > threshold o max_rounds
    """
    
    def __init__(
        self,
        max_rounds: int = 3,
        confidence_threshold: float = 0.8,
        reflection_prompt: str = None,
    ):
        self.max_rounds = max_rounds
        self.confidence_threshold = confidence_threshold
        self.reflection_prompt = reflection_prompt or (
            "Revisa tu respuesta anterior. Identifica:\n"
            "1. Errores o inexactitudes\n"
            "2. Información faltante\n"
            "3. Ambigüedades o confusiones\n"
            "4. Mejoras posibles (claridad, estructura, ejemplos)\n\n"
            "Luego genera una versión mejorada."
        )
    
    async def reflect(
        self,
        task: str,
        initial_output: str,
        generate_func: Any,
        context: str = "",
    ) -> ReflectionResult:
        """
        Ejecuta ciclo de reflexión.
        
        Args:
            task: Tarea original
            initial_output: Respuesta inicial del agente
            generate_func: Función async para generar nueva respuesta
                          Signature: (prompt, context) -> str
            context: Contexto adicional
        """
        current_output = initial_output
        changes = []
        round_num = 0
        
        for round_num in range(1, self.max_rounds + 1):
            logger.info(f"Reflection round {round_num}/{self.max_rounds}")
            
            confidence = await self._assess_quality(task, current_output)
            
            if confidence >= self.confidence_threshold:
                logger.info(f"Reflection complete: confidence {confidence:.2f} >= threshold")
                break
            
            reflection_prompt = (
                f"Task: {task}\n\n"
                f"Your previous answer:\n{current_output}\n\n"
                f"{self.reflection_prompt}"
            )
            
            improved = await generate_func(reflection_prompt, context)
            
            diff = self._compute_diff(current_output, improved)
            if diff:
                changes.extend(diff)
            
            current_output = improved
        
        return ReflectionResult(
            original_output=initial_output,
            improved_output=current_output,
            changes_made=changes,
            confidence_before=await self._assess_quality(task, initial_output),
            confidence_after=await self._assess_quality(task, current_output),
            reflection_rounds=round_num,
        )
    
    async def _assess_quality(self, task: str, output: str) -> float:
        """Evalúa calidad de respuesta (heurística)"""
        score = 0.5
        
        if len(output) > 50:
            score += 0.1
        if len(output) > 200:
            score += 0.1
        
        if any(kw in output.lower() for kw in ["ejemplo", "example", "como", "how"]):
            score += 0.1
        
        if not any(kw in output.lower() for kw in ["no se", "no puedo", "no tengo"]):
            score += 0.1
        
        if output.count("\n") > 2:
            score += 0.1
        
        return min(1.0, score)
    
    def _compute_diff(self, old: str, new: str) -> List[str]:
        """Computa diferencias entre versiones"""
        changes = []
        
        if len(new) > len(old) * 1.2:
            changes.append(f"Added {len(new) - len(old)} characters")
        elif len(new) < len(old) * 0.8:
            changes.append(f"Removed {len(old) - len(new)} characters (conciseness)")
        
        old_lower = old.lower()
        new_lower = new.lower()
        
        if "example" in new_lower and "example" not in old_lower:
            changes.append("Added example")
        if "step" in new_lower and "step" not in old_lower:
            changes.append("Added step-by-step structure")
        
        return changes
    
    def get_status(self) -> Dict:
        return {
            "max_rounds": self.max_rounds,
            "confidence_threshold": self.confidence_threshold,
            "pattern": "reflection",
        }
