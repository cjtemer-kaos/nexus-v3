"""
Session Manager — F1: Auto-Compact Context

Manages conversation sessions with auto-compaction at configurable thresholds.
Used by DirectorNexus, API server, and all agents.
"""

import json
import logging
import sqlite3
import threading
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger("nexus-sessions")


@dataclass
class SessionMessage:
    role: str
    content: str
    timestamp: str = ""
    tokens: int = 0
    model: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now().isoformat()

    def to_dict(self) -> Dict:
        return {
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "tokens": self.tokens,
            "model": self.model,
        }


@dataclass
class Session:
    id: str
    project: str = "default"
    messages: List[SessionMessage] = field(default_factory=list)
    total_tokens: int = 0
    created_at: str = ""
    last_active: str = ""
    parent_session_id: Optional[str] = None
    summary: str = ""
    compact_count: int = 0

    def __post_init__(self):
        now = datetime.now().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.last_active:
            self.last_active = now

    def add_message(self, role: str, content: str, tokens: int = 0, model: str = ""):
        msg = SessionMessage(role=role, content=content, tokens=tokens, model=model)
        self.messages.append(msg)
        self.total_tokens += tokens
        self.last_active = datetime.now().isoformat()

    def get_messages_for_llm(self, max_messages: int = None, scrub: bool = True) -> List[Dict]:
        msgs = self.messages if max_messages is None else self.messages[-max_messages:]
        raw = [{"role": m.role, "content": m.content} for m in msgs]
        if scrub:
            return SequenceScrubber.scrub(raw)
        return raw

    def to_dict(self) -> Dict:
        return {
            "id": self.id,
            "project": self.project,
            "message_count": len(self.messages),
            "total_tokens": self.total_tokens,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "parent_session_id": self.parent_session_id,
            "compact_count": self.compact_count,
        }


class SequenceScrubber:
    """
    Reestructuracion cognitiva de turnos.

    Muchos APIs (especialmente Gemini) requieren alternancia estricta:
    user -> model -> user -> model. Este scrubber:
    1. Agrupa respuestas consecutivas de herramientas en un bloque "user"
    2. Elimina mensajes huerfanos del asistente al inicio
    3. Asegura alternancia user/model antes de enviar a inferencia
    """

    @staticmethod
    def scrub(messages: List[Dict]) -> List[Dict]:
        """
        Depura y alinea los turnos de dialogo.

        Args:
            messages: Lista de dicts con "role" y "content"

        Returns:
            Lista de mensajes con alternancia garantizada
        """
        if not messages:
            return []

        cleaned = []

        # 1. Eliminar assistant/tool huerfanos al inicio (solo si hay mas mensajes despues)
        start_idx = 0
        for i, msg in enumerate(messages):
            role = msg.get("role", "")
            if role in ("user", "system"):
                break
            # Only skip if there are non-orphan messages after
            remaining = messages[i+1:]
            if any(m.get("role", "") in ("user", "system") for m in remaining):
                start_idx = i + 1
            else:
                break  # Keep orphaned message if nothing follows

        # 2. Procesar mensajes con agrupacion de tool responses
        i = start_idx
        while i < len(messages):
            msg = messages[i]
            role = msg.get("role", "")

            if role in ("tool", "tool_result"):
                # Agrupar tool responses consecutivos en un bloque user
                tool_parts = []
                while i < len(messages) and messages[i].get("role", "") in ("tool", "tool_result"):
                    tool_parts.append(messages[i].get("content", ""))
                    i += 1
                if tool_parts:
                    combined = "\n\n".join(tool_parts)
                    cleaned.append({"role": "user", "content": f"[Tool Results]\n{combined}"})
                continue

            if role == "system":
                # System messages se mantienen al inicio
                if not cleaned or cleaned[-1].get("role") == "system":
                    cleaned.append(msg)
                else:
                    # Insertar system como user con prefijo
                    cleaned.append({"role": "user", "content": f"[System]\n{msg.get('content', '')}"})
                i += 1
                continue

            if role == "assistant":
                # Evitar dos assistant consecutivos
                if cleaned and cleaned[-1].get("role") == "assistant":
                    # Fusionar con el anterior
                    cleaned[-1]["content"] += "\n" + msg.get("content", "")
                else:
                    cleaned.append(msg)
                i += 1
                continue

            # user messages
            cleaned.append(msg)
            i += 1

        # 3. Verificar alternancia final
        return SequenceScrubber._enforce_alternation(cleaned)

    @staticmethod
    def _enforce_alternation(messages: List[Dict]) -> List[Dict]:
        """Asegura alternancia estricta user/model"""
        if not messages:
            return []

        result = [messages[0]]

        for msg in messages[1:]:
            last_role = result[-1].get("role", "")
            current_role = msg.get("role", "")

            if current_role == last_role:
                if current_role == "assistant":
                    # Fusionar assistant consecutivos
                    result[-1]["content"] += "\n" + msg.get("content", "")
                elif current_role == "user":
                    # Fusionar user consecutivos
                    result[-1]["content"] += "\n" + msg.get("content", "")
                else:
                    result.append(msg)
            else:
                result.append(msg)

        # Asegurar que el ultimo mensaje sea user (para que el modelo responda)
        # Solo si hay mas de 1 mensaje y termina en assistant
        if len(result) > 1 and result[-1].get("role") == "assistant":
            result = result[:-1]

        return result


