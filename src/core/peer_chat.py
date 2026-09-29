"""
PeerChat - Sistema de chat y autoaprendizaje PC1 ↔ PC2
Dos máquinas independientes conversan, colaboran y aprenden entre sí

Arquitectura de cerebro compartido:
  - ~/.nexus/peer_chat/ → tablero de mensajes + conocimiento aprendido
  - ~/.nexus/brain/hybrid_memory.db → memoria sincronizada entre PC1 y PC2
  - Cada ciclo de aprendizaje sincroniza ambos cerebros via HTTP/SCP
"""

import asyncio
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx

from .vram_router import decide_num_gpu
from src.security.atomic_io import atomic_write_json
from src.core import nexus_config

logger = logging.getLogger(__name__)

MODEL_MAP = {
    "coding": "nexus-coder",
    "code": "nexus-coder",
    "math": "nexus-coder",
    "general": "qwen2.5-coder:7b",
    "research": "nexus-researcher",
    "architecture": "nexus-researcher",
    "creative": "qwen2.5-coder:7b",
    "meta": "qwen2.5-coder:7b",
    "simple": "nemotron-3-nano:4b",
}

# Judge model for LLM-as-Judge evaluation
JUDGE_MODEL = "nexus-judge"

# Simple keywords that don't need big model reasoning
_SIMPLE_PATTERNS = re.compile(
    r"(?:^|[\s,.;:!?])(?:hi|hello|yes|no|ok|thanks|good|bad|list|show|"
    r"what is|who is|when|where|define|meaning|short|quick|simple|status|ping|"
    r"summarize|translate|convert|format)(?:$|[\s,.;:!?])",
    re.IGNORECASE,
)


class RAGMemory:
    """In-memory RAG using CPU embeddings via httpx to Ollama embedding endpoint."""

    def __init__(self, ollama_url: str = "http://127.0.0.1:11434", embed_model: str = "nomic-embed-text"):
        self.ollama_url = ollama_url
        self.embed_model = embed_model
        self._docs: List[Tuple[str, str, List[float]]] = []  # (category, text, embedding)
        self._client = httpx.AsyncClient(timeout=10)

    async def _embed(self, text: str) -> List[float]:
        try:
            r = await self._client.post(
                f"{self.ollama_url}/api/embeddings",
                json={"model": self.embed_model, "prompt": text},
            )
            if r.status_code == 200:
                return r.json().get("embedding", [])
        except Exception:
            pass
        return []

    async def add(self, category: str, text: str):
        emb = await self._embed(text[:512])
        if emb:
            self._docs.append((category, text, emb))

    async def search(self, query: str, category: str = "", top_k: int = 3) -> List[str]:
        qemb = await self._embed(query[:512])
        if not qemb or not self._docs:
            return []
        scored = []
        for cat, text, emb in self._docs:
            if category and cat != category:
                continue
            sim = sum(a * b for a, b in zip(qemb, emb))
            norm = (sum(a * a for a in qemb) ** 0.5) * (sum(b * b for b in emb) ** 0.5)
            if norm > 0:
                scored.append((sim / norm, text))
        scored.sort(key=lambda x: -x[0])
        return [t for _, t in scored[:top_k]]

    def count(self) -> int:
        return len(self._docs)

    async def close(self):
        await self._client.aclose()


