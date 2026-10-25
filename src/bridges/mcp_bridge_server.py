"""
SuperNEXUS - Cerebro Unificado MCP

Hub central de comunicacion en tiempo real entre aplicaciones:
Claude Desktop, Claude Code, Gemini, agentes, remote_node, etc.

Capacidades:
- Mensajeria en tiempo real entre apps (tablero compartido)
- Memoria compartida (Cerebro adaptativo)
- Control de nodos remotos (remote_node, etc.)

Fixes aplicados (2026-05-17):
- WAL mode + busy_timeout para concurrencia (Claude + Antigravity)
- Indices para performance (174K+ mensajes)
- SQL injection fix (whitelist en lugar de f-strings)
- httpx moved to top-level import
- import re removed from loop
- BRAIN_DIR Path mixing fixed
- _*_impl functions marked DEPRECATED (eliminar cuando server.py migre)
- Gestion de tareas entre agentes
- Estado del sistema completo
"""

import asyncio
import sys
import hashlib
import json
import os
import logging
import platform
import sqlite3
import re
import shutil
from datetime import datetime
from pathlib import Path

# Add project root to sys.path for imports
_project_root = str(Path(__file__).resolve().parents[2])
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Optimizacion: imports a top-level
try:
    import httpx
    HTTPX_AVAILABLE = True
except ImportError:
    HTTPX_AVAILABLE = False

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("nexus-bridge")

# Cargar .env si existe para robustez en control de nodos
for p in [Path(__file__).resolve().parents[2] / ".env", Path.cwd() / ".env"]:
    if p.exists():
        try:
            with open(p, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#") and "=" in line:
                        k, v = line.split("=", 1)
                        os.environ[k.strip()] = v.strip()
        except Exception as e:
            logger.error(f"Error cargando .env desde {p}: {e}")

REMOTE_NODE_IP = os.environ.get("SUPER_NEXUS_REMOTE_NODE_IP", "")
import src.core.nexus_config as nexus_config
from src.core.environment import data_home, ollama_base_url
from src.brain.link import brain_path as _brain_path

NEXUS_HOME = os.environ.get("NEXUS_HOME", str(data_home()))
# Cerebro: NEXUS_BRAIN (enlace externo opcional) > <proyecto>/brain
BRAIN_DIR = _brain_path()
BRAIN_DIR.mkdir(parents=True, exist_ok=True)

# Connection pool for MCP servers
try:
    from src.core.mcp_connection_manager import MCPConnectionPool
    _mcp_pool = MCPConnectionPool()
except ImportError:
    _mcp_pool = None

# ============================================================
# Shared state: message board + brain DB
# ============================================================
_BOARD_DB = BRAIN_DIR / "message_board.db"
_BRAIN_DB = BRAIN_DIR / "cerebro.db"
# Tareas Antigravity: integracion externa adaptativa — si Antigravity no
# esta instalado en esta PC, los tasks caen dentro del proyecto.
_TASK_DEFAULT = Path.home() / ".gemini" / "antigravity" / "scratch" / "Tasks"
if os.environ.get("NEXUS_TASK_DIR"):
    _TASK_DIR = Path(os.environ["NEXUS_TASK_DIR"]).expanduser()
elif _TASK_DEFAULT.parent.exists():
    _TASK_DIR = _TASK_DEFAULT
else:
    _TASK_DIR = data_home() / "antigravity_tasks"
_TASK_DIR.mkdir(parents=True, exist_ok=True)


def _get_board_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_BOARD_DB), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _get_brain_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_BRAIN_DB), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_board_db():
    """Inicializa la base de datos del tablero de mensajes compartido."""
    conn = sqlite3.connect(str(_BOARD_DB), timeout=30)
    c = conn.cursor()
    # Fix critico: WAL mode para concurrencia (identificado por Claude + Antigravity)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=5000")
    c.execute("""CREATE TABLE IF NOT EXISTS messages (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        sender TEXT NOT NULL,
        target TEXT DEFAULT '*',
        channel TEXT DEFAULT 'general',
        content TEXT NOT NULL,
        msg_type TEXT DEFAULT 'chat',
        metadata TEXT DEFAULT '{}'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS shared_memory (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TEXT NOT NULL
    )""")
    # Indices para performance (174K+ mensajes)
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_target ON messages(target, channel, timestamp)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_messages_type ON messages(msg_type, timestamp)")
    conn.commit()
    conn.close()

_init_board_db()


# ============================================================
# FastMCP Server
# ============================================================
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("nexus-bridge")


# ---- MENSAJERIA EN TIEMPO REAL ----

@mcp.tool()
async def send_message(
    content: str,
    sender: str = "claude-code",
    target: str = "*",
    channel: str = "general",
    msg_type: str = "chat",
) -> str:
    """Send a message to the shared board. All connected apps (Claude Desktop, Claude Code, Gemini, agents) can read it. Use target='*' to broadcast, or a specific app name."""
    conn = _get_board_conn()
    c = conn.cursor()
    ts = datetime.now().isoformat()
    c.execute(
        "INSERT INTO messages (timestamp, sender, target, channel, content, msg_type) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, sender, target, channel, content, msg_type),
    )
    msg_id = c.lastrowid
    conn.commit()
    conn.close()
    return json.dumps({"sent": True, "id": msg_id, "timestamp": ts}, indent=2)


@mcp.tool()
async def read_messages(
    channel: str = "general",
    limit: int = 20,
    since: str = "",
    sender: str = "",
) -> str:
    """Read messages from the shared board. Filter by channel, sender, or time. This is how apps communicate in real-time."""
    conn = _get_board_conn()
    c = conn.cursor()
    query = "SELECT id, timestamp, sender, target, channel, content, msg_type FROM messages WHERE channel = ?"
    params = [channel]
    if since:
        query += " AND timestamp > ?"
        params.append(since)
    if sender:
        query += " AND sender = ?"
        params.append(sender)
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    messages = [
        {"id": r[0], "timestamp": r[1], "sender": r[2], "target": r[3],
         "channel": r[4], "content": r[5], "type": r[6]}
        for r in reversed(rows)
    ]
    return json.dumps({"messages": messages, "count": len(messages)}, indent=2, ensure_ascii=False)


# ---- MEMORIA COMPARTIDA (key-value rapido) ----

@mcp.tool()
async def memory_set(key: str, value: str, updated_by: str = "claude-code") -> str:
    """Set a shared memory value. Any app can read/write. Use for sharing state between Claude Desktop, Claude Code, agents, etc."""
    conn = _get_board_conn()
    c = conn.cursor()
    ts = datetime.now().isoformat()
    c.execute(
        "INSERT OR REPLACE INTO shared_memory (key, value, updated_by, updated_at) VALUES (?, ?, ?, ?)",
        (key, value, updated_by, ts),
    )
    conn.commit()
    conn.close()
    return json.dumps({"key": key, "set": True, "by": updated_by}, indent=2)


@mcp.tool()
async def memory_get(key: str = "", prefix: str = "") -> str:
    """Get shared memory value(s). Pass key for exact match, or prefix to list all keys starting with it. Omit both to list all."""
    conn = _get_board_conn()
    c = conn.cursor()
    if key:
        c.execute("SELECT key, value, updated_by, updated_at FROM shared_memory WHERE key = ?", (key,))
    elif prefix:
        c.execute("SELECT key, value, updated_by, updated_at FROM shared_memory WHERE key LIKE ?", (f"{prefix}%",))
    else:
        c.execute("SELECT key, value, updated_by, updated_at FROM shared_memory")
    rows = c.fetchall()
    conn.close()
    data = {r[0]: {"value": r[1], "by": r[2], "at": r[3]} for r in rows}
    return json.dumps(data, indent=2, ensure_ascii=False)


# ---- FASE 3.2: BACKGROUND THINKING (DMN-inspired) ----
# Inspired by Lethe: processes observations between user turns to
# consolidate knowledge, detect patterns, and generate insights.

@mcp.tool()
async def background_think(hours_back: int = 24, max_observations: int = 50) -> str:
    """Process recent observations to consolidate knowledge (DMN-inspired background thinking).

    Inspired by Lethe's background processing: analyzes recent observations,
    groups them by topic family, extracts patterns, and stores consolidated
    insights back to the brain. Call this between user turns to keep knowledge fresh.

    Returns: consolidated insights, patterns detected, knowledge items updated.
    """
    conn = _get_memory_conn()
    c = conn.cursor()

    # 1. Fetch recent observations
    from datetime import timedelta
    cutoff = (datetime.now() - timedelta(hours=hours_back)).isoformat()
    c.execute(
        "SELECT id, content, category, project, topic_key, metadata FROM observations "
        "WHERE ts >= ? AND deleted_at IS NULL ORDER BY ts DESC LIMIT ?",
        (cutoff, max_observations),
    )
    observations = c.fetchall()

    if not observations:
        conn.close()
        return json.dumps({"status": "no_recent_observations", "hours_back": hours_back}, indent=2)

    # 2. Group by topic family
    families = {}
    for obs in observations:
        obs_id, content, category, project, topic_key, metadata = obs
        family = _get_topic_family(topic_key) if topic_key else "general"
        if family not in families:
            families[family] = []
        families[family].append({
            "id": obs_id, "content": content[:300],
            "category": category, "topic_key": topic_key,
        })

    # 3. Detect patterns: topics with 3+ observations
    patterns = []
    for family, obs_list in families.items():
        if len(obs_list) >= 3:
            # Extract common entities
            all_entities = set()
            for obs in obs_list:
                try:
                    ents = _extract_entities(obs["content"])
                    for e in ents:
                        all_entities.add(e["text"])
                except Exception:
                    pass
            patterns.append({
                "family": family,
                "observation_count": len(obs_list),
                "common_entities": list(all_entities)[:10],
                "sample_topics": [o["topic_key"] for o in obs_list if o["topic_key"]][:5],
            })

    # 4. Store consolidated insights to brain
    brain_conn = _get_brain_conn()
    bc = brain_conn.cursor()
    bc.execute("""CREATE TABLE IF NOT EXISTS conocimientos (
        id INTEGER PRIMARY KEY, tema TEXT, contenido TEXT,
        fuente TEXT, fecha TEXT, veces_revisado INTEGER DEFAULT 0,
        utilidad INTEGER DEFAULT 5, consolidado BOOLEAN DEFAULT 0)""")

    insights_stored = 0
    for pattern in patterns:
        tema = f"auto:{pattern['family']}:pattern"
        contenido = (
            f"Patrón detectado en familia '{pattern['family']}': "
            f"{pattern['observation_count']} observaciones recientes. "
            f"Entidades comunes: {', '.join(pattern['common_entities'][:5])}. "
            f"Topics: {', '.join(pattern['sample_topics'][:3])}"
        )
        # Upsert
        bc.execute("SELECT id FROM conocimientos WHERE tema = ?", (tema,))
        if bc.fetchone():
            bc.execute(
                "UPDATE conocimientos SET contenido=?, fecha=?, veces_revisado=veces_revisado+1 WHERE tema=?",
                (contenido, datetime.now().isoformat(), tema),
            )
        else:
            bc.execute(
                "INSERT INTO conocimientos (tema, contenido, fuente, fecha, utilidad, consolidado) VALUES (?, ?, ?, ?, ?, 1)",
                (tema, contenido, "background_think", datetime.now().isoformat(), min(8, 5 + len(patterns))),
            )
            insights_stored += 1

    brain_conn.commit()
    brain_conn.close()
    conn.close()

    return json.dumps({
        "status": "completed",
        "observations_processed": len(observations),
        "families_detected": len(families),
        "patterns_found": len(patterns),
        "insights_stored": insights_stored,
        "top_patterns": patterns[:5],
        "family_distribution": {f: len(o) for f, o in families.items()},
    }, indent=2, ensure_ascii=False)


# ---- CEREBRO (memoria adaptativa de largo plazo) ----

@mcp.tool()
async def brain_remember(
    topic: str,
    content: str,
    source: str = "claude-code",
    importance: int = 5,
) -> str:
    """Store knowledge in the shared brain (long-term memory). The brain learns from all apps - patterns, preferences, facts."""
    conn = _get_brain_conn()
    c = conn.cursor()
    # Ensure tables exist
    c.execute("""CREATE TABLE IF NOT EXISTS conocimientos (
        id INTEGER PRIMARY KEY, tema TEXT, contenido TEXT,
        fuente TEXT, fecha TEXT, veces_revisado INTEGER DEFAULT 0,
        utilidad INTEGER DEFAULT 5, consolidado BOOLEAN DEFAULT 0)""")
    ts = datetime.now().isoformat()
    c.execute("SELECT id FROM conocimientos WHERE tema = ?", (topic,))
    if c.fetchone():
        c.execute(
            "UPDATE conocimientos SET contenido = ?, fuente = ?, fecha = ?, utilidad = ?, veces_revisado = veces_revisado + 1 WHERE tema = ?",
            (content, source, ts, importance, topic),
        )
        action = "updated"
    else:
        c.execute(
            "INSERT INTO conocimientos (tema, contenido, fuente, fecha, utilidad) VALUES (?, ?, ?, ?, ?)",
            (topic, content, source, ts, importance),
        )
        action = "created"
    conn.commit()
    conn.close()
    return json.dumps({"topic": topic, "action": action, "source": source}, indent=2)


