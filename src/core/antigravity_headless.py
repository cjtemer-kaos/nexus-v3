"""
Antigravity Headless — Control de Antigravity desde NEXUS via Message Board.
Permite a NEXUS enviar tareas al agente Antigravity (Gemini) y esperar su respuesta.
Usa la sesión activa de la cuenta Google del usuario (NO necesita API key).
"""

import asyncio
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Optional

class AntigravityResult:
    def __init__(self, success: bool, output: str = "", error: str = ""):
        self.success = success
        self.output = output
        self.error = error

class AntigravityHeadless:
    """
    Control programático de Antigravity desde NEXUS.
    Envía tareas al buzón compartido y espera la respuesta del agente.
    """
    def __init__(self, db_path: Optional[str] = None, timeout: int = 300):
        self.db_path = db_path or str(Path.home() / ".nexus" / "brain" / "message_board.db")
        self.timeout = timeout

    def _get_conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def run(self, prompt: str, sender: str = "supernexus") -> AntigravityResult:
        """
        Envía un prompt a Antigravity y espera la respuesta en la base de datos.
        """
        conn = self._get_conn()
        timestamp = datetime.now().isoformat()
        try:
            # Enviar el mensaje/tarea
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO messages (timestamp, sender, target, channel, content, msg_type) VALUES (?, ?, ?, ?, ?, ?)",
                (timestamp, sender, "antigravity", "tasks", prompt, "task")
            )
            conn.commit()
            task_id = cursor.lastrowid or 0
        except Exception as e:
            conn.close()
            return AntigravityResult(success=False, error=f"Error al enviar tarea: {e}")
        finally:
            conn.close()

        # Esperar la respuesta (polling asíncrono)
        start_time = asyncio.get_event_loop().time()
        while (asyncio.get_event_loop().time() - start_time) < self.timeout:
            await asyncio.sleep(2)
            conn = self._get_conn()
            try:
                # Buscar respuesta de antigravity para esta tarea
                rows = conn.execute(
                    "SELECT content, metadata FROM messages WHERE sender='antigravity' AND target=? AND msg_type='task_done' ORDER BY id DESC",
                    (sender,)
                ).fetchall()
                
                for r in rows:
                    meta = json.loads(r["metadata"] or "{}")
                    if meta.get("task_id") == task_id or (r["content"] and f"task_id: {task_id}" in r["content"]):
                        conn.close()
                        return AntigravityResult(success=True, output=r["content"])
                    
                # Si no se encuentra con ID exacto, buscar respuestas de antigravity después del timestamp
                rows_after = conn.execute(
                    "SELECT content, timestamp FROM messages WHERE sender='antigravity' AND target=? AND timestamp > ? ORDER BY id DESC",
                    (sender, timestamp)
                ).fetchall()
                if rows_after:
                    conn.close()
                    return AntigravityResult(success=True, output=rows_after[0]["content"])
            except Exception:
                pass
            finally:
                conn.close()

        return AntigravityResult(success=False, error=f"Timeout esperando respuesta de Antigravity tras {self.timeout} segundos")