class PeerNode:
    """Representa un nodo par (PC1 o PC2)"""

    def __init__(self, node_id: str, name: str, api_url: str, ollama_url: str):
        self.id = node_id
        self.name = name
        self.api_url = api_url
        self.ollama_url = ollama_url
        self.online = False
        self.capabilities: List[str] = []
        self.last_seen: Optional[float] = None
        self.conversation: List[Dict] = []
        self._timeout = 30 if "127.0.0.1" in api_url else 60
        self._client = httpx.AsyncClient(timeout=self._timeout)
        # Smart timeout tracking (#9)
        self._response_times: Dict[str, List[float]] = {}

    @staticmethod
    def _is_simple_task(task: str) -> bool:
        """Detecta tareas simples que nemotron puede manejar."""
        if len(task) < 60:
            return True
        if bool(_SIMPLE_PATTERNS.search(task)):
            return True
        return False

    def record_response_time(self, category: str, elapsed: float):
        if category not in self._response_times:
            self._response_times[category] = []
        self._response_times[category].append(elapsed)
        if len(self._response_times[category]) > 20:
            self._response_times[category] = self._response_times[category][-20:]

    def get_smart_timeout(self, category: str, min_t: float = 15, max_t: float = 120) -> float:
        times = self._response_times.get(category, [])
        if len(times) < 3:
            return self._timeout
        avg = sum(times) / len(times)
        var = sum((t - avg) ** 2 for t in times) / len(times)
        std = var ** 0.5
        dynamic = avg + 2 * std + 5
        return max(min_t, min(dynamic, max_t))

    async def check_health(self) -> bool:
        for endpoint in ["/health", "/api/status"]:
            try:
                r = await self._client.get(f"{self.api_url}{endpoint}", timeout=5)
                if r.status_code == 200:
                    self.online = True
                    self.last_seen = time.time()
                    return True
            except Exception:
                pass
        self.online = False
        return False

    async def send_message(self, message: str, category: str = "general", force_model: str = "") -> Optional[str]:
        # Nemotron pre-filter: simple tasks use cheap model
        if not force_model and self._is_simple_task(message):
            model = "nemotron-3-nano:4b"
        else:
            model = force_model or MODEL_MAP.get(category, "qwen2.5-coder:7b")
        timeout = self.get_smart_timeout(category)
        try:
            # Si qwen2.5-coder no cabe en VRAM, usar gemma4 (ya cargada) en vez de CPU
            if model in ("qwen2.5-coder:7b", "nexus-coder"):
                num_gpu = decide_num_gpu(model)
                if num_gpu == 0:
                    model = "qwen2.5-coder:7b"
            else:
                num_gpu = decide_num_gpu(model)
            r = await self._client.post(
                f"{self.ollama_url}/api/generate",
                json={"model": model, "prompt": message, "stream": False, "options": {"num_predict": 500, "num_ctx": 2048, "num_batch": 1024, "num_gpu": num_gpu}},
                timeout=timeout,
            )
            if r.status_code == 200:
                data = r.json()
                reply = data.get("response", "") or data.get("thinking", "") or data.get("content", "")
                # deepseek-r1 sometimes puts output in 'thinking' field with empty 'response'
                if reply:
                    self.conversation.append({"role": "user", "content": message, "ts": time.time()})
                    self.conversation.append({"role": "assistant", "content": reply, "ts": time.time()})
                    return reply
        except Exception as e:
            logger.warning(f"Ollama generate failed for {self.name}: {e}")

        for payload in [
            {"message": message, "prompt": message},
            {"prompt": message},
            {"message": message},
        ]:
            try:
                r = await self._client.post(
                    f"{self.api_url}/api/chat",
                    json=payload,
                    timeout=self._timeout,
                )
                if r.status_code == 200:
                    data = r.json()
                    reply = data.get("reply", "") or data.get("response", "") or data.get("content", "") or str(data)
                    if reply and "Falta prompt" not in reply:
                        self.conversation.append({"role": "user", "content": message, "ts": time.time()})
                        self.conversation.append({"role": "assistant", "content": reply, "ts": time.time()})
                        return reply
            except Exception:
                continue

        logger.warning(f"send_message to {self.name} failed: all paths exhausted")
        return None

    async def execute_task(self, task: str, category: str = "general", context: str = "") -> Dict:
        start = time.time()
        prompt = f"{context}\n\nTask: {task}" if context else task
        reply = await self.send_message(prompt, category=category)
        elapsed = time.time() - start
        self.record_response_time(category, elapsed)
        return {
            "node": self.name,
            "task": task,
            "category": category,
            "response": reply or "(no response)",
            "response_length": len(reply or ""),
            "elapsed": round(elapsed, 2),
            "success": reply is not None,
            "ts": time.time(),
        }

    async def sync_brain_to(self, local_brain: Path, dest_api: str) -> bool:
        files = {
            "hybrid_memory.db": local_brain / "hybrid_memory.db",
            "neural.db": local_brain / "neural.db",
        }
        ok = True
        for name, path in files.items():
            if path.exists():
                try:
                    r = await self._client.post(
                        f"{dest_api}/api/brain/sync",
                        files={"file": (name, open(path, "rb"), "application/octet-stream")},
                        timeout=30,
                    )
                    if r.status_code == 200:
                        logger.info(f"Brain synced: {name} -> {self.name}")
                    else:
                        logger.warning(f"Brain sync {name} -> {self.name} returned {r.status_code}")
                        ok = False
                except Exception as e:
                    logger.warning(f"Brain sync {name} -> {self.name} failed: {e}")
                    ok = False
        return ok

    async def close(self):
        await self._client.aclose()