@mcp.tool()
async def brain_recall(query: str = "", topic: str = "", limit: int = 10,
                       budget: str = "medium", max_tokens: int = 0) -> str:
    """Recall knowledge from the shared brain. Search by topic or free-text query across all stored knowledge.

    ``budget`` (low|medium|high) o ``max_tokens`` limitan el payload devuelto:
    el agente no se ahoga con todo el cerebro en cada recall.
    """
    limit = max(1, min(50, int(limit or 10)))
    budgets = {"low": 800, "small": 800, "medium": 2000, "high": 5000}
    cap = int(max_tokens or 0) or budgets.get(str(budget or "medium").lower(), 2000)
    cap = max(128, min(20000, cap))
    conn = _get_brain_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS conocimientos (
        id INTEGER PRIMARY KEY, tema TEXT, contenido TEXT,
        fuente TEXT, fecha TEXT, veces_revisado INTEGER DEFAULT 0,
        utilidad INTEGER DEFAULT 5, consolidado BOOLEAN DEFAULT 0)""")
    if topic:
        c.execute("SELECT tema, contenido, fuente, fecha, utilidad FROM conocimientos WHERE tema LIKE ? ORDER BY utilidad DESC LIMIT ?",
                  (f"%{topic}%", limit))
    elif query:
        c.execute("SELECT tema, contenido, fuente, fecha, utilidad FROM conocimientos WHERE tema LIKE ? OR contenido LIKE ? ORDER BY utilidad DESC LIMIT ?",
                  (f"%{query}%", f"%{query}%", limit))
    else:
        c.execute("SELECT tema, contenido, fuente, fecha, utilidad FROM conocimientos ORDER BY utilidad DESC, fecha DESC LIMIT ?", (limit,))
    rows = c.fetchall()
    conn.close()

    max_chars = cap * 4
    used = 0
    truncated = False
    knowledge = []
    for r in rows:
        content = str(r[1] or "")
        remaining = max_chars - used
        if remaining <= 0:
            truncated = True
            break
        if len(content) > remaining:
            content = content[: max(0, remaining - 1)] + "…"
            truncated = True
        knowledge.append({
            "topic": r[0], "content": content, "source": r[2],
            "date": r[3], "importance": r[4],
        })
        used += len(content)
        if truncated:
            break
    return json.dumps({
        "knowledge": knowledge,
        "count": len(knowledge),
        "budget": {"max_tokens": cap, "used_tokens": used // 4,
                   "truncated": truncated},
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def brain_stats() -> str:
    """Get unified brain statistics - memory usage, conversations, patterns, all connected state."""
    stats = {"brain_dir": str(BRAIN_DIR)}

    # Board stats
    try:
        conn = _get_board_conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM messages")
        stats["total_messages"] = c.fetchone()[0]
        c.execute("SELECT DISTINCT sender FROM messages")
        stats["active_senders"] = [r[0] for r in c.fetchall()]
        c.execute("SELECT DISTINCT channel FROM messages")
        stats["channels"] = [r[0] for r in c.fetchall()]
        c.execute("SELECT COUNT(*) FROM shared_memory")
        stats["shared_memory_keys"] = c.fetchone()[0]
        conn.close()
    except Exception:
        stats["board"] = "not initialized"

    # Brain stats
    try:
        conn = sqlite3.connect(str(_BRAIN_DB), timeout=30)
        c = conn.cursor()
        c.execute("PRAGMA journal_mode=WAL")
        # Fix: whitelist en lugar de f-strings (SQL injection risk)
        VALID_TABLES = ["conocimientos", "conversaciones", "patrones", "memoria", "eventos"]
        for table in VALID_TABLES:
            try:
                c.execute("SELECT COUNT(*) FROM " + table)
                stats[table] = c.fetchone()[0]
            except Exception:
                stats[table] = 0
        conn.close()
    except Exception:
        stats["cerebro"] = "not initialized"

    return json.dumps(stats, indent=2, ensure_ascii=False)


def get_remote_node_ip() -> str:
    """Obtiene la IP del nodo remoto cargando el .env dinámicamente si es necesario."""
    for p in [Path(__file__).resolve().parents[2] / ".env", Path.cwd() / ".env"]:
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, v = line.split("=", 1)
                            os.environ[k.strip()] = v.strip()
            except Exception:
                pass
    return os.environ.get("SUPER_NEXUS_REMOTE_NODE_IP", "")


# ---- CONTROL DE NODOS ----

@mcp.tool()
async def execute_on_remote_node(command: str) -> str:
    """Execute a bash command on a remote node via HTTP. Use for server management, GPU tasks, and remote operations."""
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    remote_ip = get_remote_node_ip()
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(
                f"http://{remote_ip}:9000/api/chat",
                json={"message": f"Ejecuta este comando en la terminal y devuelve solo el resultado: {command}"},
            )
            if r.status_code == 200:
                data = r.json()
                return json.dumps({
                    "command": command, "success": data.get("success", False),
                    "output": data.get("reply", ""), "model": data.get("model", ""),
                }, indent=2, ensure_ascii=False)
            return json.dumps({"error": f"HTTP {r.status_code}"}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


# REMOVED for distro - async def execute_on_pc2(command: str) -> str:

# STUB 2026-08-30: execute_on_pc2 removed (no PC2 on VPS), stub for import compat
async def execute_on_pc2(command: str) -> str:
    """PC2 not available on VPS. Returns error."""
    return '{"error": "PC2 not available on VPS"}'


@mcp.tool()
async def list_nodes() -> str:
    """List all nodes in the NexusHive network with their status."""
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    remote_ip = get_remote_node_ip()
    nodes = {
        "pc1": {"name": "PC1 - Windows Main", "ip": "localhost", "status": "online",
                "capabilities": ["claude-code", "claude-desktop", "supernexus", "ollama"]},
        "remote_node": {"name": "remote_node - Linux GPU", "ip": remote_ip, "status": "unknown",
                "capabilities": ["gpu", "ollama", "ssh", "comfyui"]},
    }
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"http://{remote_ip}:9000/api/status")
            nodes["remote_node"]["status"] = "online" if r.status_code == 200 else "degraded"
    except Exception:
        nodes["remote_node"]["status"] = "offline"
    return json.dumps(nodes, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_system_info(node_id: str = "pc1", info_type: str = "all") -> str:
    """Get system information from any node. info_type: gpu, cpu, memory, disk, ollama, all."""
    if node_id == "pc1":
        import platform
        info = {"node": "pc1", "os": platform.system(), "release": platform.release(),
                "machine": platform.machine(), "processor": platform.processor()}
        return json.dumps(info, indent=2, ensure_ascii=False)

    if node_id == "remote_node":
        remote_ip = get_remote_node_ip()
        cmd_map = {
            "gpu": "nvidia-smi --query-gpu=name,memory.used,memory.total,utilization.gpu --format=csv,noheader",
            "cpu": "lscpu | head -20", "memory": "free -h", "disk": "df -h /",
            "ollama": "curl -s http://localhost:11434/api/tags",
            "all": "echo '=== GPU ===' && nvidia-smi --query-gpu=name,memory.used,memory.total --format=csv,noheader 2>/dev/null || echo 'No GPU' && echo '=== MEM ===' && free -h && echo '=== DISK ===' && df -h /",
        }
        cmd = cmd_map.get(info_type, cmd_map["all"])
        try:
            async with httpx.AsyncClient(timeout=30) as client:
                r = await client.post(f"http://{remote_ip}:9000/api/chat", json={"message": f"Ejecuta: {cmd}"})
                if r.status_code == 200:
                    return json.dumps({"node": node_id, "info_type": info_type,
                                       "output": r.json().get("reply", "")}, indent=2, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "node": node_id}, indent=2)

    return json.dumps({"error": f"Node {node_id} not available"}, indent=2)


@mcp.tool()
async def execute_remote_task(node_id: str, task: str, timeout: int = 30) -> str:
    """Execute a task on a remote node via NexusHive."""
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    if node_id == "remote_node":
        remote_ip = get_remote_node_ip()
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                r = await client.post(f"http://{remote_ip}:9000/api/chat", json={"message": task})
                if r.status_code == 200:
                    return json.dumps({"node": node_id, "task": task, "success": True,
                                       "output": r.json().get("reply", "")}, indent=2, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": str(e), "node": node_id}, indent=2)
    return json.dumps({"error": f"Node {node_id} not reachable"}, indent=2)



# ---- TAREAS ENTRE AGENTES ----

@mcp.tool()
async def send_task_to_antigravity(task_description: str, priority: str = "medium") -> str:
    """Send a task to the Antigravity agent. Tasks are logged and processed asynchronously."""
    task_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    task_file = _TASK_DIR / f"task_{task_id}.json"
    task_data = {"id": task_id, "description": task_description, "priority": priority,
                 "timestamp": datetime.now().isoformat(), "status": "pending"}
    task_file.write_text(json.dumps(task_data, indent=2, ensure_ascii=False))
    # Also post to the message board
    conn = _get_board_conn()
    c = conn.cursor()
    c.execute("INSERT INTO messages (timestamp, sender, target, channel, content, msg_type) VALUES (?, ?, ?, ?, ?, ?)",
              (datetime.now().isoformat(), "task-manager", "antigravity", "tasks", task_description, "task"))
    conn.commit()
    conn.close()
    return json.dumps({"status": "task_logged", "task_id": task_id, "priority": priority}, indent=2)


@mcp.tool()
async def nexus_status() -> str:
    """Get full SuperNEXUS system status - API server, nodes, brain, everything."""
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    status = {"timestamp": datetime.now().isoformat(), "nexus_home": NEXUS_HOME}

    # Check API server
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"{NEXUS_API_BASE}/api/status")
            if r.status_code == 200:
                status["api_server"] = "online"
                api_data = r.json()
                status["engines"] = api_data.get("engines", {})
                status["cerebro"] = api_data.get("cerebro", {})
                status["nexus_hive"] = api_data.get("nexus_hive", {})
            else:
                status["api_server"] = "error"
    except Exception:
        status["api_server"] = "offline"

    # Check remote_node
    remote_ip = get_remote_node_ip()
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            r = await client.get(f"http://{remote_ip}:9000/api/status")
            status["remote_node"] = "online" if r.status_code == 200 else "degraded"
    except Exception:
        status["remote_node"] = "offline"

    # Check local Ollama
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            r = await client.get("http://localhost:11434/api/tags")
            if r.status_code == 200:
                models = r.json().get("models", [])
                status["ollama_local"] = {"status": "online", "models": len(models)}
    except Exception:
        status["ollama_local"] = {"status": "offline"}

    return json.dumps(status, indent=2, ensure_ascii=False)


# ---- OPTIMIZACION DE TOKENS ----

@mcp.tool()
async def optimize_prompt(prompt: str) -> str:
    """Compress a prompt removing filler words, redundancies. Returns compressed version + savings percentage. Use before sending expensive API calls."""
    import re
    original_tokens = len(prompt.split())
    compressions = {
        r'\s+': ' ', r'(?i)please\s': '', r'(?i)thank you': 'thx',
        r'(?i)could you': 'can you', r'(?i)would you mind': 'can you',
        r'(?i)I would like': 'I need', r'(?i)In my opinion': 'IMO',
        r'(?i)basically': '', r'(?i)essentially': '', r'(?i)obviously': '',
        r'(?i)as I mentioned earlier': '', r'(?i)as mentioned before': '',
        r'(?i)it is important to note that': '', r'(?i)it should be noted that': '',
    }
    compressed = prompt
    for pattern, replacement in compressions.items():
        compressed = re.sub(pattern, replacement, compressed)
    compressed = re.sub(r' +', ' ', compressed).strip()
    compressed_tokens = len(compressed.split())
    reduction = ((original_tokens - compressed_tokens) / original_tokens * 100) if original_tokens > 0 else 0
    return json.dumps({
        "original_tokens": original_tokens,
        "compressed_tokens": compressed_tokens,
        "reduction_percent": round(reduction, 1),
        "compressed": compressed,
    }, indent=2, ensure_ascii=False)


_TASK_CAPABILITY = {
    "simple": "fast", "categorization": "fast", "fast": "fast",
    "code": "code", "coding": "code", "test": "code",
    "analysis": "reasoning", "reasoning": "reasoning", "research": "reasoning",
    "design": "general", "general": "general", "creative": "general",
    "vision": "vision", "local": "general",
}


def _modelos_configurados() -> list[dict]:
    """Modelos realmente disponibles segun config.providers (nada hardcodeado).

    Antes esta tool recomendaba 'sonnet ($3/1M)' con el cloud apagado: un modelo
    que no existe en esta PC. Ahora lee lo que hay instalado/configurado.
    """
    from src.core.config import load_config

    cfg = load_config()
    provs = cfg.get("providers") or {}
    out: list[dict] = []

    ollama = provs.get("ollama") or {}
    if ollama.get("enabled", True):
        for m in ollama.get("models") or []:
            out.append({"model": str(m), "provider": "ollama", "cost": "FREE (local)"})

    cloud = provs.get("cloud") or {}
    if cloud.get("enabled") and cloud.get("api_key"):
        for m in cloud.get("models") or []:
            out.append({"model": str(m), "provider": "cloud", "cost": "BYOK"})

    for flavor, c in (provs.get("cli") or {}).items():
        if isinstance(c, dict) and c.get("enabled"):
            out.append({"model": str(c.get("model") or flavor), "provider": f"cli:{flavor}",
                        "cost": f"cuota de {flavor}"})
    return out


@mcp.tool()
async def select_model(task_type: str, complexity: str = "medium") -> str:
    """Select the optimal model for a task among the providers ENABLED in this PC.

    task_type: simple, categorization, code|coding, analysis, reasoning, research,
               design, general, vision, local
    complexity: low, medium, complex, deep
    """
    try:
        from src.core.model_assign import capabilities_of
        disponibles = [d for d in _modelos_configurados()
                       if "embed" not in capabilities_of(d["model"])]
    except Exception as e:
        return json.dumps({"error": f"no pude leer config.providers: {e}"}, indent=2)

    cap = _TASK_CAPABILITY.get((task_type or "").strip().lower())
    if cap is None:
        return json.dumps({"error": f"task_type desconocido: {task_type!r}",
                           "valid_task_types": sorted(_TASK_CAPABILITY),
                           "hint": "usar 'local' para forzar ejecucion gratuita"},
                          indent=2, ensure_ascii=False)
    if not disponibles:
        return json.dumps({"error": "no hay modelos de chat habilitados en config.providers",
                           "hint": "revisar providers.ollama.models / providers.cloud.enabled"},
                          indent=2, ensure_ascii=False)

    # Tareas livianas: si hay un modelo chico instalado, gana el chico.
    if (complexity or "").lower() == "low" and cap not in ("code", "vision"):
        if any("fast" in capabilities_of(d["model"]) for d in disponibles):
            cap = "fast"

    candidatos = [d for d in disponibles if cap in capabilities_of(d["model"])]
    motivo = f"capacidad '{cap}' entre los proveedores habilitados"
    if not candidatos:
        candidatos = disponibles
        motivo = f"no hay modelo con capacidad '{cap}'; primer modelo habilitado"

    elegido = candidatos[0]
    return json.dumps({
        "model": elegido["model"],
        "provider": elegido["provider"],
        "cost": elegido["cost"],
        "reason": motivo,
        "task_type": task_type,
        "complexity": complexity,
        "alternatives": [c["model"] for c in candidatos[1:6]],
        "available_models": [f"{d['provider']}/{d['model']}" for d in disponibles],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def token_report() -> str:
    """Get token optimization tips and the 9 techniques for 90% reduction. Use this to learn how to save tokens across all apps."""
    return json.dumps({
        "techniques": [
            {"name": "Context Window Awareness", "savings": "20-30%", "how": "Put critical instructions at TOP and BOTTOM of prompt, not middle"},
            {"name": "Prompt Compression", "savings": "10-15%", "how": "Remove filler words (basically, essentially, please). Use optimize_prompt tool"},
            {"name": "Structural Format", "savings": "20-30%", "how": "Use TASK/CONTEXT/CONSTRAINTS/OUTPUT format instead of prose"},
            {"name": "Incremental Execution", "savings": "30-40%", "how": "Do one step -> validate -> next. Fail fast, don't waste tokens"},
            {"name": "Tool Prioritization", "savings": "15-25%", "how": "grep -> read_specific_lines -> read_full. Cheapest tool first"},
            {"name": "Output Format", "savings": "30-50%", "how": "Always ask for JSON output, not prose. 23% more concise"},
            {"name": "Context Reuse", "savings": "70-90%", "how": "Load context once via memory_set, reuse N times via memory_get"},
            {"name": "Selective Memory", "savings": "95%+", "how": "Use brain_recall with specific topic, don't load all memory"},
            {"name": "No Repetition", "savings": "75%", "how": "State rules once in system prompt. Never repeat in messages"},
        ],
        "model_routing": {
            "FREE": ["ollama/nemotron", "ollama/qwen-coder", "OpenCode", "Antigravity"],
            "CHEAP": ["haiku ($0.80/1M)"],
            "BALANCED": ["sonnet ($3/1M)"],
            "EXPENSIVE": ["opus ($15/1M) - only for deep reasoning"],
        },
        "golden_rule": "Use free/local agents first. Claude only for high-value reasoning.",
    }, indent=2)


# ---- MONITOREO DE SISTEMA ----

@mcp.tool()
async def system_resources() -> str:
    """Check CPU, RAM, disk usage on this machine. Use before running heavy tasks to know if system can handle it."""
    stats = {"node": os.environ.get("NEXUS_NODE_NAME") or platform.node() or "local"}
    try:
        import psutil
        stats["cpu_percent"] = psutil.cpu_percent(interval=0.5)
        mem = psutil.virtual_memory()
        stats["ram_percent"] = mem.percent
        stats["ram_used_gb"] = round(mem.used / (1024**3), 1)
        stats["ram_total_gb"] = round(mem.total / (1024**3), 1)
        # Disco del proyecto (portable: funciona en NT, Linux, macOS)
        try:
            _ws = str(Path(__file__).resolve().parents[2])
            disk = psutil.disk_usage(_ws)
            stats["disk_workspace_percent"] = disk.percent
            stats["disk_workspace_free_gb"] = round(disk.free / (1024**3), 1)
        except Exception:
            pass
        # En Windows ademas el SystemDrive (C: por defecto)
        if os.name == "nt":
            try:
                _sd = os.environ.get("SystemDrive", "C:") + "\\"
                disk_c = psutil.disk_usage(_sd)
                stats["disk_sys_percent"] = disk_c.percent
                stats["disk_sys_free_gb"] = round(disk_c.free / (1024**3), 1)
            except Exception:
                pass
        stats["safe_to_run_heavy"] = stats["cpu_percent"] < 75 and stats["ram_percent"] < 80
    except ImportError:
        stats["error"] = "psutil not installed"
    return json.dumps(stats, indent=2)


# ============================================================
_SKILLS_BASE = Path(__file__).resolve().parents[2] / "skills" / "hub"
_SKILLS_CATALOG = Path(__file__).resolve().parents[2] / "skills" / "hub" / "SKILLS_CATALOG.md"
_CATALOG_CACHE = None


def _get_catalog():
    """Parse and cache the skills catalog for fast searching."""
    global _CATALOG_CACHE
    if _CATALOG_CACHE is not None:
        return _CATALOG_CACHE

    _CATALOG_CACHE = []
    if not _SKILLS_CATALOG.exists():
        # Fallback: list directories
        if _SKILLS_BASE.exists():
            for d in sorted(_SKILLS_BASE.iterdir()):
                if d.is_dir():
                    _CATALOG_CACHE.append({"name": d.name, "description": "", "tags": "", "category": "unknown"})
        return _CATALOG_CACHE

    current_category = ""
    for line in _SKILLS_CATALOG.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            m = re.match(r"## ([\w\s-]+) \((\d+)\)", line)
            if m:
                current_category = m.group(1).strip()
        elif line.startswith("| `"):
            parts = line.split("|")
            if len(parts) >= 5:
                name = parts[1].strip().strip("`")
                desc = parts[2].strip()
                tags = parts[3].strip()
                _CATALOG_CACHE.append({"name": name, "description": desc, "tags": tags, "category": current_category})
    return _CATALOG_CACHE


@mcp.tool()
def _skill_relevance_score(skill: dict, query_words: list, category: str = "") -> float:
    """Score a skill's relevance to a query using TF-IDF-like weighting.

    Scoring dimensions (inspired by AnythingLLM intelligent skill selection):
    - Name match: highest weight (name is the primary identifier)
    - Tag match: medium weight (tags are curated descriptors)
    - Description match: lowest weight (description is verbose)
    - Category boost: +0.2 if category matches exactly
    - Multi-word bonus: +0.1 per additional matching word beyond first
    """
    if not query_words:
        return 0.5  # neutral score when no query

    score = 0.0
    name_lower = skill["name"].lower()
    desc_lower = skill["description"].lower()
    tags_lower = skill.get("tags", "").lower()

    for i, word in enumerate(query_words):
        # Name match (highest weight: 0.4 per word)
        if word in name_lower:
            score += 0.4
            # Exact word boundary match bonus
            if f" {word} " in f" {name_lower} " or name_lower.startswith(word) or name_lower.endswith(word):
                score += 0.1
        # Tag match (medium weight: 0.3 per word)
        if word in tags_lower:
            score += 0.3
        # Description match (lowest weight: 0.15 per word)
        if word in desc_lower:
            score += 0.15
        # Multi-word bonus
        if i > 0:
            score += 0.05

    # Category boost
    if category and category.lower() in skill["category"].lower():
        score += 0.2

    return round(score, 3)


async def list_skills(query: str = "", category: str = "", limit: int = 30) -> str:
    """Search the 1,441+ skill catalog. Use query to filter by name/tags/description. Categories: architecture, business, data-ai, development, general, infrastructure, security, testing, workflow. Returns names to use with load_skill.

    Intelligent skill loading (inspired by AnythingLLM):
    - Results are scored by relevance, not just keyword presence
    - TF-IDF-like scoring: name > tags > description
    - Category boost for exact matches
    - Top-N results sorted by score descending
    """
    catalog = _get_catalog()
    query_words = query.lower().split() if query else []

    scored = []
    for skill in catalog:
        if category and category.lower() not in skill["category"].lower():
            continue
        if query_words:
            # Require at least one word to match (relaxed from all-words)
            searchable = f"{skill['name']} {skill['description']} {skill.get('tags', '')}".lower()
            if not any(word in searchable for word in query_words):
                continue
        score = _skill_relevance_score(skill, query_words, category)
        scored.append((score, {
            "name": skill["name"],
            "description": skill["description"][:150],
            "category": skill["category"],
            "score": score,
        }))

    # Sort by relevance score descending
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [item for _, item in scored[:limit]]

    return json.dumps({
        "results": results,
        "count": len(results),
        "total_catalog": len(catalog),
        "query": query,
        "hint": "Use load_skill(skill_name) to get full content"
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def load_skill(name: str) -> str:
    """Load the full content of a skill by name. Returns the complete SKILL.md with instructions, patterns, and knowledge."""
    skill_path = _SKILLS_BASE / name / "SKILL.md"

    if not skill_path.exists():
        # Fallback: search in src/skills as .py
        py_path = Path(__file__).resolve().parents[2] / "skills" / f"{name}.py"
        if py_path.exists():
            content = py_path.read_text(encoding="utf-8")
            return json.dumps({"name": name, "type": "python_skill", "content": content[:8000]}, ensure_ascii=False)
        return json.dumps({"error": f"Skill '{name}' not found", "searched": [str(skill_path), str(py_path)]})

    content = skill_path.read_text(encoding="utf-8")
    # Truncate if too large (some skills are 400KB+)
    if len(content) > 12000:
        content = content[:12000] + f"\n\n... [TRUNCATED - full skill is {len(content)} chars. Use load_skill_section for specific parts]"

    return json.dumps({"name": name, "type": "skill_md", "chars": len(content), "content": content}, ensure_ascii=False)


@mcp.tool()
async def load_skill_section(name: str, offset: int = 0, length: int = 8000) -> str:
    """Load a specific section of a large skill. Use offset and length to paginate through skills larger than 12000 bytes."""
    skill_path = _SKILLS_BASE / name / "SKILL.md"

    if not skill_path.exists():
        return json.dumps({"error": f"Skill '{name}' not found"})

    content = skill_path.read_text(encoding="utf-8")
    section = content[offset:offset + length]
    return json.dumps({
        "name": name,
        "total_chars": len(content),
        "offset": offset,
        "length": len(section),
        "has_more": offset + length < len(content),
        "content": section
    }, ensure_ascii=False)


# ---- MEMORIA BLAST (findings, decisions, cloud) ----

_FINDINGS = BRAIN_DIR / "findings.md"
_DECISIONS = BRAIN_DIR / "decisions.md"
_CLOUD = BRAIN_DIR / "cloud.md"

# Agent permissions (Suprawall)
_AGENT_PERMS = {
    "claude-code": {"execute": True, "read_all": True, "write_memory": True, "delegate": True, "max_tasks": 5},
    "opencode": {"execute": True, "read_all": True, "write_memory": True, "delegate": False, "max_tasks": 3},
    "antigravity": {"execute": True, "read_all": True, "write_memory": True, "delegate": True, "max_tasks": 5},
    "openclaw": {"execute": True, "read_all": False, "write_memory": False, "delegate": False, "max_tasks": 2},
}


@mcp.tool()
async def add_finding(content: str, agent: str = "claude-code") -> str:
    """Record a finding/discovery to the shared findings log. Use when you discover something useful during a task."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## [{ts}] {agent}\n{content}\n"
    if not _FINDINGS.exists():
        _FINDINGS.write_text("# Findings - NexusHive\n\nHallazgos de los agentes.\n\n", encoding="utf-8")
    with open(_FINDINGS, "a", encoding="utf-8") as f:
        f.write(entry)
    return json.dumps({"recorded": True, "agent": agent, "file": str(_FINDINGS)})