class SessionManager:
    """Gestiona sesiones con auto-compact"""

    def __init__(self, db_path: Optional[str] = None, max_tokens: int = 32000, compact_threshold: float = 0.95):
        self.max_tokens = max_tokens
        self.compact_threshold = compact_threshold
        self._sessions: Dict[str, Session] = {}
        self._active_session_id: Optional[str] = None
        self._lock = threading.RLock()  # RLock para evitar deadlock en llamadas anidadas
        self._db_lock = threading.Lock()
        self.compressor = None  # TrajectoryCompressor (set externally or lazily)
        self._checkpoint_store = None  # Auto-trigger checkpoint store

        if db_path is None:
            db_path = str(Path.home() / ".nexus" / "brain" / "sessions.db")
        self.db_path = db_path
        self._in_memory = db_path == ":memory:"
        if not self._in_memory:
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        # For :memory: keep a single shared connection (separate connects create blank DBs)
        self._mem_conn: Optional[sqlite3.Connection] = None
        self._init_db()

    def _get_compressor(self):
        """Lazy init del TrajectoryCompressor"""
        if self.compressor is None:
            try:
                from src.core.trajectory_compressor import TrajectoryCompressor
                self.compressor = TrajectoryCompressor()
            except Exception:
                pass
        return self.compressor

    def _get_conn(self) -> sqlite3.Connection:
        """Return a DB connection. Uses shared conn for in-memory DBs."""
        if self._in_memory:
            with self._db_lock:
                if self._mem_conn is None:
                    self._mem_conn = sqlite3.connect(self.db_path, check_same_thread=False)
            return self._mem_conn
        return sqlite3.connect(self.db_path, timeout=30)

    def _release_conn(self, conn: sqlite3.Connection):
        """Close conn only if it's a real file-based connection."""
        if not self._in_memory:
            conn.close()

    def _init_db(self):
        conn = self._get_conn()
        c = conn.cursor()
        if not self._in_memory:
            c.execute("PRAGMA journal_mode=WAL")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            project TEXT DEFAULT 'default',
            total_tokens INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            last_active TEXT NOT NULL,
            parent_session_id TEXT,
            summary TEXT DEFAULT '',
            compact_count INTEGER DEFAULT 0,
            messages_json TEXT DEFAULT '[]'
        )""")
        conn.commit()
        self._release_conn(conn)


    def _save_session(self, session: Session):
        with self._lock:
            conn = self._get_conn()
            c = conn.cursor()
            if not self._in_memory:
                c.execute("BEGIN IMMEDIATE")
            try:
                messages_json = json.dumps([m.to_dict() for m in session.messages], ensure_ascii=False)
                c.execute("""INSERT OR REPLACE INTO sessions 
                    (id, project, total_tokens, created_at, last_active, parent_session_id, summary, compact_count, messages_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
                    session.id, session.project, session.total_tokens,
                    session.created_at, session.last_active, session.parent_session_id,
                    session.summary, session.compact_count, messages_json
                ))
                conn.commit()
            finally:
                self._release_conn(conn)


    def _load_session(self, session_id: str) -> Optional[Session]:
        with self._lock:
            conn = self._get_conn()
            conn.row_factory = sqlite3.Row
            c = conn.cursor()
            c.execute("SELECT * FROM sessions WHERE id = ?", (session_id,))
            row = c.fetchone()
            self._release_conn(conn)
            if not row:
                return None
            try:
                messages = [SessionMessage(**m) for m in json.loads(row[8] or "[]")]
            except (json.JSONDecodeError, TypeError):
                messages = []
            return Session(
                id=row[0], project=row[1], messages=messages,
                total_tokens=row[2], created_at=row[3], last_active=row[4],
                parent_session_id=row[5], summary=row[6], compact_count=row[7]
            )


    def create_session(self, session_id: str = None, project: str = "default", parent_id: str = None) -> Session:
        import uuid
        sid = session_id or str(uuid.uuid4())[:8]
        session = Session(id=sid, project=project, parent_session_id=parent_id)
        self._sessions[sid] = session
        self._active_session_id = sid
        self._save_session(session)
        logger.info(f"Session created: {sid} (project: {project})")
        return session

    def get_session(self, session_id: str = None) -> Session:
        sid = session_id or self._active_session_id
        if sid and sid in self._sessions:
            return self._sessions[sid]
        if sid:
            session = self._load_session(sid)
            if session:
                self._sessions[sid] = session
                self._active_session_id = sid
                return session
        return self.create_session()

    def set_checkpoint_store(self, store):
        """Connect a CheckpointStore for auto-trigger on context pressure"""
        self._checkpoint_store = store

    def add_message(self, role: str, content: str, tokens: int = 0, model: str = "", session_id: str = None):
        session = self.get_session(session_id)
        session.add_message(role, content, tokens, model)
        self._save_session(session)

        # Auto-trigger checkpoint at 20%/45%/70% context pressure
        if self._checkpoint_store is not None:
            try:
                result = self._checkpoint_store.check_and_auto_checkpoint(
                    self, session_id=session_id, project=session.project
                )
                if result:
                    import logging
                    logging.getLogger("nexus-sessions").info(
                        f"Auto-checkpoint: {result['checkpoint_id']} @ {result['threshold']:.0%}"
                    )
            except Exception as e:
                import logging
                logging.getLogger("nexus-sessions").warning(f"Auto-checkpoint failed: {e}")

    def needs_compact(self, session_id: str = None) -> bool:
        session = self.get_session(session_id)
        return session.total_tokens > (self.max_tokens * self.compact_threshold)

    def compact_session(self, session_id: str = None, summary: str = "") -> Dict:
        """Compacta sesión: guarda resumen y crea nueva sesión hija"""
        session = self.get_session(session_id)

        if not summary:
            summary = f"[Resumen de {len(session.messages)} mensajes, {session.total_tokens} tokens]"

        old_id = session.id
        old_count = len(session.messages)
        old_tokens = session.total_tokens

        # Crear nueva sesión hija con resumen
        new_session = self.create_session(
            project=session.project,
            parent_id=old_id
        )
        new_session.summary = summary
        new_session.compact_count = session.compact_count + 1
        new_session.add_message("system", f"[RESUMEN DE SESIÓN ANTERIOR]\n{summary}")

        # Actualizar sesión padre
        session.compact_count += 1
        session.summary = summary
        self._save_session(session)
        self._save_session(new_session)

        logger.info(f"Session compacted: {old_id} → {new_session.id} ({old_tokens} tokens → summary)")

        return {
            "old_session": old_id,
            "new_session": new_session.id,
            "messages_before": old_count,
            "messages_after": len(new_session.messages),
            "tokens_before": old_tokens,
            "tokens_after": new_session.total_tokens,
            "summary": summary,
        }

    def compact_session_trajectory(self, session_id: str = None, summary_text: str = "", protect_last_n: int = 4) -> Dict:
        """
        F1: Context Trajectory Compression
        Protege el inicio (System/Primer User) y el final (ultimos N mensajes), 
        y condensa el historial medio reemplazándolo con una sola tarjeta de resumen.
        """
        session = self.get_session(session_id)
        
        n_messages = len(session.messages)
        if n_messages <= protect_last_n + 2:
            # Muy pocos mensajes para comprimir de forma segura
            return {
                "session_id": session.id,
                "status": "skipped_too_short",
                "messages_count": n_messages,
            }
            
        old_count = n_messages
        old_tokens = session.total_tokens
        
        # 1. Identificar partes protegidas
        # Head (System y el primer mensaje de User/Human)
        head_indices = []
        has_system = False
        has_user = False
        for idx, msg in enumerate(session.messages):
            if idx >= n_messages - protect_last_n:
                break
            if msg.role == "system" and not has_system:
                head_indices.append(idx)
                has_system = True
            elif msg.role == "user" and not has_user:
                head_indices.append(idx)
                has_user = True
                
        if not head_indices:
            # Si no se detecta system/user en la primera parte, proteger por defecto el primer mensaje
            head_indices = [0]
            
        max_head_idx = max(head_indices)
        
        # Tail (últimos N mensajes)
        tail_start_idx = n_messages - protect_last_n
        
        # 2. Extraer región comprimible
        middle_messages = session.messages[max_head_idx + 1:tail_start_idx]
        
        if not middle_messages:
            return {
                "session_id": session.id,
                "status": "skipped_no_middle",
                "messages_count": n_messages,
            }
            
        # 3. Generar resumen
        if not summary_text:
            # Fallback a resumen descriptivo de herramientas y respuestas
            tool_calls = sum(1 for m in middle_messages if "tool" in m.role or "execute" in m.content.lower())
            summary_text = (
                f"[RESUMEN DE CONTEXTO INTERMEDIO]: Se comprimieron {len(middle_messages)} mensajes "
                f"medios para liberar espacio de tokens. Durante esta fase se interactuó en "
                f"{tool_calls} ocasiones con herramientas de sistema y subprocesos. "
                f"El estado final de las variables y del workspace se conserva intacto."
            )
            
        # 4. Construir lista compacta
        head_messages = [session.messages[i] for i in head_indices]
        summary_msg = SessionMessage(
            role="system",
            content=summary_text,
            tokens=len(summary_text) // 4,
            model="trajectory_compressor"
        )
        tail_messages = session.messages[tail_start_idx:]
        
        new_messages = head_messages + [summary_msg] + tail_messages
        
        # 5. Actualizar sesión
        session.messages = new_messages
        session.compact_count += 1
        session.summary = summary_text
        
        # Re-estimar tokens totales
        session.total_tokens = sum(m.tokens if m.tokens > 0 else (len(m.content) // 4) for m in session.messages)
        
        self._save_session(session)
        
        logger.info(f"Session trajectory compressed: {session.id} ({old_tokens} -> {session.total_tokens} tokens)")
        
        return {
            "session_id": session.id,
            "status": "success",
            "messages_before": old_count,
            "messages_after": len(session.messages),
            "tokens_before": old_tokens,
            "tokens_after": session.total_tokens,
            "summary": summary_text,
        }

    async def compact_session_trajectory_async(self, session_id: str = None, protect_last_n: int = 4) -> Dict:
        """
        F1: Context Trajectory Compression con LLM
        Usa TrajectoryCompressor con Ollama local para generar resumenes inteligentes.
        Fallback a version sync si el compressor no esta disponible.
        """
        compressor = self._get_compressor()
        if not compressor:
            # Fallback a version sync sin LLM
            return self.compact_session_trajectory(session_id, protect_last_n=protect_last_n)

        session = self.get_session(session_id)
        n_messages = len(session.messages)
        if n_messages <= protect_last_n + 2:
            return {
                "session_id": session.id,
                "status": "skipped_too_short",
                "messages_count": n_messages,
            }

        old_count = n_messages
        old_tokens = session.total_tokens

        # Usar TrajectoryCompressor
        result = await compressor.compress(session.messages)
        
        if result["status"] in ("skipped_too_short", "skipped_no_middle"):
            return result

        new_messages = result["messages"]
        summary_text = result.get("summary", "")
        metrics = result.get("metrics")

        # Actualizar sesion
        session.messages = new_messages
        session.compact_count += 1
        session.summary = summary_text
        session.total_tokens = sum(
            m.tokens if m.tokens > 0 else (len(getattr(m, "content", m.get("content", ""))) // 4)
            for m in session.messages
        )

        self._save_session(session)

        logger.info(
            f"Session trajectory compressed (LLM): {session.id} "
            f"({old_tokens} -> {session.total_tokens} tokens, "
            f"ratio: {metrics.compression_ratio:.2f})"
        )

        return {
            "session_id": session.id,
            "status": "success",
            "messages_before": old_count,
            "messages_after": len(session.messages),
            "tokens_before": old_tokens,
            "tokens_after": session.total_tokens,
            "tokens_saved": metrics.tokens_saved if metrics else 0,
            "compression_ratio": metrics.compression_ratio if metrics else 1.0,
            "summary": summary_text,
        }


    def get_context_pressure(self, session_id: str = None) -> Dict:
        """F10: Context Pressure Monitoring"""
        session = self.get_session(session_id)
        usage_pct = (session.total_tokens / self.max_tokens) * 100

        if usage_pct >= 95:
            level = "critical"
        elif usage_pct >= 80:
            level = "high"
        elif usage_pct >= 60:
            level = "medium"
        else:
            level = "low"

        return {
            "session_id": session.id,
            "total_tokens": session.total_tokens,
            "max_tokens": self.max_tokens,
            "usage_percent": round(usage_pct, 1),
            "level": level,
            "messages": len(session.messages),
            "needs_compact": self.needs_compact(session.id),
            "compact_count": session.compact_count,
        }

    def get_stats(self) -> Dict:
        return {
            "active_sessions": len(self._sessions),
            "active_session_id": self._active_session_id,
            "max_tokens": self.max_tokens,
            "compact_threshold": self.compact_threshold,
        }

    def list_sessions(self, project: str = None) -> List[Dict]:
        sessions = self._sessions.values()
        if project:
            sessions = [s for s in sessions if s.project == project]
        return [s.to_dict() for s in sorted(sessions, key=lambda s: s.last_active, reverse=True)]

    def close(self):
        if self._mem_conn:
            self._mem_conn.close()
            self._mem_conn = None