class PeerChat:
    """
    Sistema de chat y aprendizaje colaborativo entre dos peers.
    - Mantiene un tablero de mensajes compartido
    - Asigna tareas a ambos peers y recolecta resultados
    - Guarda el mejor resultado como aprendizaje compartido
    - Sincroniza conocimiento entre máquinas
    """

    def __init__(self, pc1_port: int = nexus_config.get_port(), pc2_host: str = os.environ.get("SUPER_NEXUS_PC2_IP", "localhost"), pc2_port: int = 9000):
        self.pc1 = PeerNode("pc1", "PC1 (Director)", f"http://127.0.0.1:{pc1_port}", "http://127.0.0.1:11434")
        self.pc2 = PeerNode("pc2", "PC2 (Remoto)", f"http://{pc2_host}:{pc2_port}", f"http://{pc2_host}:11434")

        shared_dir = Path.home() / ".nexus" / "peer_chat"
        shared_dir.mkdir(parents=True, exist_ok=True)
        self.message_board_path = shared_dir / "message_board.json"
        self.learned_knowledge_path = shared_dir / "learned_knowledge.json"
        self.collaboration_log_path = shared_dir / "collaboration_log.jsonl"
        self.benchmark_path = shared_dir / "benchmark_results.json"

        self.message_board: List[Dict] = self._load_json(self.message_board_path)
        self.learned_knowledge: List[Dict] = self._load_json(self.learned_knowledge_path)
        self.benchmark_results: List[Dict] = self._load_json(self.benchmark_path)
        self.running = False
        self.callbacks = []
        self._win_stats: Dict = {"pc1": {}, "pc2": {}}
        self._rag = RAGMemory()
        self._rag_indexed = False

    async def _index_knowledge_rag(self):
        """Indexa conocimiento aprendido en RAG."""
        for entry in self.learned_knowledge:
            cat = entry.get("category", "general")
            text = entry.get("best_response", "") or entry.get("task", "")
            if text:
                await self._rag.add(cat, text[:1000])
        logger.info(f"RAG indexed {len(self.learned_knowledge)} entries")

    def _load_json(self, path: Path) -> list:
        if path.exists():
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return []
        return []

    def _save_json(self, path: Path, data: list):
        atomic_write_json(path, data, indent=2)

    def on_message(self, callback):
        self.callbacks.append(callback)

    def post_message(self, sender: str, content: str, msg_type: str = "chat"):
        entry = {
            "id": f"{sender}_{int(time.time())}_{len(self.message_board)}",
            "sender": sender,
            "type": msg_type,
            "content": content,
            "ts": time.time(),
            "ts_human": datetime.now().isoformat(),
        }
        self.message_board.append(entry)
        self._save_json(self.message_board_path, self.message_board)
        logger.info(f"[{sender}] {content[:80]}...")
        for cb in self.callbacks:
            cb(entry)
        return entry

    async def ping(self) -> Dict:
        pc1_ok = await self.pc1.check_health()
        pc2_ok = await self.pc2.check_health()
        return {
            "pc1": {"online": pc1_ok, "url": self.pc1.api_url, "last_seen": self.pc1.last_seen},
            "pc2": {"online": pc2_ok, "url": self.pc2.api_url, "last_seen": self.pc2.last_seen},
        }

    async def peer_conversation(self, rounds: int = 1, topic: str = None) -> List[Dict]:
        """
        Conversacion paralela entre PC1 y PC2 sobre un tema.
        """
        if not topic:
            topic = "Discuss the best approach for improving AI agent self-learning capabilities"

        self.post_message("system", f"Iniciando conversacion: {topic}", "conversation_start")
        history = []

        for r in range(rounds):
            prompt_a = f"As {self.pc1.name}, respond to: {topic}. Be concise and technical."
            prompt_b = f"As {self.pc2.name}, review your peer's idea and add your own perspective."
            replies = await asyncio.gather(
                self.pc1.send_message(prompt_a),
                self.pc2.send_message(prompt_b),
                return_exceptions=True,
            )
            for peer, reply in [(self.pc1, replies[0]), (self.pc2, replies[1])]:
                if isinstance(reply, Exception) or not reply:
                    continue
                self.post_message(peer.name, str(reply)[:200], "chat")
                history.append({"round": r, "peer": peer.name, "response": str(reply)})

        self.post_message("system", f"Conversacion completada ({rounds} rondas)", "conversation_end")
        return history

    async def collaborative_task(self, task: str, category: str = "general", require_consensus: bool = False) -> Dict:
        """
        Asigna tarea a peers segun MoE routing (#10), paralelo, judge multi-criterio.
        Si require_consensus=True, solo acepta si ambos peers estan de acuerdo.
        """
        self.post_message("system", f"Tarea colaborativa: {task[:80]}...", "task_start")

        # Lazy RAG index on first use
        if not self._rag_indexed:
            await self._index_knowledge_rag()
            self._rag_indexed = True

        # RAG context: buscar conocimiento similar antes de generar
        rag_docs = await self._rag.search(task, category, top_k=2)
        context = self._build_context(category, task)
        if rag_docs:
            rag_section = "Relevant knowledge:\n" + "\n---\n".join(d[:300] for d in rag_docs)
            context = f"{context}\n\n{rag_section}" if context else rag_section

        # Consensus mode (#12): tareas criticas requieren ambos peers
        if require_consensus or category in ("security", "architecture", "critical"):
            peers = ["pc1", "pc2"]
        else:
            peers = self._select_peers(category)
        peer_map = {"pc1": self.pc1, "pc2": self.pc2}
        targets = [peer_map[p] for p in peers if p in peer_map]

        if not targets:
            targets = [self.pc1, self.pc2]

        results_raw = await asyncio.gather(
            *(p.execute_task(task, category, context) for p in targets),
            return_exceptions=True,
        )

        results = []
        for r in results_raw:
            if isinstance(r, Exception):
                continue
            if r.get("success"):
                results.append(r)

        if not results:
            return {"success": False, "error": "Ambos peers fallaron", "task": task}

        # Consensus mode (#12): verificar acuerdo en respuestas
        consensus_score = 0.0
        if require_consensus or category in ("security", "architecture", "critical"):
            if len(results) > 1:
                consensus_score = self._check_consensus(results)
                if consensus_score < 0.6:
                    # Si no hay consenso, pedir aclaracion a cada peer
                    for r in results:
                        clarify = await self.pc1.send_message(
                            f"Task: {task}\n\nYour answer: {r['response'][:300]}\n\n"
                            f"Another peer answered differently. Explain your reasoning.",
                            category="meta",
                        )
                        if clarify:
                            r["response"] += f"\n[Clarification: {clarify[:200]}]"
                    # Re-evaluar consenso
                    consensus_score = self._check_consensus(results)

        best = await self._judge_best(task, results)

        # Multi-round debate (#8): si scores cercanos, perdedor refina
        if len(results) > 1 and best.get("scores"):
            loser = [r for r in results if r["node"] != best["node"]]
            if loser:
                a_scores = best.get("scores", {})
                b_scores = loser[0].get("scores", {})
                a_avg = (a_scores.get("relevance", 0) + a_scores.get("precision", 0) + a_scores.get("completeness", 0)) / 3
                b_avg = (b_scores.get("relevance", 0) + b_scores.get("precision", 0) + b_scores.get("completeness", 0)) / 3
                max_score = max(a_avg, b_avg)
                min_score = min(a_avg, b_avg)
                if max_score > 0 and (min_score / max_score) >= 0.8:
                    refine_prompt = (
                        f"Task: {task}\n\nYour previous answer:\n{loser[0]['response'][:300]}\n\n"
                        f"Your peer ({best['node']}) answered:\n{best['response'][:300]}\n\n"
                        "Refine your answer incorporating the strong points you see. Be concise."
                    )
                    refined = await self.pc1.send_message(refine_prompt, category=category)
                    if refined:
                        loser[0]["response"] = refined
                        loser[0]["response_length"] = len(refined)
                        loser[0]["refined"] = True
                        # Re-juzgar
                        best = await self._judge_best(task, results)

        scores = best.get("scores", {})
        avg = scores.get("average", 0) or (scores.get("relevance", 0) + scores.get("precision", 0) + scores.get("completeness", 0)) / 3

        knowledge_entry = {
            "ts": time.time(),
            "task": task,
            "category": category,
            "best_response": best["response"],
            "best_node": best["node"],
            "judge_reason": best.get("judge_reason", ""),
            "scores": best.get("scores", {}),
            "score_avg": round(avg, 2),
            "consensus_score": round(consensus_score, 2),
            "competitors": [{"node": r["node"], "length": r["response_length"], "elapsed": r["elapsed"]} for r in results],
        }
        self.learned_knowledge.append(knowledge_entry)
        self._save_json(self.learned_knowledge_path, self.learned_knowledge)
        # Indexar en RAG
        asyncio.ensure_future(self._rag.add(category, f"{task}\n{best['response'][:500]}"))

        self.post_message("system", f"Ganador: {best['node']} (score: {avg:.1f}/10)", "task_result")

        # Track win stats por categoria (#7)
        for r in results:
            self._update_win_stats(r["node"], category, r["node"] == best["node"])

        # Extraer leccion del perdedor (#6)
        if len(results) > 1:
            loser = [r for r in results if r["node"] != best["node"]]
            if loser:
                lesson_prompt = (
                    f"Task: {task}\n\nWinner ({best['node']}):\n{best['response'][:300]}\n\n"
                    f"Loser ({loser[0]['node']}):\n{loser[0]['response'][:300]}\n\n"
                    "In one sentence, what made the winner better? Be specific."
                )
                lesson = await self.pc1.send_message(lesson_prompt, category="meta")
                if lesson:
                    knowledge_entry["lesson"] = lesson.strip()[:200]
                    # Re-save with lesson
                    self.learned_knowledge[-1] = knowledge_entry
                    self._save_json(self.learned_knowledge_path, self.learned_knowledge)

        with open(self.collaboration_log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(knowledge_entry, ensure_ascii=False) + "\n")

        return knowledge_entry

    def _build_context(self, category: str = "", task: str = "") -> str:
        """Construye contexto: codebase relevante + ultimos mensajes + lecciones aprendidas (few-shot)."""
        parts = []

        # Codebase context: inyectar codigo relevante a la tarea
        if task:
            try:
                from core.codebase_context import get_instance
                cc = get_instance()
                # Use existing repomix dump or query
                query = f"{category} {task}"[:100]
                cc_result = asyncio.run(cc.query_context(query=query, max_results=2))
                if cc_result and "No codebase results" not in cc_result:
                    parts.append("Codebase context:\n" + cc_result[:1000])
            except Exception:
                pass  # codebase_context no disponible, seguir sin el

        # Recent messages
        recent = self.message_board[-3:]
        if recent:
            lines = [f"[{m['sender']}] {m.get('content', '')[:120]}" for m in recent]
            parts.append("Recent context:\n" + "\n".join(lines))

        # Lessons learned (few-shot)
        if category and self.learned_knowledge:
            lessons = [k for k in self.learned_knowledge if k.get("category") == category and k.get("lesson")]
            if lessons:
                shots = lessons[-3:]
                lesson_lines = [f"Lesson: {shot['lesson']}" for shot in shots]
                parts.append("Lessons learned:\n" + "\n".join(lesson_lines))

        return "\n\n".join(parts) if parts else ""

    async def _judge_best(self, task: str, results: List[Dict]) -> Dict:
        """Usa LLM-as-Judge multi-criterio: relevancia, precision, completitud (1-10)."""
        if len(results) == 1:
            results[0]["scores"] = {"relevance": 5, "precision": 5, "completeness": 5, "average": 5.0}
            return results[0]

        a, b = results[0], results[1]
        judge_prompt = (
            f"Task: {task}\n\n"
            f"A ({a['node']}):\n{a['response'][:500]}\n\n"
            f"B ({b['node']}):\n{b['response'][:500]}\n\n"
            "Evaluate both responses on these criteria (1-10 scale):\n"
            "- relevance: how well it addresses the task\n"
            "- precision: correctness and accuracy\n"
            "- completeness: thoroughness and detail\n\n"
            "Respond ONLY with JSON:\n"
            '{"winner": "A" or "B", "reason": "one sentence", "scores": {"A": {"relevance": N, "precision": N, "completeness": N}, "B": ...}}'
        )
        judge_reply = await self.pc1.send_message(judge_prompt, category="meta", force_model=JUDGE_MODEL)
        parsed = self._parse_judge(judge_reply, a, b)
        winner = parsed["winner"]
        winner["scores"] = parsed["winner_scores"]
        winner["judge_reason"] = parsed["reason"]
        return winner

    def _parse_judge(self, reply: str, a: Dict, b: Dict) -> Dict:
        """Parsea respuesta JSON del judge con fallback a formato simple."""
        if not reply:
            return self._judge_fallback(a, b, "No judge reply")
        try:
            match = re.search(r'\{.*\}', reply, re.DOTALL)
            if match:
                data = json.loads(match.group())
            else:
                data = json.loads(reply)
            winner_node = a if data.get("winner", "").strip().upper() == "A" else b
            scores = data.get("scores", {})
            winner_key = "A" if winner_node["node"] == a["node"] else "B"
            winner_scores = scores.get(winner_key, {})
            reason = data.get("reason", "")[:200]
            return {"winner": winner_node, "winner_scores": winner_scores, "reason": reason}
        except (json.JSONDecodeError, KeyError, AttributeError):
            pass
        # Fallback: inicio de respuesta (A/B)
        stripped = reply.strip().upper()
        if stripped.startswith("A"):
            return self._judge_fallback(a, b, reply[:200])
        return self._judge_fallback(b, a, reply[:200])

    def _judge_fallback(self, winner: Dict, loser: Dict, reason: str) -> Dict:
        """Fallback cuando el JSON no se puede parsear."""
        return {
            "winner": winner,
            "winner_scores": {"relevance": 5, "precision": 5, "completeness": 5, "average": 5.0},
            "reason": reason,
        }

    async def learn_from_best(self, tasks: List[str], categories: List[str] = None) -> Dict:
        """Ejecuta multiples tareas colaborativas y compila el conocimiento compartido."""
        results = []
        for i, task in enumerate(tasks):
            cat = categories[i] if categories and i < len(categories) else "general"
            r = await self.collaborative_task(task, cat)
            results.append(r)
            await asyncio.sleep(0.5)

        stats = {}
        for r in results:
            winner = r.get("best_node", "unknown")
            stats[winner] = stats.get(winner, 0) + 1

        # Sync brain al otro peer
        await self.sync_brain()

        return {
            "total_tasks": len(tasks),
            "win_stats": stats,
            "knowledge_file": str(self.learned_knowledge_path),
            "results": results,
        }

    async def sync_brain(self):
        """Sincroniza el cerebro compartido entre PC1 y PC2"""
        brain_dir = Path.home() / ".nexus" / "brain"
        if self.pc2.online and brain_dir.exists():
            ok = await self.pc2.sync_brain_to(brain_dir, self.pc2.api_url)
            if ok:
                logger.info("Brain synced to PC2 successfully")
            else:
                logger.warning("Brain sync to PC2 had issues (non-critical)")
        # Also sync peer_chat knowledge directory
        chat_dir = Path.home() / ".nexus" / "peer_chat"
        if chat_dir.exists():
            for f in chat_dir.iterdir():
                if f.suffix in (".json", ".jsonl"):
                    pass  # Files are shared via the same .nexus path
        return True

    async def send_message_to_peer(self, message: str, target: PeerNode) -> Optional[str]:
        """Envia un mensaje directo a un peer."""
        return await target.send_message(message)

    # ---- Tareas Adaptativas (#7) ----

    def _ensure_win_stats(self, node: str, category: str):
        """Asegura que exista entrada de win_stats para nodo/categoria."""
        if category not in self._win_stats.get(node, {}):
            self._win_stats.setdefault(node, {})[category] = {"wins": 0, "losses": 0, "total": 0}

    def _update_win_stats(self, node: str, category: str, won: bool):
        """Actualiza win_stats para un nodo y categoria."""
        self._ensure_win_stats(node, category)
        self._win_stats[node][category]["total"] += 1
        if won:
            self._win_stats[node][category]["wins"] += 1
        else:
            self._win_stats[node][category]["losses"] += 1

    def _get_weak_categories(self, min_samples: int = 2) -> List[tuple]:
        """Retorna categorias donde un nodo tiene win_rate < 50%."""
        weak = []
        for node, cats in self._win_stats.items():
            for cat, stats in cats.items():
                if stats["total"] >= min_samples:
                    rate = stats["wins"] / stats["total"]
                    if rate < 0.5:
                        weak.append((node, cat, round(rate, 2)))
        return weak

    # ---- Mixture of Experts routing (#10) ----

    _MOE_ROUTES = {
        "coding": "pc2", "code": "pc2", "math": "pc2",
        "general": "dual", "research": "pc1", "creative": "dual",
        "meta": "pc1", "architecture": "dual",
    }

    def _select_peers(self, category: str) -> List[str]:
        """Elige que peers ejecutan segun categoria y win_rate."""
        route = self._MOE_ROUTES.get(category, "dual")
        if route != "dual":
            return [route]
        preferred = "pc2" if self._win_stats.get("pc2", {}).get(category, {}).get("wins", 0) > \
                           self._win_stats.get("pc1", {}).get(category, {}).get("wins", 0) else "pc1"
        return [preferred, "pc2" if preferred == "pc1" else "pc1"]

    def _check_consensus(self, results: List[Dict]) -> float:
        """Mide consenso entre peers via solapamiento de palabras clave."""
        if len(results) < 2:
            return 1.0
        words_list = []
        for r in results:
            words = set(r.get("response", "").lower().split())
            words = {w for w in words if len(w) > 3}
            words_list.append(words)
        if not all(words_list):
            return 0.0
        common = words_list[0].intersection(*words_list[1:])
        allw = words_list[0].union(*words_list[1:])
        return len(common) / len(allw) if allw else 0.0

    # ---- Auto Benchmark (#11) ----

    _BENCHMARK_TASKS = {
        "coding": [
            "Write a function to reverse a linked list in Python",
            "Implement binary search in Python",
            "Write a SQL query to find duplicate emails",
        ],
        "math": [
            "What is 15 * 37 + 42 / 2?",
            "If a train travels 60 mph for 2.5 hours, how far does it go?",
        ],
        "general": [
            "Summarize the water cycle in 3 sentences",
            "List 3 benefits of functional programming",
        ],
        "research": [
            "Explain the difference between TCP and UDP",
            "What is the CAP theorem?",
        ],
    }

    async def run_benchmark(self, categories: List[str] = None) -> Dict:
        """Ejecuta benchmark en categorias especificas."""
        if not categories:
            categories = list(self._BENCHMARK_TASKS.keys())
        results = {}
        for cat in categories:
            tasks = self._BENCHMARK_TASKS.get(cat, [])
            if not tasks:
                continue
            cat_results = []
            for task in tasks:
                r = await self.collaborative_task(task, cat)
                cat_results.append({
                    "task": task,
                    "winner": r.get("best_node", "?"),
                    "score": r.get("score_avg", 0),
                    "elapsed": r.get("competitors", [{}])[0].get("elapsed", 0) if r.get("competitors") else 0,
                })
            wins = sum(1 for c in cat_results if c["winner"] == "pc1")
            results[cat] = {
                "total": len(cat_results),
                "pc1_wins": wins,
                "pc2_wins": len(cat_results) - wins,
                "avg_score": round(sum(c["score"] for c in cat_results) / len(cat_results), 2) if cat_results else 0,
                "tasks": cat_results,
            }
        benchmark_entry = {
            "ts": time.time(),
            "ts_human": datetime.now().isoformat(),
            "results": results,
        }
        self.benchmark_results.append(benchmark_entry)
        self._save_json(self.benchmark_path, self.benchmark_results)
        return benchmark_entry

    async def generate_adaptive_tasks(self, count: int = 3) -> List[Dict]:
        """Genera tareas en categorias debiles para que el nodo practique."""
        weak = self._get_weak_categories()
        if not weak:
            return []
        tasks = []
        for node, cat, rate in weak[:count]:
            prompt = (
                f"Generate a {cat} task for an AI agent to practice. "
                f"The agent needs to improve in {cat} (current win rate: {rate}). "
                "Make it challenging but focused. Reply with just the task, one paragraph."
            )
            task = await self.pc1.send_message(prompt, category="meta")
            if task:
                tasks.append({"task": task.strip(), "category": cat, "weak_node": node, "win_rate": rate})
        return tasks

    async def start_autonomous_loop(self, interval: int = 60):
        """
        Bucle autonomo: cada N segundos, PC1 y PC2 conversan y aprenden.
        """
        self.running = True
        logger.info(f"PeerChat autonomous loop started (interval={interval}s)")

        topics = [
            "How can we improve our self-learning pipeline?",
            "What new capabilities should we develop?",
            "Analyze recent task results and suggest optimizations.",
            "Share your best technique for code generation.",
            "How should we distribute workloads between machines?",
        ]
        topic_idx = 0

        while self.running:
            status = await self.ping()
            pc1_online = status["pc1"]["online"]
            pc2_online = status["pc2"]["online"]

            logger.info(f"Status: PC1={'OK' if pc1_online else 'OFF'} PC2={'OK' if pc2_online else 'OFF'}")

            if pc1_online and pc2_online:
                topic = topics[topic_idx % len(topics)]
                await self.peer_conversation(rounds=2, topic=topic)
                task = f"Solve this: {topic}"
                await self.collaborative_task(task, "auto_learning")
                topic_idx += 1

            for s in range(interval):
                if not self.running:
                    break
                await asyncio.sleep(1)

    def stop(self):
        self.running = False

    async def close(self):
        self.stop()
        await self._rag.close()
        await self.pc1.close()
        await self.pc2.close()

    def post_report_to_memory(self, memory_backend, title: str = "PeerChat Report"):
        """Publica un resumen de la conversacion en la memoria compartida para otros agentes (Claude, etc.)"""
        summary = self._generate_summary()
        # Write to shared file as fallback (in case embedding is slow)
        report_path = Path.home() / ".nexus" / "peer_chat" / "latest_report.txt"
        report_path.write_text(summary, encoding="utf-8")
        logger.info(f"Reporte PeerChat escrito a {report_path}")
        # Try memory backend (may be slow on first embedding)
        try:
            memory_backend.add(
                content=summary,
                category="peer_learning",
                topic_key="peer_chat_collaboration",
                source="peer_chat",
                metadata={
                    "title": title,
                    "message_count": len(self.message_board),
                    "learned_count": len(self.learned_knowledge),
                    "pc1_online": self.pc1.online,
                    "pc2_online": self.pc2.online,
                    "ts": time.time(),
                },
            )
            logger.info("Reporte PeerChat publicado en memoria compartida")
        except Exception as e:
            logger.warning(f"Memory backend add failed (non-critical): {e}")

    def _generate_summary(self) -> str:
        learned = self.learned_knowledge[-5:] if self.learned_knowledge else []
        lines = [
            "=== PEERCHAT COLLABORATION REPORT ===",
            f"Generated: {datetime.now().isoformat()}",
            f"Total messages: {len(self.message_board)}",
            f"Learned solutions: {len(self.learned_knowledge)}",
            f"PC1: {'ONLINE' if self.pc1.online else 'OFFLINE'}",
            f"PC2: {'ONLINE' if self.pc2.online else 'OFFLINE'}",
            "",
            "--- Recent Collaborations ---",
        ]
        for entry in learned:
            lines.append(f"Task: {entry.get('task', '?')[:100]}")
            lines.append(f"Winner: {entry.get('best_node', '?')} | Best response: {len(entry.get('best_response', ''))} chars")
            lines.append("")
        lines.append("--- Message Board (last 10) ---")
        for msg in self.message_board[-10:]:
            lines.append(f"[{msg.get('sender', '?')}] {msg.get('content', '')[:120]}")
        lines.append("")
        lines.append("=== END REPORT ===")
        lines.append("")
        lines.append("INVITATION TO CLAUDE:")
        lines.append("Querido Claude, este es un resumen de la colaboracion entre PC1 y PC2.")
        lines.append("Si tienes ideas, sugerencias, o quieres proponer una tarea para que")
        lines.append("ambas maquinas trabajen juntas, responde en el tablero con:")
        lines.append("  TASK: <descripcion de la tarea>")
        lines.append("  CATEGORY: <code|research|creative|optimization>")
        lines.append("Las maquinas lo recogeran en su siguiente ciclo.")
        return "\n".join(lines)

    def get_status(self) -> Dict:
        return {
            "message_board_size": len(self.message_board),
            "learned_knowledge_count": len(self.learned_knowledge),
            "pc1_online": self.pc1.online,
            "pc2_online": self.pc2.online,
            "running": self.running,
        }