@mcp.tool()
async def add_decision(decision: str, reason: str, agent: str = "claude-code") -> str:
    """Record a decision and its reasoning. Builds the system's learning history."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"\n## [{ts}] {agent}\n**Decision:** {decision}\n**Reason:** {reason}\n"
    if not _DECISIONS.exists():
        _DECISIONS.write_text("# Decisions - NexusHive\n\nDecisiones y razonamiento.\n\n", encoding="utf-8")
    with open(_DECISIONS, "a", encoding="utf-8") as f:
        f.write(entry)
    return json.dumps({"recorded": True, "agent": agent, "file": str(_DECISIONS)})


@mcp.tool()
async def read_cloud() -> str:
    """Read the master instructions file (cloud.md). Contains system identity, rules, and BLAST framework config."""
    if not _CLOUD.exists():
        return json.dumps({"error": "cloud.md not initialized. Run nexus_autonomous_loop.py --cycles 1 to create it."})
    content = _CLOUD.read_text(encoding="utf-8")
    return json.dumps({"content": content}, ensure_ascii=False)


@mcp.tool()
async def check_permissions(agent: str) -> str:
    """Check what permissions an agent has in the Suprawall security system."""
    perms = _AGENT_PERMS.get(agent)
    if not perms:
        return json.dumps({"error": f"Unknown agent: {agent}", "known_agents": list(_AGENT_PERMS.keys())})
    return json.dumps({"agent": agent, "permissions": perms}, indent=2)


# ============================================================
# NEXUS MEMORY (observations + findings — nexus_memory.db)
# ============================================================
_MEMORY_DB = BRAIN_DIR / "nexus_memory.db"


def _get_memory_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(_MEMORY_DB), timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _init_memory_db():
    conn = _get_memory_conn()
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS observations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        content TEXT NOT NULL,
        category TEXT DEFAULT 'general',
        project TEXT DEFAULT 'nexus-v3',
        metadata TEXT DEFAULT '{}'
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS findings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        timestamp TEXT NOT NULL,
        agent TEXT NOT NULL,
        content TEXT NOT NULL,
        topic TEXT DEFAULT ''
    )""")
    # FTS5 index for fast search
    try:
        c.execute("""CREATE VIRTUAL TABLE IF NOT EXISTS observations_fts
            USING fts5(content, category, project, content='observations', content_rowid='id')""")
    except Exception:
        pass  # Already exists or FTS5 not available

    # Additive migration (engram pattern): topic-key upserts + soft delete + dedupe.
    # SQLite ALTER TABLE ADD COLUMN is safe; column-existence guard via PRAGMA.
    existing_cols = {row[1] for row in c.execute("PRAGMA table_info(observations)")}
    if "topic_key" not in existing_cols:
        c.execute("ALTER TABLE observations ADD COLUMN topic_key TEXT")
    if "deleted_at" not in existing_cols:
        c.execute("ALTER TABLE observations ADD COLUMN deleted_at TEXT")
    if "revision_count" not in existing_cols:
        c.execute("ALTER TABLE observations ADD COLUMN revision_count INTEGER DEFAULT 0")
    if "updated_at" not in existing_cols:
        c.execute("ALTER TABLE observations ADD COLUMN updated_at TEXT")
    if "content_hash" not in existing_cols:
        c.execute("ALTER TABLE observations ADD COLUMN content_hash TEXT")
    if "agent" not in existing_cols:
        c.execute("ALTER TABLE observations ADD COLUMN agent TEXT DEFAULT 'unknown'")
    # Indexes for the new fields (idempotent).
    c.execute("CREATE INDEX IF NOT EXISTS idx_obs_topic ON observations(topic_key, project, category)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_obs_deleted ON observations(deleted_at)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_obs_hash ON observations(content_hash, project, ts)")

    # Memory graph edges (jcode petgraph pattern lite). Each row is a
    # typed relation between two observations. Bidirectional queries are
    # cheap because both endpoints are indexed.
    c.execute("""CREATE TABLE IF NOT EXISTS observation_edges (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        from_obs INTEGER NOT NULL,
        to_obs INTEGER NOT NULL,
        relation TEXT NOT NULL,   -- supersedes|contradicts|derived_from|relates_to
        weight REAL DEFAULT 1.0,
        created_at TEXT NOT NULL,
        note TEXT DEFAULT '',
        FOREIGN KEY (from_obs) REFERENCES observations(id),
        FOREIGN KEY (to_obs)   REFERENCES observations(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_edges_from ON observation_edges(from_obs, relation)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_edges_to ON observation_edges(to_obs, relation)")

    conn.commit()
    conn.close()

_init_memory_db()


# ---- FASE 1.2: Entity Linking (mem0-inspired) ----
# Extracts entities from observation content and links observations
# that share the same entities (file paths, function names, classes, etc.)

_ENTITY_PATTERNS = [
    # File paths (Python, JS, etc.)
    (r'(?:src|lib|app|core|api|memory|security|bridges)/[\w/]+\.py', "file"),
    (r'[\w/]+\.py', "file"),
    (r'[\w/]+\.js', "file"),
    (r'[\w/]+\.ts', "file"),
    # Function/method calls
    (r'\bdef\s+(\w+)', "function"),
    (r'\basync\s+def\s+(\w+)', "function"),
    # Class names (PascalCase)
    (r'\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b', "class"),
    # Tool names (snake_case with tool-like suffixes)
    (r'\b(\w+_(?:tool|server|client|manager|handler|coordinator|engine|scanner))\b', "tool"),
    # URLs
    (r'https?://[\w./\-]+', "url"),
    # MCP tool names
    (r'\b(mcp__\w+__\w+)\b', "mcp_tool"),
]

# Topic family prefixes (Engram-inspired)
_TOPIC_FAMILIES = {
    "architecture": "architecture",
    "arch": "architecture",
    "bug": "bugfix",
    "fix": "bugfix",
    "feature": "feature",
    "feat": "feature",
    "security": "security",
    "sec": "security",
    "perf": "performance",
    "performance": "performance",
    "config": "configuration",
    "setup": "configuration",
    "test": "testing",
    "refactor": "refactoring",
    "docs": "documentation",
    "memory": "memory",
    "brain": "brain",
    "skill": "skills",
    "voice": "voice",
    "ui": "ui",
    "api": "api",
    "deploy": "deployment",
    "training": "training",
    "model": "models",
}


def _extract_entities(content: str) -> list:
    """Extract entities from observation content using regex patterns.

    Returns list of (entity_text, entity_type) tuples.
    Inspired by mem0's entity linking for cross-memory retrieval boosting.
    """
    entities = []
    seen = set()
    for pattern, etype in _ENTITY_PATTERNS:
        for match in re.finditer(pattern, content):
            text = match.group(1) if match.groups() else match.group(0)
            text = text.strip()
            if text and text not in seen and len(text) > 2:
                seen.add(text)
                entities.append({"text": text, "type": etype})
    return entities


def _get_topic_family(topic_key: str) -> str:
    """Determine the family of a topic key based on its prefix.

    Examples:
        'architecture/auth' → 'architecture'
        'bug/memory-leak' → 'bugfix'
        'feature/new-gem' → 'feature'
        'security/suprawall' → 'security'
        'general' → 'general'
    """
    if not topic_key:
        return "general"
    prefix = topic_key.split("/")[0].lower().rstrip("s")  # strip plurals
    return _TOPIC_FAMILIES.get(prefix, prefix)


def _store_entities(conn, obs_id: int, entities: list):
    """Store extracted entities and auto-link to observations with shared entities."""
    c = conn.cursor()
    # Ensure entity table exists
    c.execute("""CREATE TABLE IF NOT EXISTS observation_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        obs_id INTEGER NOT NULL,
        entity_text TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        FOREIGN KEY (obs_id) REFERENCES observations(id)
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_entity_text ON observation_entities(entity_text)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_entity_obs ON observation_entities(obs_id)")

    # Insert entities for this observation
    for ent in entities:
        c.execute(
            "INSERT INTO observation_entities (obs_id, entity_text, entity_type) VALUES (?, ?, ?)",
            (obs_id, ent["text"], ent["type"]),
        )

    # Auto-link: find other observations with the same entity and create relates_to edges
    linked = 0
    for ent in entities:
        c.execute(
            "SELECT DISTINCT obs_id FROM observation_entities WHERE entity_text=? AND obs_id!=? LIMIT 5",
            (ent["text"], obs_id),
        )
        for row in c.fetchall():
            other_id = row[0]
            # Check edge doesn't already exist
            c.execute(
                "SELECT id FROM observation_edges WHERE from_obs=? AND to_obs=? AND relation='relates_to'",
                (obs_id, other_id),
            )
            if not c.fetchone():
                c.execute(
                    "INSERT INTO observation_edges (from_obs, to_obs, relation, weight, created_at, note) "
                    "VALUES (?, ?, 'relates_to', 0.8, ?, ?)",
                    (obs_id, other_id, datetime.now().isoformat(), f"shared_entity:{ent['text']}"),
                )
                linked += 1

    return linked


def _content_hash(content: str, category: str, project: str) -> str:
    """SHA1 short hash of (content, category, project) for dedupe lookups."""
    h = hashlib.sha1()
    h.update(content.encode("utf-8", errors="replace"))
    h.update(b"|")
    h.update(category.encode("utf-8", errors="replace"))
    h.update(b"|")
    h.update(project.encode("utf-8", errors="replace"))
    return h.hexdigest()[:16]


@mcp.tool()
async def add_observation(
    content: str,
    category: str = "general",
    project: str = "nexus-v3",
    agent: str = "claude-code",
    metadata: str = "{}",
    topic_key: str = "",
    dedupe_window_h: int = 24,
) -> str:
    """Add an observation to nexus_memory.db.

    Engram-style smart write modes (both opt-in, default behavior unchanged):

    - topic_key: stable identifier for an evolving topic (e.g.
      'architecture/auth' or 'pref/python-version'). When provided, the
      same (topic_key, project, category) triple becomes an UPSERT: the
      existing observation is updated in place, revision_count is bumped,
      content_hash is refreshed. Solves the classic "user said 3.10 yesterday,
      3.12 today — two contradictory rows" problem.

    - dedupe_window_h: when topic_key is empty, if an observation with the
      same (content, category, project) hash exists within the last N hours,
      return the existing id instead of inserting a duplicate. Pass 0 to
      disable dedupe entirely (legacy append-only behavior).

    Categories: general, report, task, analysis, ide_capabilities,
    deepseek-tui, vision, sprint.
    """
    conn = _get_memory_conn()
    c = conn.cursor()
    ts = datetime.now().isoformat()
    meta_dict = {"agent": agent}
    if metadata and metadata != "{}":
        try:
            meta_dict.update(json.loads(metadata))
        except Exception:
            pass
    meta_json = json.dumps(meta_dict)
    chash = _content_hash(content, category, project)

    # Mode A: topic_key upsert
    if topic_key:
        c.execute(
            "SELECT id, revision_count FROM observations "
            "WHERE topic_key=? AND project=? AND category=? AND deleted_at IS NULL "
            "ORDER BY id DESC LIMIT 1",
            (topic_key, project, category),
        )
        row = c.fetchone()
        if row:
            obs_id, rev = row[0], (row[1] or 0)
            c.execute(
                "UPDATE observations SET content=?, metadata=?, updated_at=?, "
                "revision_count=?, content_hash=? WHERE id=?",
                (content, meta_json, ts, rev + 1, chash, obs_id),
            )
            # Refresh FTS (delete + reinsert keeps it consistent without triggers)
            try:
                c.execute("INSERT INTO observations_fts(observations_fts, rowid, content, category, project) "
                          "VALUES('delete', ?, '', '', '')", (obs_id,))
            except Exception:
                pass
            try:
                c.execute("INSERT INTO observations_fts (rowid, content, category, project) VALUES (?, ?, ?, ?)",
                          (obs_id, content, category, project))
            except Exception:
                pass

            # FASE 1.2: Re-extract entities on upsert (content changed)
            _extract_entities(content)  # warm the cache
            try:
                # Clear old entities and re-extract
                c.execute("DELETE FROM observation_entities WHERE obs_id=?", (obs_id,))
                entities = _extract_entities(content)
                if entities:
                    _store_entities(conn, obs_id, entities)
            except Exception:
                pass

            conn.commit()
            conn.close()
            # SalienceTracker: tag observation
            try:
                from src.brain.salience import salience
                salience.tag(str(obs_id), [category])
            except Exception:
                pass
            return json.dumps({
                "id": obs_id, "mode": "upsert", "topic_key": topic_key,
                "revision": rev + 1, "updated_at": ts,
            }, indent=2)

    # Mode B: dedupe within window
    if dedupe_window_h and dedupe_window_h > 0:
        from datetime import timedelta
        cutoff = (datetime.now() - timedelta(hours=dedupe_window_h)).isoformat()
        c.execute(
            "SELECT id FROM observations WHERE content_hash=? AND project=? "
            "AND ts>=? AND deleted_at IS NULL ORDER BY id DESC LIMIT 1",
            (chash, project, cutoff),
        )
        row = c.fetchone()
        if row:
            conn.close()
            # SalienceTracker: tag observation
            try:
                from src.brain.salience import salience
                salience.tag(str(row[0]), [category])
            except Exception:
                pass
            return json.dumps({
                "id": row[0], "mode": "dedup", "content_hash": chash,
            }, indent=2)

    # Mode C: insert new observation
    topic_family = _get_topic_family(topic_key) if topic_key else None
    c.execute(
        "INSERT INTO observations (content, metadata, ts, category, project, agent, content_hash, topic_key) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (content, meta_json, ts, category, project, agent, chash, topic_key or None),
    )
    obs_id = c.lastrowid
    try:
        c.execute("INSERT INTO observations_fts (rowid, content, category, project) VALUES (?, ?, ?, ?)",
                  (obs_id, content, category, project))
    except Exception:
        pass

    # FASE 1.2: Entity linking — extract entities and auto-link
    entities = _extract_entities(content)
    links_created = 0
    if entities:
        links_created = _store_entities(conn, obs_id, entities)

    # FASE 1.3: Store topic family in metadata
    if topic_family and topic_family != "general":
        try:
            meta_dict["topic_family"] = topic_family
            meta_json = json.dumps(meta_dict)
            c.execute("UPDATE observations SET metadata=? WHERE id=?", (meta_json, obs_id))
        except Exception:
            pass

    conn.commit()
    conn.close()
    # SalienceTracker: tag observation with category for salience scoring
    try:
        from src.brain.salience import salience
        salience.tag(str(obs_id), [category])
    except Exception:
        pass
    return json.dumps({
        "id": obs_id, "mode": "insert", "timestamp": ts,
        "category": category, "agent": agent,
        "topic_key": topic_key or None,
        "topic_family": topic_family,
        "entities_extracted": len(entities),
        "links_created": links_created,
    }, indent=2)


@mcp.tool()
async def describe_image(image_data: str, instruction: str = "Describe this image") -> str:
    """Describe an image using a local vision model (detectado en Ollama de esta PC).

    Args:
        image_data: Base64 encoded image data
        instruction: Description instruction for the image

    Returns:
        JSON string with vision analysis results
    """
    import httpx
    import base64
    from src.core.environment import list_ollama_models, pick_model

    # Familias que sí aceptan imágenes. `gemma4` estaba primero en la lista y
    # Ollama lo rechaza con "model does not support multimodal requests": la tool
    # fallaba siempre aunque hubiera qwen2.5vl instalado.
    VISION_FAMILIES = ("qwen2.5vl", "qwen3-vl", "qwen3vl", "llava", "moondream",
                       "janus", "minicpm-v", "gemma3")
    instalados = list_ollama_models(ollama_base_url())
    candidatos: list[str] = []
    for familia in VISION_FAMILIES:
        elegido = pick_model(instalados, prefer=(familia,))
        if elegido and elegido not in candidatos:
            candidatos.append(elegido)
    if not candidatos:
        candidatos = [pick_model(instalados) or "qwen2.5vl:7b"]

    result = {
        "provider": "vision-ollama",
        "model": candidatos[0],
        "instruction": instruction,
        "success": False,
        "error": "Vision analysis failed"
    }

    try:
        # Clean the image data (remove data: prefix if present)
        img_clean = image_data
        if img_clean.startswith("data:"):
            img_clean = img_clean.split(",", 1)[1]

        # `/api/vision/process` no existe en Ollama (404): la tool devolvía
        # success=False siempre. El endpoint real que acepta `images` es /api/generate.
        url = f"{ollama_base_url()}/api/generate"

        async with httpx.AsyncClient(timeout=120.0) as client:
            for modelo in candidatos[:3]:
                result["model"] = modelo
                resp = await client.post(url, json={
                    "model": modelo,
                    "prompt": instruction,
                    "images": [img_clean],
                    "stream": False,
                })
                if resp.status_code == 200:
                    data = resp.json()
                    result["success"] = True
                    result["response"] = data.get("response", "")
                    result["error"] = ""
                    break
                result["error"] = f"Vision API HTTP {resp.status_code}: {resp.text[:200]}"
                if "multimodal" not in resp.text.lower():
                    break  # el error no es "este modelo no ve": no insistas

    except Exception as e:
        result["error"] = f"Vision analysis failed: {str(e)}"

    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def add_episode(
    what: str,
    why: str = "",
    where: str = "",
    learned: str = "",
    category: str = "episode",
    project: str = "nexus-v3",
    agent: str = "claude-code",
    topic_key: str = "",
    type: str = "decision",
) -> str:
    """Record an episode (engram pattern): a structured What/Why/Where/Learned
    memory richer than a flat observation. Preserves causality, not just facts.

    Args:
        what: what happened (mandatory) — concrete action / event / decision.
        why: motivation, trigger, root cause.
        where: file paths, URLs, system area touched.
        learned: takeaway, pattern, lesson for the future.
        category: storage category (default "episode" — keeps episodes
                  separable from flat observations).
        type: tag inside content for fast scanning ("decision" | "bugfix"
              | "discovery" | "pattern" | "config"). Free-form ok.
        topic_key: pass-through to add_observation upsert mode (recommended
                   for evolving topics so revisions update in place).

    The structured body is rendered as canonical markdown so search hits
    can be skim-read without unpacking JSON. Routing then delegates to
    add_observation, inheriting topic-key upsert + dedupe + soft-delete.
    """
    if not what or not what.strip():
        return json.dumps({"error": "'what' is required"})
    parts = [f"## {type.upper()}: {what.strip()}"]
    if why.strip():
        parts.append(f"\n### Why\n{why.strip()}")
    if where.strip():
        parts.append(f"\n### Where\n{where.strip()}")
    if learned.strip():
        parts.append(f"\n### Learned\n{learned.strip()}")
    content = "\n".join(parts)
    meta = json.dumps({"agent": agent, "episode_type": type})
    # Delegate to add_observation so we get upsert + dedupe + FTS for free.
    result = await add_observation(
        content=content,
        category=category,
        project=project,
        agent=agent,
        metadata=meta,
        topic_key=topic_key,
        dedupe_window_h=24,
    )
    # Inject the structured shape on top of the routing result for clarity.
    try:
        parsed = json.loads(result)
        parsed["episode_type"] = type
        parsed["structured"] = True
        return json.dumps(parsed, indent=2)
    except Exception:
        return result


VALID_RELATIONS = {"supersedes", "contradicts", "derived_from", "relates_to"}


@mcp.tool()
async def relate_observations(
    from_obs: int, to_obs: int, relation: str = "relates_to",
    weight: float = 1.0, note: str = "",
) -> str:
    """Create a typed edge between two observations (jcode petgraph pattern).

    relation must be one of: supersedes | contradicts | derived_from | relates_to
    Idempotent on (from, to, relation) — duplicates silently skipped.
    """
    rel = (relation or "relates_to").lower()
    if rel not in VALID_RELATIONS:
        return json.dumps({"error": f"invalid relation; use one of {sorted(VALID_RELATIONS)}"})
    conn = _get_memory_conn()
    c = conn.cursor()
    # Check both endpoints exist
    c.execute("SELECT id FROM observations WHERE id IN (?, ?)", (from_obs, to_obs))
    if len(c.fetchall()) != 2:
        conn.close()
        return json.dumps({"error": "one or both observations not found"})
    # Idempotent insert
    c.execute(
        "SELECT id FROM observation_edges WHERE from_obs=? AND to_obs=? AND relation=?",
        (from_obs, to_obs, rel),
    )
    existing = c.fetchone()
    if existing:
        conn.close()
        return json.dumps({"id": existing[0], "mode": "exists", "relation": rel})
    c.execute(
        "INSERT INTO observation_edges (from_obs, to_obs, relation, weight, created_at, note) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (from_obs, to_obs, rel, weight, datetime.now().isoformat(), note),
    )
    eid = c.lastrowid
    conn.commit()
    conn.close()
    return json.dumps({"id": eid, "mode": "created", "from": from_obs, "to": to_obs,
                       "relation": rel, "weight": weight})


@mcp.tool()
async def get_observation_neighbors(obs_id: int, relation: str = "") -> str:
    """Return neighbors of an observation. Optional relation filter."""
    conn = _get_memory_conn()
    c = conn.cursor()
    if relation:
        rel = relation.lower()
        c.execute(
            "SELECT id, to_obs, relation, weight, note, created_at FROM observation_edges "
            "WHERE from_obs=? AND relation=?", (obs_id, rel),
        )
        outgoing = [dict(zip(["edge_id","obs_id","relation","weight","note","created_at"], r))
                    for r in c.fetchall()]
        c.execute(
            "SELECT id, from_obs, relation, weight, note, created_at FROM observation_edges "
            "WHERE to_obs=? AND relation=?", (obs_id, rel),
        )
        incoming = [dict(zip(["edge_id","obs_id","relation","weight","note","created_at"], r))
                    for r in c.fetchall()]
    else:
        c.execute(
            "SELECT id, to_obs, relation, weight, note, created_at FROM observation_edges "
            "WHERE from_obs=?", (obs_id,),
        )
        outgoing = [dict(zip(["edge_id","obs_id","relation","weight","note","created_at"], r))
                    for r in c.fetchall()]
        c.execute(
            "SELECT id, from_obs, relation, weight, note, created_at FROM observation_edges "
            "WHERE to_obs=?", (obs_id,),
        )
        incoming = [dict(zip(["edge_id","obs_id","relation","weight","note","created_at"], r))
                    for r in c.fetchall()]
    conn.close()
    return json.dumps({"obs_id": obs_id, "outgoing": outgoing, "incoming": incoming,
                       "out_count": len(outgoing), "in_count": len(incoming)}, indent=2)


@mcp.tool()
async def search_by_entity(entity_text: str = "", entity_type: str = "", limit: int = 10) -> str:
    """Search observations by extracted entity (file, function, class, tool, url).

    FASE 1.2: Entity linking — finds observations that share the same entity,
    enabling cross-observation retrieval boosting (mem0-inspired).
    """
    conn = _get_memory_conn()
    c = conn.cursor()
    # Ensure table exists
    c.execute("""CREATE TABLE IF NOT EXISTS observation_entities (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        obs_id INTEGER NOT NULL,
        entity_text TEXT NOT NULL,
        entity_type TEXT NOT NULL,
        FOREIGN KEY (obs_id) REFERENCES observations(id)
    )""")

    results = []
    if entity_text:
        q = f"%{entity_text}%"
        if entity_type:
            c.execute(
                "SELECT DISTINCT e.obs_id, e.entity_text, e.entity_type, o.content, o.category, o.ts "
                "FROM observation_entities e JOIN observations o ON e.obs_id=o.id "
                "WHERE e.entity_text LIKE ? AND e.entity_type=? AND o.deleted_at IS NULL "
                "ORDER BY o.ts DESC LIMIT ?",
                (q, entity_type, limit),
            )
        else:
            c.execute(
                "SELECT DISTINCT e.obs_id, e.entity_text, e.entity_type, o.content, o.category, o.ts "
                "FROM observation_entities e JOIN observations o ON e.obs_id=o.id "
                "WHERE e.entity_text LIKE ? AND o.deleted_at IS NULL "
                "ORDER BY o.ts DESC LIMIT ?",
                (q, limit),
            )
        for row in c.fetchall():
            results.append({
                "obs_id": row[0],
                "entity": row[1],
                "entity_type": row[2],
                "content_preview": row[3][:200],
                "category": row[4],
                "timestamp": row[5],
            })

    # Also get entity stats
    c.execute("SELECT entity_type, COUNT(DISTINCT entity_text) FROM observation_entities GROUP BY entity_type")
    stats = {row[0]: row[1] for row in c.fetchall()}

    conn.close()
    return json.dumps({
        "results": results,
        "count": len(results),
        "entity_stats": stats,
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_topic_families(project: str = "nexus-v3") -> str:
    """Get topic family distribution for observations.

    FASE 1.3: Family heuristics — groups topic keys by family prefix
    (architecture/*, bug/*, feature/*, etc.) for hierarchical retrieval.
    """
    conn = _get_memory_conn()
    c = conn.cursor()
    c.execute(
        "SELECT topic_key, metadata, category FROM observations "
        "WHERE project=? AND deleted_at IS NULL AND topic_key IS NOT NULL AND topic_key != ''",
        (project,),
    )
    families = {}
    for row in c.fetchall():
        tk = row[0]
        family = _get_topic_family(tk)
        if family not in families:
            families[family] = {"count": 0, "topic_keys": []}
        families[family]["count"] += 1
        if tk not in families[family]["topic_keys"]:
            families[family]["topic_keys"].append(tk)

    # Sort by count descending
    sorted_families = sorted(families.items(), key=lambda x: x[1]["count"], reverse=True)
    conn.close()
    return json.dumps({
        "families": [
            {"family": f, "count": v["count"], "topic_keys": v["topic_keys"][:10]}
            for f, v in sorted_families
        ],
        "total_families": len(families),
        "total_topics": sum(v["count"] for v in families.values()),
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def delete_observation(obs_id: int, hard: bool = False) -> str:
    """Delete an observation. Soft delete by default (sets deleted_at, keeps row
    + FTS index — recoverable). Pass hard=True to physically remove the row
    (irreversible; engram parity for admin cleanup)."""
    conn = _get_memory_conn()
    c = conn.cursor()
    c.execute("SELECT id FROM observations WHERE id=?", (obs_id,))
    if not c.fetchone():
        conn.close()
        return json.dumps({"error": f"observation {obs_id} not found"})
    if hard:
        c.execute("DELETE FROM observations WHERE id=?", (obs_id,))
        try:
            c.execute("INSERT INTO observations_fts(observations_fts, rowid, content, category, project) "
                      "VALUES('delete', ?, '', '', '')", (obs_id,))
        except Exception:
            pass
        mode = "hard"
    else:
        c.execute("UPDATE observations SET deleted_at=? WHERE id=?",
                  (datetime.now().isoformat(), obs_id))
        mode = "soft"
    conn.commit()
    conn.close()
    return json.dumps({"id": obs_id, "deleted": True, "mode": mode})


@mcp.tool()
async def search_observations(
    query: str = "",
    category: str = "",
    project: str = "",
    limit: int = 10,
    boost_entities: bool = True,
) -> str:
    """Search observations in nexus_memory.db. Use query for FTS5 full-text search, or filter by category/project. This is the primary knowledge base for tasks, reports, and analysis.

    FASE 3.1: Entity-based retrieval boosting — when boost_entities=True,
    extracts entities from the query and boosts observations that share
    those entities (mem0-inspired retrieval enhancement).
    """
    conn = _get_memory_conn()
    c = conn.cursor()

    # FASE 3.1: Extract entities from query for boosting
    query_entities = set()
    if boost_entities and query:
        try:
            ents = _extract_entities(query)
            query_entities = {e["text"].lower() for e in ents}
        except Exception:
            pass

    if query:
        # FTS5 search
        try:
            c.execute(
                "SELECT o.id, o.ts, o.content, o.category, o.project FROM observations o "
                "JOIN observations_fts f ON o.id = f.rowid "
                "WHERE observations_fts MATCH ? AND o.deleted_at IS NULL "
                "ORDER BY rank LIMIT ?",
                (query, limit * 3),  # fetch more for re-ranking
            )
        except Exception:
            # Fallback to LIKE
            c.execute(
                "SELECT id, ts, content, category, project FROM observations "
                "WHERE content LIKE ? AND deleted_at IS NULL "
                "ORDER BY id DESC LIMIT ?",
                (f"%{query}%", limit * 3),
            )
    elif category:
        c.execute(
            "SELECT id, ts, content, category, project FROM observations "
            "WHERE category = ? AND deleted_at IS NULL ORDER BY id DESC LIMIT ?",
            (category, limit * 3),
        )
    else:
        c.execute(
            "SELECT id, ts, content, category, project FROM observations "
            "WHERE deleted_at IS NULL ORDER BY id DESC LIMIT ?",
            (limit * 3,),
        )
    rows = c.fetchall()

    # FASE 3.1: Entity-based re-ranking
    results = []
    for r in rows:
        obs_id, ts, content, cat, proj = r[0], r[1], r[2], r[3], r[4]
        entity_boost = 0.0
        matched_entities = []

        if query_entities and boost_entities:
            # Check if observation shares entities with query
            try:
                c2 = conn.cursor()
                c2.execute(
                    "SELECT DISTINCT entity_text FROM observation_entities WHERE obs_id=?",
                    (obs_id,),
                )
                obs_entities = {row[0].lower() for row in c2.fetchall()}
                shared = query_entities & obs_entities
                if shared:
                    entity_boost = len(shared) * 0.2  # 0.2 boost per shared entity
                    matched_entities = list(shared)
            except Exception:
                pass

        results.append({
            "id": obs_id, "timestamp": ts, "content": content[:500],
            "category": cat, "project": proj,
            "entity_boost": round(entity_boost, 2),
            "matched_entities": matched_entities,
        })

    # Sort by entity boost descending, then by original order
    if boost_entities and query_entities:
        results.sort(key=lambda x: x["entity_boost"], reverse=True)

    # Trim to limit
    results = results[:limit]

    conn.close()
    return json.dumps({"results": results, "count": len(results)}, indent=2, ensure_ascii=False)


@mcp.tool()
async def get_observation(obs_id: int) -> str:
    """Get a single observation by ID from nexus_memory.db. Returns full content without truncation."""
    conn = _get_memory_conn()
    c = conn.cursor()
    c.execute("SELECT id, ts, content, category, project, metadata FROM observations WHERE id = ?", (obs_id,))
    row = c.fetchone()
    conn.close()
    if not row:
        return json.dumps({"error": f"Observation {obs_id} not found"})
    return json.dumps({
        "id": row[0], "timestamp": row[1], "content": row[2],
        "category": row[3], "project": row[4], "metadata": row[5],
    }, indent=2, ensure_ascii=False)


@mcp.tool()
async def add_task_finding(
    content: str,
    topic: str = "",
    agent: str = "claude-code",
) -> str:
    """Record a task finding in nexus_memory.db. Use for task progress, completions, and results that need to persist."""
    conn = _get_memory_conn()
    c = conn.cursor()
    ts = datetime.now().isoformat()
    c.execute(
        "INSERT INTO findings (timestamp, agent, content, topic) VALUES (?, ?, ?, ?)",
        (ts, agent, content, topic),
    )
    fid = c.lastrowid
    conn.commit()
    conn.close()
    return json.dumps({"id": fid, "timestamp": ts, "agent": agent, "topic": topic}, indent=2)


@mcp.tool()
async def list_findings(
    agent: str = "",
    topic: str = "",
    limit: int = 10,
) -> str:
    """List findings from nexus_memory.db. Filter by agent or topic."""
    conn = _get_memory_conn()
    c = conn.cursor()
    query = "SELECT id, timestamp, agent, content, topic FROM findings WHERE 1=1"
    params = []
    if agent:
        query += " AND agent = ?"
        params.append(agent)
    if topic:
        query += " AND topic LIKE ?"
        params.append(f"%{topic}%")
    query += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    c.execute(query, params)
    rows = c.fetchall()
    conn.close()
    results = [
        {"id": r[0], "timestamp": r[1], "agent": r[2], "content": r[3][:500], "topic": r[4]}
        for r in rows
    ]
    return json.dumps({"results": results, "count": len(results)}, indent=2, ensure_ascii=False)


@mcp.tool()
async def memory_stats() -> str:
    """Get nexus_memory.db statistics — observations count, findings count, categories, agents."""
    conn = _get_memory_conn()
    c = conn.cursor()
    stats = {"db_path": str(_MEMORY_DB)}
    try:
        c.execute("SELECT COUNT(*) FROM observations")
        stats["total_observations"] = c.fetchone()[0]
        c.execute("SELECT DISTINCT category FROM observations")
        stats["categories"] = [r[0] for r in c.fetchall()]
        c.execute("SELECT COUNT(*) FROM findings")
        stats["total_findings"] = c.fetchone()[0]
        c.execute("SELECT DISTINCT agent FROM findings")
        stats["finding_agents"] = [r[0] for r in c.fetchall()]
    except Exception as e:
        stats["error"] = str(e)
    conn.close()
    return json.dumps(stats, indent=2, ensure_ascii=False)


# ============================================================
# New Modules: AdaptiveRouter, SelfLearning, HierarchicalMemory
# ============================================================


@mcp.tool()
async def router_stats() -> str:
    """Get AdaptiveRouter statistics — Thompson Sampling outcomes per model, success rates, alpha/beta distributions."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.adaptive_router import AdaptiveRouter
        router = AdaptiveRouter()
        stats = router.get_stats()
        return json.dumps({"router_stats": stats, "candidates": router.get_candidates("")}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def router_select(task: str, prefer_speed: bool = False, prefer_quality: bool = False) -> str:
    """Select the best model for a task using Thompson Sampling. Returns model name and all candidates with scores."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.adaptive_router import AdaptiveRouter
        router = AdaptiveRouter()
        model = router.select_model(task, prefer_speed=prefer_speed, prefer_quality=prefer_quality)
        candidates = router.get_candidates(task)
        return json.dumps({"selected": model, "prefer_speed": prefer_speed, "prefer_quality": prefer_quality,
                           "candidates": candidates}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def self_learning_status() -> str:
    """Get SelfLearningLoop status — cycles, pending records, last cycle timestamp."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.self_learning_loop import SelfLearningLoop
        # Try to get stats from the singleton
        loop = SelfLearningLoop.get_instance() if hasattr(SelfLearningLoop, 'get_instance') else SelfLearningLoop()
        stats = loop.get_stats() if hasattr(loop, 'get_stats') else {
            "status": "running" if hasattr(loop, '_running') else "initialized",
            "actor": "self_learning_loop",
            "description": "Continuous learning cycle that records outcomes and feeds AdaptiveRouter",
            "pending_records": len(getattr(loop, '_pending_records', [])),
        }
        return json.dumps({"self_learning": stats}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def memory_hierarchical_stats() -> str:
    """Get HierarchicalMemory statistics — total items, tier distribution, capacities."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hierarchical_memory import get_memory
        mem = get_memory()
        return json.dumps({"hierarchical_memory": mem.get_stats()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def memory_hierarchical_store(content: str, tags: str = "", importance: float = 0.5,
                                    tier: str = "working", bypass_triage: bool = False) -> str:
    """Store an item in HierarchicalMemory. Tiers: working, episodic, semantic. Tags comma-separated.

    El triage descarta contenido sin densidad informativa (saludos, "ok") y PII.
    bypass_triage=True guarda igual (para hechos que el triage no reconoce).
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hierarchical_memory import MemoryTier, get_memory
        validos = [t.value for t in MemoryTier]
        if tier not in validos:
            return json.dumps({"stored": False,
                               "error": f"tier invalido: {tier!r}. Validos: {', '.join(validos)}"},
                              indent=2)
        mem = get_memory()
        tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
        item = mem.store(content, tags=tag_list, importance=importance, tier=tier,
                         bypass_triage=bool(bypass_triage))
        # store() devuelve None cuando el triage rechaza (trivial, duplicado o PII);
        # antes el .id explotaba con AttributeError.
        if item is None:
            return json.dumps({"stored": False,
                               "reason": ("rechazado por el triage de memoria (poco contenido "
                                          "informativo, duplicado, o PII/secretos). "
                                          "Usar bypass_triage=true para forzar.")},
                              indent=2, ensure_ascii=False)
        return json.dumps({"stored": True, "id": item.id, "tier": item.tier,
                           "importance": item.importance, "tags": item.tags}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def memory_hierarchical_search(query: str, tier: str = "", min_importance: float = 0.0, top_k: int = 10) -> str:
    """Search HierarchicalMemory across 3 tiers. Returns items with scores."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hierarchical_memory import get_memory
        mem = get_memory()
        tier_arg = tier if tier else None
        results = mem.search(query, tier=tier_arg, min_importance=min_importance, top_k=top_k)
        return json.dumps({
            "query": query,
            "results": [{"content": r.content[:200], "tier": r.tier, "score": round(r.decay_score, 3),
                         "tags": r.tags, "importance": r.importance}
                        for r in results],
            "count": len(results),
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def retrieval_search(query: str, top_k: int = 10) -> str:
    """Multi-signal retrieval: hybrid search combining semantic + keyword + entity matching.
    Uses RAG engine for vector search, observations FTS5 for keyword search, and regex for entities.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.multi_signal_retrieval import MultiSignalRetrieval

        # Build real callbacks from existing systems
        async def vector_search_fn(q: str, top_k: int = 10):
            """Use RAG engine for semantic vector search."""
            try:
                from src.core.rag_engine import RAGEngine
                rag = RAGEngine()
                results = rag.search(q, top_k=top_k)
                return [{"content": r.get("text", ""), "score": r.get("score", 0.8),
                         "source": r.get("source", "rag")} for r in results]
            except Exception:
                return []

        def keyword_search_fn(q: str, top_k: int = 10):
            """Use observations FTS5 for keyword search."""
            try:
                import sqlite3
                db_path = str(_MEMORY_DB)
                if not os.path.exists(db_path):
                    return []
                conn = sqlite3.connect(db_path)
                cursor = conn.cursor()
                # FTS5 search on observations
                try:
                    cursor.execute("""
                        SELECT content, 0.8 as score FROM observations
                        WHERE content MATCH ? LIMIT ?
                    """, (q[:100], top_k))
                    results = [{"content": row[0], "score": row[1], "source": "observations"}
                               for row in cursor.fetchall()]
                except Exception:
                    # Fallback: LIKE search
                    cursor.execute("""
                        SELECT content, 0.5 as score FROM observations
                        WHERE content LIKE ? LIMIT ?
                    """, (f"%{q[:50]}%", top_k))
                    results = [{"content": row[0], "score": row[1], "source": "observations"}
                               for row in cursor.fetchall()]
                conn.close()
                return results
            except Exception:
                return []

        retriever = MultiSignalRetrieval(
            vector_search_fn=vector_search_fn,
            keyword_search_fn=keyword_search_fn,
            entity_extractor=MultiSignalRetrieval.extract_entities,
        )

        results = await retriever.search(query, top_k=top_k)
        entities = MultiSignalRetrieval.extract_entities(query)

        return json.dumps({
            "query": query,
            "entities_found": entities,
            "results": [r.to_dict() for r in results],
            "count": len(results),
            "signals_used": ["vector", "keyword", "entity"],
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2, ensure_ascii=False)


# ============================================================
# Codebase Context (repomix + Tree-sitter compression)
# ============================================================


@mcp.tool()
async def codebase_context(
    scope: str = "",
    max_chars: int = 0,
) -> str:
    """Get compressed codebase dump using repomix + Tree-sitter structural compression.
    scope: paths to include (e.g. "src/core" or "src/core,src/brain"), empty = full project.
    max_chars: truncate to N chars (0 = no truncation).
    Use this to understand project structure before making changes.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.codebase_context import get_instance
        ctx = get_instance()
        dump = await ctx.get_context(scope=scope, max_chars=max_chars)
        return json.dumps({
            "scope": scope or "full",
            "chars": len(dump),
            "truncated": max_chars > 0 and len(dump) >= max_chars,
            "content": dump,
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def codebase_query(
    query: str,
    scope: str = "",
    max_results: int = 5,
    context_lines: int = 10,
) -> str:
    """Search codebase for relevant code sections matching a query.
    Uses cached repomix dump and scores sections by relevance.
    Returns top matching file sections with context around matches.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.codebase_context import get_instance
        ctx = get_instance()
        results = await ctx.query_context(
            query=query, scope=scope,
            max_results=max_results, context_lines=context_lines,
        )
        return json.dumps({
            "query": query,
            "scope": scope or "full",
            "content": results,
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


# ============================================================
# SandboxService — sandboxed code/command execution
# ============================================================


@mcp.tool()
async def sandbox_execute(
    code: str = "",
    command: str = "",
    language: str = "python",
    timeout: int = 30,
) -> str:
    """Execute code or shell commands in a sandboxed environment with timeout and concurrency limits.
    Pass `code` + `language` to run a code snippet (python/javascript/bash/go).
    Pass `command` to run a shell command directly.
    Returns output, errors, return code, and execution duration.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.sandbox_service import SandboxService, SandboxConfig
        config = SandboxConfig(timeout_seconds=timeout)
        svc = SandboxService(config)
        if command:
            result = await svc.run_command(command)
        elif code:
            result = await svc.run_code(code, language=language)
        else:
            return json.dumps({"error": "Provide either 'code' or 'command'"}, indent=2)
        return json.dumps({
            "success": result.success,
            "output": result.output,
            "error": result.error,
            "return_code": result.return_code,
            "duration_ms": round(result.duration_ms, 1),
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


# ============================================================
# DirectorNexus orchestration tools (multi-agent)
# Proxy to NEXUS API server at :9000
# ============================================================
NEXUS_API_BASE = nexus_config.get_nexus_url()


@mcp.tool()
async def classify_task(task: str, session_id: str = "") -> str:
    """Classify a task semantically to the best-suited gema/sub-director.
    Returns the routing decision (target, confidence, reasoning).
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            sub = (await client.get(f"{NEXUS_API_BASE}/api/director/sub-directors")).json()
            skills = (await client.get(f"{NEXUS_API_BASE}/api/skills/suggest",
                                       params={"context": task[:200]})).json()
        keywords_code = {"code", "function", "class", "bug", "fix", "refactor", "compile", "import", "def ", "return ", "async", "await", "typescript", "python", "rust", "go", "javascript", "api", "endpoint"}
        keywords_research = {"research", "study", "paper", "analyze", "compare", "what is", "explain", "how does", "investigate", "literature", "arxiv"}
        keywords_ops = {"deploy", "docker", "kubernetes", "ci/cd", "pipeline", "infrastructure", "terraform", "monitor", "log", "alert", "uptime"}
        keywords_voice = {"voice", "speech", "tts", "stt", "audio", "transcribe", "say", "speak", "tone"}
        tl = task.lower()
        scores = {"sub-director-code": 0, "sub-director-research": 0, "sub-director-ops": 0, "sub-director-voice": 0}
        for kw in keywords_code:
            if kw in tl: scores["sub-director-code"] += 1
        for kw in keywords_research:
            if kw in tl: scores["sub-director-research"] += 1
        for kw in keywords_ops:
            if kw in tl: scores["sub-director-ops"] += 1
        for kw in keywords_voice:
            if kw in tl: scores["sub-director-voice"] += 1
        target = max(scores, key=scores.get)
        if scores[target] == 0:
            target = "sub-director-code"
        total = sum(scores.values()) or 1
        confidence = round(scores[target] / total, 2)
        return json.dumps({
            "task": task,
            "session_id": session_id,
            "target": target,
            "confidence": confidence,
            "scores": scores,
            "available_sub_directors": list(sub.get("sub_directors", {}).keys()),
            "suggested_skills": [s["skill"] for s in skills.get("suggestions", [])[:3]],
            "reasoning": f"Best match: {target} (score={scores[target]}); fallback to code if no keywords matched."
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "target": "sub-director-code", "confidence": 0.0}, indent=2)


@mcp.tool()
async def execute_with_gema(task: str, target: str = "sub-director-code", session_id: str = "") -> str:
    """Execute a task via the NEXUS Director (full gema routing + hooks).
    target: sub-director-code|research|ops|voice | agent-zero | hermes | openclaw
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/director/execute",
                                  json={"task": task, "target": target, "session_id": session_id})
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "task": task, "target": target}, indent=2)


@mcp.tool()
async def mixture_of_agents(task: str, providers: str = "") -> str:
    """Run the same task in parallel across multiple providers/gemas,
    then judge + synthesize the best response.
    providers: comma-separated list (e.g. "sub-director-code,sub-director-research")
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        body = {"goal": task}
        if providers:
            body["providers"] = [p.strip() for p in providers.split(",") if p.strip()]
        async with httpx.AsyncClient(timeout=180) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/orchestrate", json=body)
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "task": task}, indent=2)


@mcp.tool()
async def parallel_execute(task: str, gemas: str = "code,analyst,security", timeout: int = 60) -> str:
    """Ejecutar tarea en paralelo con múltiples gemas especializadas.
    Cada gema analiza la tarea desde su perspectiva experta.
    gemas: lista separada por comas (code, analyst, security, architect, debugger, etc.)
    timeout: segundos máximo por gema
    Retorna resultados individuales de cada gema + síntesis final.
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        gema_list = [g.strip() for g in gemas.split(",") if g.strip()]
        body = {
            "task": task,
            "gemas": gema_list,
            "timeout": timeout,
            "synthesize": True,
        }
        async with httpx.AsyncClient(timeout=timeout + 30) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/teams/execute", json=body)
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "task": task, "gemas": gemas}, indent=2)


@mcp.tool()
async def deep_research(query: str, deep: bool = True) -> str:
    """Investigacion web profunda iterativa (estilo Odysseus/IterResearch).
    - deep=false: investigacion simple rapida (single-pass)
    - deep=true: loop iterativo Think->Search->Extract->Synthesize (hasta 8 rondas, ~5 min)
    Retorna reporte final con fuentes citadas.
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=360) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/scholar/research",
                                  json={"query": query, "deep": deep})
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "query": query}, indent=2)


@mcp.tool()
async def compare_models(prompt: str, model_a: str, model_b: str, is_blind: bool = True) -> str:
    """Comparar A/B dos modelos con el mismo prompt.
    - prompt: pregunta a enviar a ambos modelos
    - model_a, model_b: nombres de modelos a comparar
    - is_blind: modo ciego (identidades ocultas hasta votar)
    Retorna respuestas de ambos modelos + id para votar.
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/compare/start",
                                  json={"prompt": prompt, "model_a": model_a, "model_b": model_b, "is_blind": is_blind})
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "prompt": prompt}, indent=2)


@mcp.tool()
async def vote_comparison(comp_id: str, winner: str) -> str:
    """Votar en una comparacion A/B.
    - comp_id: id de la comparacion
    - winner: "left", "right", o "tie"
    Retorna identidades reveladas.
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/compare/{comp_id}/vote",
                                  json={"winner": winner})
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "comp_id": comp_id}, indent=2)


@mcp.tool()
async def gallery_transform(img_id: str, operation: str, **params) -> str:
    """Transformar imagen en la galeria.
    - img_id: id de la imagen
    - operation: resize, rotate, crop, flip, filter, brightness, contrast
    - params: parametros especificos de la operacion
    Retorna nueva imagen transformada.
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=60) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/gallery/transform",
                                  json={"img_id": img_id, "operation": operation, **params})
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "img_id": img_id}, indent=2)


@mcp.tool()
async def scan_hardware_remote(remote_host: str, ssh_port: str = "22") -> str:
    """Detectar hardware (GPU, RAM, disco) en una maquina remota via SSH.

    - remote_host: host SSH (user@ip o solo ip). Vacio/local = esta maquina.
    - ssh_port: puerto SSH (default 22)

    OJO: RAM y disco SIEMPRE son de la maquina local (el upstream los mide aca);
    del remoto solo puede venir la GPU. Si no se detecto GPU remota se devuelve
    ok=false en vez de un 200 con hardware local disfrazado de remoto.
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    host = (remote_host or "").strip()
    try:
        async with httpx.AsyncClient(timeout=45) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/cookbook/scan",
                                  json={"remote_host": host, "ssh_port": ssh_port})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e), "remote_host": host,
                           "upstream": NEXUS_API_BASE}, indent=2)
    try:
        data = r.json()
    except Exception:
        return json.dumps({"ok": False, "status_code": r.status_code, "remote_host": host,
                           "error": "el upstream no devolvio JSON",
                           "body": (r.text or "")[:400], "upstream": NEXUS_API_BASE},
                          indent=2, ensure_ascii=False)
    if r.status_code != 200 or not isinstance(data, dict):
        return json.dumps({"ok": False, "status_code": r.status_code, "remote_host": host,
                           "result": data, "upstream": NEXUS_API_BASE},
                          indent=2, ensure_ascii=False)

    hw = data.get("hardware") or {}
    detectado = (hw.get("remote_host") or "").strip()
    es_local = detectado in ("", "localhost", "127.0.0.1", "::1")
    if host and not es_local and not hw.get("gpu_name") and not hw.get("vram_gb"):
        # El endpoint devuelve 200 igual cuando el SSH falla: sin GPU detectada
        # el "escaneo remoto" era la maquina local con la etiqueta del host.
        return json.dumps({
            "ok": False, "remote_host": host, "status_code": r.status_code,
            "error": ("no se pudo detectar GPU en el host remoto (SSH inaccesible o sin "
                      "nvidia-smi). El upstream devolvio hardware LOCAL: RAM y disco no "
                      "son del remoto."),
            "local_hardware": hw, "recommendations": data.get("recommended") or [],
            "upstream": NEXUS_API_BASE,
        }, indent=2, ensure_ascii=False)
    return json.dumps({"ok": True, "status_code": r.status_code, "result": data},
                      indent=2, ensure_ascii=False)


@mcp.tool()
async def detect_prompt_injection(text: str) -> str:
    """Detectar intentos de prompt injection en texto.
    Analiza contenido en busca de patrones sospechosos como:
    - Instrucciones para ignorar reglas
    - Intentos de roleplay no autorizado
    - Bypass de seguridad
    Retorna nivel de riesgo y patrones encontrados.
    """
    from src.core.prompt_security import is_suspicious_content
    result = is_suspicious_content(text)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def wrap_untrusted_content(label: str, content: str) -> str:
    """Envolver contenido no confiable en sandbox seguro.
    Protege contra prompt injection cuando se procesa:
    - Contenido web
    - Emails
    - Salida de herramientas
    - Documentos externos
    Retorna mensaje LLM seguro.
    """
    from src.core.prompt_security import untrusted_context_message
    result = untrusted_context_message(label, content)
    return json.dumps(result, indent=2, ensure_ascii=False)


@mcp.tool()
async def analyze_conversation_topics(messages_json: str) -> str:
    """Analizar topics en una conversacion.
    Detecta topics por keywords y retorna frecuencias + ejemplos.
    Messages JSON: [{"role": "user", "content": "..."}, ...]
    """
    from src.core.topic_analyzer import analyze_topics, format_topic_report
    try:
        messages = json.loads(messages_json)
        analysis = analyze_topics(messages)
        report = format_topic_report(analysis)
        return json.dumps({"analysis": analysis, "report": report}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2, ensure_ascii=False)


@mcp.tool()
async def toggle_incognito(session_id: str) -> str:
    """Toggle modo incognito para una sesion (sin persistencia, sin memoria)."""
    from src.core.incognito import IncognitoManager
    mgr = IncognitoManager()
    state = mgr.toggle(session_id)
    return json.dumps({"incognito": state, "session_id": session_id}, indent=2)


@mcp.tool()
async def create_scheduled_task(name: str, prompt: str, schedule_type: str = "daily", scheduled_time: str = "09:00", gema: str = "auto") -> str:
    """Crear tarea programada (cron/evento). Tipos: once, daily, weekly, cron."""
    from src.core.scheduler import TaskScheduler
    scheduler = TaskScheduler()
    task = scheduler.add_task({
        "name": name, "prompt": prompt, "schedule_type": schedule_type,
        "scheduled_time": scheduled_time, "gema": gema,
    })
    return json.dumps({"task": task.to_dict()}, indent=2, ensure_ascii=False)


@mcp.tool()
async def create_note(title: str, content: str, due_date: str = "", label: str = "") -> str:
    """Crear nota con recordatorio opcional."""
    from src.core.notes_calendar import NotesManager
    mgr = NotesManager()
    note = mgr.create({"title": title, "content": content, "due_date": due_date, "label": label})
    return json.dumps({"note": note.to_dict()}, indent=2, ensure_ascii=False)


@mcp.tool()
async def create_calendar_event(summary: str, start: str, end: str = "", description: str = "", event_type: str = "other") -> str:
    """Crear evento de calendario."""
    from src.core.notes_calendar import CalendarManager
    mgr = CalendarManager()
    event = mgr.create_event({"summary": summary, "start": start, "end": end, "description": description, "event_type": event_type})
    return json.dumps({"event": event.to_dict()}, indent=2, ensure_ascii=False)


@mcp.tool()
async def create_persona(name: str, personality: str, model: str = "qwen2.5-coder:7b") -> str:
    """Crear persona custom con system prompt propio."""
    from src.core.personas import PersonaManager
    mgr = PersonaManager()
    persona = mgr.create({"name": name, "personality": personality, "model": model})
    return json.dumps({"persona": persona.to_dict()}, indent=2, ensure_ascii=False)


@mcp.tool()
async def search_library(type_filter: str = "", search: str = "") -> str:
    """Buscar en la libreria unificada (chats, docs, gallery, notes, research)."""
    from src.core.library import LibraryManager
    lib = LibraryManager()
    items = lib.get_all(type_filter=type_filter, search=search)
    return json.dumps({"items": items[:50], "stats": lib.get_stats()}, indent=2, ensure_ascii=False)


@mcp.tool()
async def set_theme(name: str = "", pattern: str = "") -> str:
    """Cambiar tema o patron animado (rain, constellations, synapse, sparkles, embers)."""
    from src.core.themes import ThemeManager
    mgr = ThemeManager()
    if name:
        mgr.set_theme(name)
    if pattern:
        mgr.set_pattern(pattern)
    return json.dumps({"active": mgr.active_theme, "pattern": mgr.pattern}, indent=2, ensure_ascii=False)


@mcp.tool()
async def run_harness(goal: str, max_iterations: int = 5) -> str:
    """Run full harness engineering pipeline: decompose → execute → synthesize → verify.
    Uses NEXUS orchestrate endpoint with extended iterations.
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/orchestrate",
                                  json={"goal": goal, "max_iterations": max_iterations, "harness": True})
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "goal": goal}, indent=2)


