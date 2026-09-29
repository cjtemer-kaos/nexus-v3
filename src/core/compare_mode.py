"""
Compare Mode - A/B testing entre modelos para SuperNEXUS v2.0

Inspirado en Odysseus:
- Crea dos sesiones efimeroas [CMP]-*
- Envia el mismo prompt a ambos modelos
- MODO CIEGO: mapeo aleatorio izquierda/derecha
- Voto del usuario (izquierda/derecha/empate)
- Revela identidades despues de votar
- Historial de comparaciones
"""

import asyncio
import logging
import random
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class Comparison:
    """Una comparacion A/B entre dos modelos."""
    id: str
    prompt: str
    model_a: str
    model_b: str
    endpoint_a: str
    endpoint_b: str
    is_blind: bool = True
    blind_mapping: Dict = field(default_factory=dict)
    session_left: str = ""
    session_right: str = ""
    response_left: str = ""
    response_right: str = ""
    winner: Optional[str] = None  # "left", "right", "tie", or None
    voted_at: Optional[str] = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())


class CompareMode:
    """Gestor de comparaciones A/B entre modelos."""

    def __init__(self, director=None):
        self.director = director
        self.comparisons: Dict[str, Comparison] = {}

    async def start_comparison(
        self,
        prompt: str,
        model_a: str,
        model_b: str,
        endpoint_a: str = "",
        endpoint_b: str = "",
        is_blind: bool = True,
    ) -> Dict:
        """
        Inicia una comparacion A/B.
        
        Returns:
            Dict con id, session_left, session_right, mapeo ciego
        """
        comp_id = str(uuid.uuid4())
        sid_left = str(uuid.uuid4())
        sid_right = str(uuid.uuid4())

        # Mapeo ciego aleatorio
        if is_blind:
            mapping = {"left": "a", "right": "b"}
            if random.random() > 0.5:
                mapping = {"left": "b", "right": "a"}
        else:
            mapping = {"left": "a", "right": "b"}

        comp = Comparison(
            id=comp_id,
            prompt=prompt,
            model_a=model_a,
            model_b=model_b,
            endpoint_a=endpoint_a,
            endpoint_b=endpoint_b,
            is_blind=is_blind,
            blind_mapping=mapping,
            session_left=sid_left,
            session_right=sid_right,
        )
        self.comparisons[comp_id] = comp

        # Ejecutar en paralelo
        left_model = model_a if mapping["left"] == "a" else model_b
        right_model = model_a if mapping["right"] == "a" else model_b
        left_endpoint = endpoint_a if mapping["left"] == "a" else endpoint_b
        right_endpoint = endpoint_a if mapping["right"] == "a" else endpoint_b

        async def _run_model(model, endpoint, slot):
            try:
                from src.core.provider_base import LLMMessage
                provider = self.director.provider_registry.get("gema-con-fallback")
                msgs = [LLMMessage(role="user", content=prompt)]
                response = await provider.chat(messages=msgs, model=model, temperature=0.7, max_tokens=2048)
                return response.content or ""
            except Exception as e:
                return f"Error: {str(e)}"

        # Ejecutar ambos en paralelo
        left_task = asyncio.create_task(_run_model(left_model, left_endpoint, "left"))
        right_task = asyncio.create_task(_run_model(right_model, right_endpoint, "right"))

        comp.response_left, comp.response_right = await asyncio.gather(left_task, right_task)

        return {
            "id": comp_id,
            "session_left": sid_left,
            "session_right": sid_right,
            "model_left": None if is_blind else left_model,
            "model_right": None if is_blind else right_model,
            "is_blind": is_blind,
            "response_left": comp.response_left,
            "response_right": comp.response_right,
        }

    async def vote(self, comp_id: str, winner: str) -> Dict:
        """
        Registra voto y revela identidades.
        
        Args:
            winner: "left", "right", o "tie"
        """
        comp = self.comparisons.get(comp_id)
        if not comp:
            return {"error": "Comparacion no encontrada"}
        if comp.winner:
            return {"error": "Ya se voto"}

        mapping = comp.blind_mapping

        if winner == "tie":
            comp.winner = "tie"
        elif winner == "left":
            comp.winner = mapping["left"]
        elif winner == "right":
            comp.winner = mapping["right"]
        else:
            return {"error": "winner debe ser 'left', 'right', o 'tie'"}

        comp.voted_at = datetime.now().isoformat()

        # Revelar identidades
        revealed = {
            "left": comp.model_a if mapping["left"] == "a" else comp.model_b,
            "right": comp.model_a if mapping["right"] == "a" else comp.model_b,
        }

        return {
            "winner": comp.winner,
            "model_a": comp.model_a,
            "model_b": comp.model_b,
            "revealed": revealed,
        }

    def get_history(self, limit: int = 20) -> List[Dict]:
        """Historial de comparaciones."""
        comps = sorted(
            self.comparisons.values(),
            key=lambda c: c.created_at,
            reverse=True,
        )[:limit]
        return [
            {
                "id": c.id,
                "prompt": c.prompt[:100],
                "model_a": c.model_a,
                "model_b": c.model_b,
                "winner": c.winner,
                "is_blind": c.is_blind,
                "voted_at": c.voted_at,
                "created_at": c.created_at,
            }
            for c in comps
        ]

    def get_stats(self) -> Dict:
        """Estadisticas de comparaciones."""
        wins_a = sum(1 for c in self.comparisons.values() if c.winner == "a")
        wins_b = sum(1 for c in self.comparisons.values() if c.winner == "b")
        ties = sum(1 for c in self.comparisons.values() if c.winner == "tie")
        total = len(self.comparisons)

        # Win rate por modelo
        model_stats = {}
        for c in self.comparisons.values():
            if c.winner and c.winner != "tie":
                winner_model = c.model_a if c.winner == "a" else c.model_b
                if winner_model not in model_stats:
                    model_stats[winner_model] = {"wins": 0, "losses": 0}
                model_stats[winner_model]["wins"] += 1
                loser_model = c.model_b if c.winner == "a" else c.model_a
                if loser_model not in model_stats:
                    model_stats[loser_model] = {"wins": 0, "losses": 0}
                model_stats[loser_model]["losses"] += 1

        return {
            "total": total,
            "wins_a": wins_a,
            "wins_b": wins_b,
            "ties": ties,
            "model_stats": model_stats,
        }