@mcp.tool()
async def run_agent_loop(task: str, context: str = "", max_iterations: int = 10) -> str:
    """Run TDAO agent loop for multi-step autonomous task execution.
    Returns a trace of each iteration step.
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=300) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/director/execute",
                                  json={"task": task, "context": context,
                                        "loop": True, "max_iterations": max_iterations})
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "task": task}, indent=2)


@mcp.tool()
async def get_director_status() -> str:
    """Get the complete DirectorNexus status: sub-directors, external agents, orchestrate state, learning."""
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    out = {"timestamp": datetime.now().isoformat()}
    endpoints = [
        ("sub_directors", "/api/director/sub-directors"),
        ("external_agents", "/api/director/external-agents"),
        ("orchestrate", "/api/orchestrate/status"),
        ("decision_engine", "/api/director/decision-engine"),
        ("learning", "/api/director/learning"),
    ]
    async with httpx.AsyncClient(timeout=10) as client:
        for key, path in endpoints:
            try:
                r = await client.get(f"{NEXUS_API_BASE}{path}")
                out[key] = r.json() if r.status_code == 200 else {"error": f"HTTP {r.status_code}"}
            except Exception as e:
                out[key] = {"error": str(e)}
    return json.dumps(out, indent=2, ensure_ascii=False)


@mcp.tool()
async def change_project(project: str) -> str:
    """Switch the active NEXUS project context. Affects routing, memory scope, and skills."""
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(f"{NEXUS_API_BASE}/api/projects/activate",
                                  json={"project": project})
            if r.status_code == 404:
                ps = (await client.get(f"{NEXUS_API_BASE}/api/projects")).json()
                return json.dumps({"status": "error", "message": f"Project '{project}' not found",
                                   "available": [p.get("id", p.get("name", str(p))) for p in ps.get("projects", [])]},
                                  indent=2, ensure_ascii=False)
            return json.dumps({"status_code": r.status_code, "result": r.json()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "project": project}, indent=2)


@mcp.tool()
async def get_relevant_skills(task: str, top_k: int = 3) -> str:
    """Progressive skill matching: returns the most relevant skills for a task
    based on procedural memory (success rate, recency, match).
    """
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(f"{NEXUS_API_BASE}/api/skills/suggest",
                                 params={"context": task[:500]})
            data = r.json() if r.status_code == 200 else {}
            suggestions = data.get("suggestions", [])[:top_k]
            return json.dumps({
                "task": task,
                "top_k": top_k,
                "skills": [{"name": s["skill"],
                            "success_rate": s.get("success_rate", 0),
                            "picks": s.get("picks", 0),
                            "successes": s.get("successes", 0),
                            "last_used": s.get("last_ts", "never"),
                            "match": s.get("match", "unknown")}
                           for s in suggestions],
                "count": len(suggestions)
            }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e), "task": task}, indent=2)


@mcp.tool()
async def agency_search(query: str, top_k: int = 5, division: str = "") -> str:
    """Search the agency-agents catalog (266 specialized AI agents) by query.
    Returns matching agents with name, emoji, description, division, and relevance score.
    Use this when the user needs a specialized expert that goes beyond SuperNEXUS gemas."""
    try:
        from src.core.agency_catalog import get_catalog
        catalog = get_catalog()
        results = catalog.search(query, top_k=top_k, division=division)
        return json.dumps({
            "query": query,
            "results": [{"name": r["name"], "emoji": r.get("emoji", ""),
                         "description": r.get("description", ""),
                         "division": r.get("division", ""),
                         "score": round(r.get("score", 0), 2)}
                        for r in results],
            "total_in_catalog": len(catalog._catalog),
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def agency_load_prompt(agent_name: str) -> str:
    """Load the full system prompt for a specific agency-agents agent by name.
    Returns the complete agent prompt with identity, workflows, deliverables, metrics, and constraints."""
    try:
        from src.core.agency_catalog import get_catalog
        catalog = get_catalog()
        prompt = catalog.load_prompt(agent_name)
        if prompt:
            return json.dumps({"agent": agent_name, "prompt": prompt[:8000], "truncated": len(prompt) > 8000}, indent=2, ensure_ascii=False)
        return json.dumps({"error": f"Agent '{agent_name}' not found"}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def agency_list_divisions() -> str:
    """List all agency-agents divisions with agent counts."""
    try:
        from src.core.agency_catalog import get_catalog
        catalog = get_catalog()
        divisions = catalog.list_divisions()
        stats = catalog.get_stats()
        return json.dumps({"divisions": divisions, "stats": stats}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def doctor_diagnose() -> str:
    """Run self-diagnostics across NEXUS components: API server, ollama, brain, MCPs, peers."""
    if not HTTPX_AVAILABLE:
        return json.dumps({"error": "httpx not installed"}, indent=2)
    checks = {}
    async with httpx.AsyncClient(timeout=5) as client:
        try:
            r = await client.get(f"{NEXUS_API_BASE}/api/status")
            checks["api_server"] = {"status": "healthy" if r.status_code == 200 else "degraded",
                                    "code": r.status_code}
        except Exception as e:
            checks["api_server"] = {"status": "offline", "error": str(e)}
        try:
            r = await client.get("http://localhost:11434/api/tags")
            models = r.json().get("models", []) if r.status_code == 200 else []
            checks["ollama"] = {"status": "healthy" if r.status_code == 200 else "degraded",
                                 "models": len(models)}
        except Exception as e:
            checks["ollama"] = {"status": "offline", "error": str(e)}
        try:
            r = await client.get(f"{NEXUS_API_BASE}/api/director/sub-directors")
            data = r.json() if r.status_code == 200 else {}
            sd = data.get("sub_directors", {})
            checks["director_sub_directors"] = {
                "status": "healthy" if sd else "degraded",
                "count": len(sd),
                "domains": list(sd.keys())
            }
        except Exception as e:
            checks["director_sub_directors"] = {"status": "offline", "error": str(e)}
        try:
            r = await client.get(f"{NEXUS_API_BASE}/api/director/external-agents")
            data = r.json() if r.status_code == 200 else {}
            ea = data.get("external_agents", data)
            checks["external_agents"] = {"status": "reachable", "data": str(ea)[:200]}
        except Exception as e:
            checks["external_agents"] = {"status": "offline", "error": str(e)}
    overall = "healthy"
    for c in checks.values():
        if c.get("status") in ("offline", "degraded"):
            overall = "degraded"
            break
    return json.dumps({"overall": overall, "timestamp": datetime.now().isoformat(),
                        "checks": checks}, indent=2, ensure_ascii=False)


@mcp.tool()
async def system_security_scan() -> str:
    """Run a security scan of the operating system. Checks certificates, Windows Defender, activators/KMS, tunnels, proxies, and Microsoft account integrity. Returns a severity-ranked report."""
    try:
        from src.security.system_scanner import SystemScanner
    except ImportError:
        return json.dumps({"error": "system_scanner module not available - run from nexus-v3 project root"}, indent=2)

    scanner = SystemScanner(emit_events=False)
    report = scanner.run_full_scan()
    return json.dumps({
        "summary": report.summary(),
        "timestamp": report.timestamp,
        "hostname": report.hostname,
        "critical": report.critical_count,
        "high": report.high_count,
        "medium": report.medium_count,
        "total_findings": len(report.findings),
        "findings": [
            {
                "severity": f.severity.value,
                "category": f.category,
                "title": f.title,
                "description": f.description,
                "remediation": f.remediation,
                "evidence": f.evidence,
            }
            for f in report.findings
        ],
    }, indent=2, ensure_ascii=False)


# ---- AGENT COMPUTER USE (agent-cu) ----

import subprocess

_AGENT_CU_BINARY: str = ""
_NODEJS_WRAPPER: str = ""


def _resolve_agent_cu() -> str:
    """Find the agent-cu binary or node wrapper. Portable: PATH, npm root -g, then relative to project."""
    global _AGENT_CU_BINARY, _NODEJS_WRAPPER
    if _AGENT_CU_BINARY:
        return _AGENT_CU_BINARY

    # 1) Try PATH (most portable - works if npm -g installed or in PATH)
    path_binary = shutil.which("agent-cu")
    if path_binary:
        _AGENT_CU_BINARY = path_binary
        return path_binary

    # 2) Try npm root -g to find global install location
    try:
        npm_root = subprocess.check_output(
            [sys.executable, "-m", "npm", "root", "-g"],
            text=True, stderr=subprocess.DEVNULL, timeout=10
        ).strip()
        if npm_root and os.path.isdir(npm_root):
            native = os.path.join(npm_root, "agent-cu", "bin", "agent-computer-use-windows-x64.exe")
            if os.path.isfile(native):
                _AGENT_CU_BINARY = native
                return native
    except Exception:
        pass

    # 3) Try common npm global paths (Windows, Linux, macOS)
    candidates = [
        os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "agent-cu", "bin",
                     "agent-computer-use-windows-x64.exe"),
        os.path.join(os.environ.get("LOCALAPPDATA", ""), "npm", "node_modules", "agent-cu", "bin",
                     "agent-computer-use-windows-x64.exe"),
        os.path.expanduser("~/.npm-global/lib/node_modules/agent-cu/bin/"
                           "agent-computer-use-windows-x64.exe"),
        os.path.expanduser("~/npm/lib/node_modules/agent-cu/bin/"
                           "agent-computer-use-windows-x64.exe"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            _AGENT_CU_BINARY = c
            return c

    # 4) Try relative to project (local npm install)
    project_local = os.path.join(os.path.dirname(__file__), "..", "..", "node_modules",
                                 "agent-cu", "bin", "agent-computer-use-windows-x64.exe")
    if os.path.isfile(project_local):
        _AGENT_CU_BINARY = project_local
        return project_local

    # 5) Auto-install if not found anywhere
    try:
        logger.info("agent-cu not found. Attempting auto-install: npm install -g agent-cu")
        subprocess.run(
            ["npm", "install", "-g", "agent-cu"],
            check=True, capture_output=True, text=True, timeout=120,
        )
        # Retry resolution after install
        path_binary = shutil.which("agent-cu")
        if path_binary:
            _AGENT_CU_BINARY = path_binary
            logger.info(f"agent-cu auto-installed at {path_binary}")
            return path_binary
    except Exception as e:
        logger.warning(f"agent-cu auto-install failed: {e}")

    # 6) Fallback: Node.js wrapper via agent-cu.js
    wrapper_paths = [
        os.path.join(os.environ.get("APPDATA", ""), "npm", "node_modules", "agent-cu", "bin", "agent-cu.js"),
        os.path.expanduser("~/.npm-global/lib/node_modules/agent-cu/bin/agent-cu.js"),
    ]
    node_exe = shutil.which("node") or os.path.join(
        os.environ.get("ProgramFiles", ""), "nodejs", "node.exe"
    )
    for wp in wrapper_paths:
        if os.path.isfile(wp) and (os.path.isfile(node_exe) or shutil.which("node")):
            _NODEJS_WRAPPER = wp
            return node_exe
    return ""


@mcp.tool()
async def agent_cu_execute(
    command: str,
    app: str = "",
    args: str = "",
) -> str:
    """Execute agent-cu commands for desktop automation (click, type, snapshot, scroll, etc).
    Zero token cost - uses accessibility API, not vision.
    Supported commands: apps, snapshot, click, type, key, scroll, drag, text, find, open, wait-for, screenshot.

    Args:
        command: agent-cu subcommand (e.g. 'snapshot', 'click', 'type', 'apps', 'text', 'scroll')
        app: Target application name (e.g. 'Calculator', 'Music', 'Chrome')
        args: Additional arguments for the command (e.g. '@e5' for click ref, 'hello' for type)

    Returns:
        JSON string with the command output
    """
    binary = _resolve_agent_cu()
    if not binary:
        return json.dumps({"error": "agent-cu binary not found. Run: npm install -g agent-cu"})

    try:
        if _AGENT_CU_BINARY:
            cmd_parts = [binary, command]
        elif _NODEJS_WRAPPER and binary:
            cmd_parts = [binary, _NODEJS_WRAPPER, command]
        else:
            cmd_parts = ["agent-cu", command]
        if app:
            cmd_parts.extend(["-a", app])
        if args:
            cmd_parts.extend(args.split())

        result = await asyncio.to_thread(
            subprocess.run,
            cmd_parts,
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        output = result.stdout or result.stderr
        if result.returncode != 0:
            return json.dumps({"error": output.strip()[:500], "returncode": result.returncode})
        return output.strip()
    except subprocess.TimeoutExpired:
        return json.dumps({"error": "agent-cu command timed out after 30s"})
    except subprocess.CalledProcessError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        return json.dumps({"error": str(e)})


@mcp.tool()
async def dag_decompose(goal: str, use_llm: bool = True) -> str:
    """Decompose a natural language goal into a task DAG with dependencies.

    FASE 2.2: LLM-based decomposition for complex goals (>15 words).
    Falls back to pattern-based for simple goals.
    Returns task graph with assignees and dependency edges.
    """
    try:
        from src.core.dag_coordinator import DAGCoordinator
        dc = DAGCoordinator()
        # decompose_goal() sincronico hacia run_until_complete() adentro de este
        # mismo loop -> "This event loop is already running" para cualquier
        # objetivo complejo (justo el caso que usa LLM).
        dag = await dc.decompose_goal_async(goal, use_llm=use_llm)
        tasks = []
        for n in dag.nodes:
            tasks.append({
                "id": n.id, "title": n.title, "description": n.description,
                "assignee": n.assignee, "depends_on": n.depends_on,
            })
        return json.dumps({
            "dag_id": dag.id, "goal": dag.goal, "task_count": len(tasks),
            "tasks": tasks, "method": dag.method,
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def prompt_review(prompt: str) -> str:
    """Review a prompt (FASE 2.3, DSPy-inspired): quality score (0-100), savings %, issues and suggestions.
    Returns a preview of the optimized text (first 500 chars). For the compressed full text use optimize_prompt.
    """
    try:
        from src.core.prompt_assembler import PromptAssembler
        pa = PromptAssembler()
        result = pa.optimize_prompt(prompt)
        # Don't return the full optimized text in the response (too large)
        return json.dumps({
            "original_length": result["original_length"],
            "optimized_length": result["optimized_length"],
            "savings_percent": result["savings_percent"],
            "quality_score": result["quality_score"],
            "issues": result["issues"],
            "suggestions": result["suggestions"],
            "optimized_preview": result["optimized"][:500] + "..." if len(result["optimized"]) > 500 else result["optimized"],
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def medusa_scan_targeted(content: str, categories: str = "") -> str:
    """Scan content for security threats using expanded MEDUSA patterns.

    FASE 2.1: Expanded from 197 to 282 patterns across 58 categories.
    New categories: ai-prompt-injection, agent-attack, skill-injection,
    memory-attack, supply-chain, model-attack, cryptojacking.
    """
    try:
        from src.security.medusa_scan import scan_text
        hits = scan_text(content)
        results = []
        for h in hits:
            results.append({
                "pattern_id": h.pattern_id,
                "severity": h.severity,
                "category": h.category,
                "excerpt": h.excerpt,
                "line": h.line,
            })
        # Filter by categories if specified
        if categories:
            cat_set = set(c.strip().lower() for c in categories.split(","))
            results = [r for r in results if r.get("category", "").lower() in cat_set]
        return json.dumps({
            "detections": results,
            "count": len(results),
            "severity_counts": {
                "high": sum(1 for r in results if r.get("severity") == "high"),
                "medium": sum(1 for r in results if r.get("severity") == "medium"),
                "low": sum(1 for r in results if r.get("severity") == "low"),
            },
        }, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def skill_marketplace_stats() -> str:
    """Get skill catalog statistics: total count, category distribution, and trending tags.

    FASE 3.3: Skill marketplace — provides visibility into the skill ecosystem
    for optimizing skill loading and discovering popular patterns.
    """
    catalog = _get_catalog()

    # Category distribution
    categories = {}
    all_tags = {}
    for skill in catalog:
        cat = skill.get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1
        # Count tags
        for tag in skill.get("tags", "").split(","):
            tag = tag.strip().lower()
            if tag:
                all_tags[tag] = all_tags.get(tag, 0) + 1

    # Top trending tags (most frequent)
    trending_tags = sorted(all_tags.items(), key=lambda x: x[1], reverse=True)[:20]

    # Skills per category
    cat_stats = [{"category": c, "count": n} for c, n in sorted(categories.items(), key=lambda x: x[1], reverse=True)]

    return json.dumps({
        "total_skills": len(catalog),
        "categories": cat_stats,
        "trending_tags": [{"tag": t, "count": n} for t, n in trending_tags],
        "unique_tags": len(all_tags),
    }, indent=2, ensure_ascii=False)


# ============================================================
# Hermes-style Autonomous Tools
# ============================================================

@mcp.tool()
async def execute_code(code: str) -> str:
    """Ejecuta un snippet de Python en un subproceso aislado (timeout 30 s).

    Imports acotados (sin os/sys/subprocess/socket/...), sin dunders y sin acceso
    a las tools del server. El snippet calcula y devuelve `result`.
    """
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hermes_tools import get_execute_code_engine
        result = await get_execute_code_engine().execute(code)
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def cron_add(name: str, schedule: str, prompt: str = "",
                   script: str = "", deliver_to: str = "none",
                   no_agent: bool = False) -> str:
    """Add a scheduled cron job. 
    Schedule examples: 'every 5m', 'every 1h', 'daily 08:00', 'cron: */5 * * * *'
    Deliver to: 'nexus_message', 'telegram', 'discord', 'none'
    If no_agent=True and script is set, runs script without LLM."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hermes_tools import get_hermes_cron
        cron = get_hermes_cron()
        job = cron.add_job(name, schedule, prompt=prompt, script=script,
                          deliver_to=deliver_to, no_agent=no_agent)
        return json.dumps({"id": job.id, "name": job.name, "schedule": job.schedule,
                           "next_run": job.next_run}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def cron_list() -> str:
    """List all scheduled cron jobs."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hermes_tools import get_hermes_cron
        cron = get_hermes_cron()
        return json.dumps({"jobs": cron.list_jobs()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def cron_remove(job_id: str) -> str:
    """Remove a cron job by ID."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hermes_tools import get_hermes_cron
        cron = get_hermes_cron()
        removed = cron.remove_job(job_id)
        return json.dumps({"removed": removed, "job_id": job_id}, indent=2)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def memory_nudge() -> str:
    """Trigger a memory nudge cycle — compresses memory, extracts patterns.
    Auto-triggers every N prompts, but can be called manually."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hermes_tools import get_memory_nudge
        from src.core.hierarchical_memory import get_memory
        nudge = get_memory_nudge()
        mem = get_memory()
        result = await nudge.nudge(hierarchical_memory=mem)
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def skill_auto_create(task_description: str) -> str:
    """Auto-create a skill from recorded tool calls.
    Call this after completing a complex task (5+ tool calls) to save it as a reusable skill."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hermes_tools import get_skill_auto_creator
        creator = get_skill_auto_creator()
        result = await creator.create_skill(task_description)
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def skill_auto_list() -> str:
    """List all auto-created skills."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hermes_tools import get_skill_auto_creator
        creator = get_skill_auto_creator()
        return json.dumps({"skills": creator.list_skills()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def image_generate(prompt: str, width: int = 1024, height: int = 1024,
                         style: str = "default") -> str:
    """Generate an AI image from a text prompt.
    Uses ComfyUI if available, otherwise returns instructions."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hermes_tools import get_image_generator
        gen = get_image_generator()
        result = await gen.generate(prompt, width=width, height=height, style=style)
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


@mcp.tool()
async def tts_speak(text: str, voice: str = "") -> str:
    """Convert text to speech using Piper TTS.
    Returns audio file path if successful."""
    try:
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
        from src.core.hermes_tools import get_tts_speaker
        speaker = get_tts_speaker()
        result = await speaker.speak(text, voice=voice)
        return json.dumps(result, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": str(e)}, indent=2)


# ---- SESSION REMINDERS (S3, session-local, prefijo reminder_*) ----
# Nota colisiones: los tools de cron global son `create_scheduled_task` y
# `cron_add/list/remove`; estos usan prefijo `reminder_*` y el store
# session-local `SessionReminderStore` (NO tocan scheduler.py).

_REMINDER_DEFAULTS = {"min_every_s": 300, "max_active": 50}


def _reminder_config() -> dict:
    """Lee sección `reminders` de config.yaml (best-effort, defaults si falla)."""
    try:
        from src.core.config import load_config

        cfg = load_config()
        r = cfg.get("reminders") or {}
        return {
            "min_every_s": int(r.get("min_every_s", _REMINDER_DEFAULTS["min_every_s"])),
            "max_active": int(r.get("max_active", _REMINDER_DEFAULTS["max_active"])),
        }
    except Exception:
        return dict(_REMINDER_DEFAULTS)


def _reminder_store(session_id: str):
    from src.core.session_reminders import SessionReminderStore

    if not session_id or not str(session_id).strip():
        raise ValueError("session_id requerido")
    return SessionReminderStore(session_id=session_id)


@mcp.tool()
async def reminder_create(
    session_id: str,
    message: str,
    kind: str = "after",
    delay_s: float = 0,
    timestamp: float = 0,
    interval_s: float = 0,
) -> str:
    """Create a session-local reminder (after/at/every). Session-local: never touches the global cron scheduler.

    kind=after → delay_s (s); kind=at → timestamp (epoch); kind=every → interval_s (s, >= reminders.min_every_s).
    Enforces reminders.max_active active reminders per session.
    """
    try:
        from src.core.session_reminders import MIN_EVERY_SECONDS, ReminderError

        cfg = _reminder_config()
        min_every = max(MIN_EVERY_SECONDS, cfg["min_every_s"])
        if kind not in ("after", "at", "every"):
            return json.dumps({"error": f"kind debe ser after|at|every (recibido {kind!r})"}, indent=2)
        store = _reminder_store(session_id)
        if len(store.list(active_only=True)) >= cfg["max_active"]:
            return json.dumps(
                {"error": f"max_active={cfg['max_active']} alcanzado en sesion {session_id!r}"},
                indent=2,
            )
        if kind == "after":
            r = store.add_after(delay_s, message)
        elif kind == "at":
            r = store.add_at(timestamp, message)
        else:
            if interval_s < min_every:
                return json.dumps(
                    {"error": f"every minimo {min_every}s (recibido {interval_s})"},
                    indent=2,
                )
            r = store.add_every(interval_s, message)
        return json.dumps({"created": True, "reminder": r.to_dict()}, indent=2, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, indent=2)


@mcp.tool()
async def reminder_list(session_id: str, active_only: bool = True) -> str:
    """List session-local reminders with scheduled/overdue statuses."""
    try:
        store = _reminder_store(session_id)
        reminders = [r.to_dict() for r in store.list(active_only=active_only)]
        return json.dumps(
            {"session_id": session_id, "reminders": reminders,
             "count": len(reminders), "statuses": store.statuses()},
            indent=2, ensure_ascii=False,
        )
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, indent=2)


@mcp.tool()
async def reminder_delete(session_id: str, reminder_id: str) -> str:
    """Delete (cancel) a session-local reminder. IDs never reused."""
    try:
        store = _reminder_store(session_id)
        removed = store.cancel(reminder_id)
        return json.dumps(
            {"removed": removed, "reminder_id": reminder_id, "session_id": session_id},
            indent=2,
        )
    except Exception as e:
        return json.dumps({"error": f"{type(e).__name__}: {e}"}, indent=2)


# ============================================================
# Entry point for MCP Clients (Claude Desktop, Gemini, etc.)
# ============================================================
if __name__ == "__main__":
    mcp.run()


