"""Genera brain/seed/nexus-seed.nexuskg.json (conocimiento base del sistema).

El bundle se versiona en git y el bootstrap lo importa en el grafo local.
Es conocimiento del PROPIO nexus-v3 (arquitectura, uso, troubleshooting), no
datos de una PC: sirve en cualquier maquina.

Uso:  python scripts/make_seed_bundle.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.core.knowledge_bundle import SCHEMA, _canonical_hash  # noqa: E402

OUT = ROOT / "brain" / "seed" / "nexus-seed.nexuskg.json"

NODES = [
    ("que-es", "que-es-nexus-v3", "Que es nexus-v3",
     "nexus-v3 es un harness de agentes autonomos con interfaz Electron + React y backend "
     "Python (FastAPI). Corre un loop TDAO (Think-Decide-Act-Observe) con tools, scope jail, "
     "aprobaciones, checkpoints, compactacion de contexto, sesiones con FTS5, grafo de "
     "conocimiento (learn/recall), RAG, sub-agentes y MCP en los dos sentidos: cliente de "
     "servidores MCP externos y servidor MCP para otros clientes.",
     ["arquitectura", "overview"], 9),
    ("instalacion-rapida", "instalacion-rapida", "Instalacion en un clic",
     "En Windows: doble clic en INSTALAR.bat (raiz del repo). El bootstrap verifica git/node/"
     "python, crea .venv-ft314, instala requirements, ejecuta npm install en la raiz y en ui/, "
     "compila electron y la UI, crea el grafo e importa la semilla, instala los MCP del bundle "
     "y corre la verificacion final. Si algo falla, el log queda en INSTALAR-LOG.txt con el "
     "paso exacto. No requiere pasos manuales ni permisos de administrador.",
     ["instalacion", "bootstrap"], 10),
    ("instalacion-manual", "instalacion-manual", "Instalacion manual (equivalente)",
     "python -m venv .venv-ft314; .venv-ft314\\Scripts\\python.exe -m pip install -r "
     "requirements.txt; npm install; npm install --prefix ui; npm run build (compila electron "
     "y ui); .venv-ft314\\Scripts\\python.exe scripts\\bootstrap_brain.py; "
     ".venv-ft314\\Scripts\\python.exe scripts\\setup_mcp.py --fix; "
     ".venv-ft314\\Scripts\\python.exe scripts\\verify_install.py. "
     "En Linux/macOS el venv es .venv-ft314/bin/python.",
     ["instalacion", "manual"], 8),
    ("estructura", "estructura-del-repo", "Estructura del repositorio",
     "src/api/server.py: FastAPI con todos los endpoints y el registry de tools. src/core: "
     "harness (agent_loop, tool_registry, scope, approvals, checkpoints, compactacion, "
     "sesiones, grafo). src/providers: ollama, nube BYOK, llama.cpp, CLIs. src/tools: tools "
     "builtin y de busqueda. src/mcp_server.py: servidor MCP entrante (stdio). ui/: React+Vite. "
     "electron/: main+preload. config/config.yaml: toda la configuracion. brain/: grafo y RAG. "
     "scripts/: bootstrap, setup MCP, verificacion, diagnostico.",
     ["arquitectura", "estructura"], 8),
    ("arranque", "arranque-y-puertos", "Arranque y puertos",
     "La app se abre con scripts\\nexus-v3-launch.vbs (acceso directo) o con npm start. Electron "
     "levanta el backend Python en un puerto libre (efimero) y sirve ui/dist. El log del backend "
     "queda en data/backend.log. En modo dev el backend usa :8765 y Vite :5174 (npm run dev). "
     "Ollama :11434. El lanzador compila electron y la UI si faltan los artefactos.",
     ["arranque", "puertos"], 7),
    ("brain", "brain-y-rutas", "Cerebro local y NEXUS_V3_BRAIN",
     "El cerebro vive en ./brain y se puede mover con NEXUS_V3_BRAIN (variable de entorno) o "
     "brain.path en config/config.yaml. Archivos: knowledge_graph.db (grafo OPCODE con FTS5), "
     "rag_index.db (indice RAG, se genera en runtime), session.db, event_store.db. "
     "NO usar NEXUS_BRAIN: esa variable pertenece a la instalación previa y enlazaria cerebros de "
     "proyectos distintos. Los .db no se versionan: viajan como bundle .nexuskg.json.",
     ["brain", "config"], 9),
    ("grafo-bundles", "grafo-bundles", "Exportar e importar conocimiento",
     "El grafo se transfiere con bundles .nexuskg.json (schema nexus.knowledge.bundle/v1). "
     "Exportar: GET /api/knowledge/export (o el boton de la UI). Importar: "
     "POST /api/knowledge/import con dry_run=true por defecto; el merge es por topic_key, no "
     "duplica y archiva revision al actualizar. La semilla del sistema esta en "
     "brain/seed/nexus-seed.nexuskg.json y se importa con scripts/bootstrap_brain.py. "
     "Nunca copies knowledge_graph.db entre maquinas: los ids locales no son estables.",
     ["brain", "bundle", "grafo"], 8),
    ("mcp-sistema", "mcp-sistema", "Sistema MCP de nexus-v3",
     "nexus-v3 es cliente y servidor MCP. Cliente: src/core/mcp_client.py lanza servidores "
     "stdio del catalogo data/mcp_bundle/manifest.json de forma just-in-time. Servidor: "
     "python -m src.mcp_server expone 41 tools por stdio (cwd = raiz del repo). Instalar y "
     "verificar el catalogo: python scripts/setup_mcp.py --fix (deja data/mcp_bundle/"
     "status.json). Config para un cliente: python scripts/setup_mcp.py --emit-client.",
     ["mcp", "setup"], 10),
    ("mcp-aprobacion", "mcp-aprobacion", "Aprobaciones de MCP",
     "Cada llamada mcp_call pasa por el gate de aprobacion. Si el par server/tool esta en "
     "approval.mcp_read_only (config/config.yaml) o en el campo read_only del manifest, se "
     "ejecuta sin preguntar. Si no, en la UI se pide aprobacion; en contextos sin canal "
     "(servidor MCP entrante, subagentes) falla cerrado con un mensaje que indica que patron "
     "agregar. Para habilitar una tool: agregar 'server/tool' (admite wildcards) a "
     "approval.mcp_read_only.",
     ["mcp", "aprobacion", "seguridad"], 9),
    ("mcp-uvx-roto", "mcp-uvx-roto", "uvx roto o ausente",
     "Varios MCP del catalogo usan uvx (computer-control, serena, mcp-local-rag). Si uv/uvx "
     "del sistema falla (en Windows es tipico 'os error 448'), el bootstrap instala un uv "
     "propio en el venv: .venv-ft314\\Scripts\\uvx.exe. El setup y el runtime resuelven uvx "
     "con esta precedencia: NEXUS_UVX, venv del proyecto, PATH. No hace falta reparar el uv "
     "del sistema ni pedir permisos de administrador.",
     ["mcp", "uvx", "troubleshooting"], 9),
    ("tools", "tools-registry", "Tools del harness",
     "El ToolRegistry expone tools por categoria: fs (read_file, write_file, edit, apply_patch, "
     "list_dir, glob, code_search, repo_map), ejecucion (exec, job_start/read/wait/kill/list), "
     "red (web_fetch, web_search), memoria (learn, recall), MCP (mcp_list, mcp_call), agentes "
     "(gema_*, agent_*, sub_agent*, team_*), imagenes (image_generate) y checkpoints. Toda tool "
     "de filesystem pasa por scope.check_path.",
     ["tools", "arquitectura"], 7),
    ("scope-jail", "scope-jail", "Scope jail",
     "El scope define que rutas y tools puede tocar el agente. permission=full libera; "
     "read_only (plan mode) bloquea mutaciones; require_confirm_mutating exige confirm=true. "
     "Se configura en config/config.yaml (scope.allow/deny) y toda tool FS pasa por "
     "scope.check_path/check_tool antes de ejecutarse.",
     ["seguridad", "scope"], 7),
    ("checkpoints", "checkpoints", "Checkpoints y diffs",
     "Antes de cada write/edit se crea un checkpoint (snapshot) automatico. Se listan y "
     "comparan con checkpoint_list/checkpoint_diff y se restauran con checkpoint_restore. En la "
     "UI aparecen en la pestana Cambios. Sirven para revertir un cambio del agente sin tocar git.",
     ["checkpoints", "seguridad"], 6),
    ("compactacion", "compactacion-de-contexto", "Compactacion de contexto",
     "El harness compacta en 4 capas: seguridad/redaccion, presupuesto por tokens, recorte de "
     "mensajes viejos y micro-compactacion. Se dispara por umbral de contexto o por budget del "
     "run (agent_loop.budget: max_iterations, max_tokens, max_wall_ms, max_tool_calls). Evita "
     "que una sesion larga reviente la ventana del modelo.",
     ["harness", "contexto"], 6),
    ("sesiones", "sesiones", "Sesiones y event store",
     "Las sesiones se guardan en data/session.db (SQLite WAL + FTS5): listar, buscar, fork, "
     "rewind y arbol. Los eventos van a data/event_store.db y alimentan auditoria, "
     "aprobaciones y replay. El session_log es la fuente para retomar una conversacion.",
     ["sesiones", "persistencia"], 6),
    ("routers", "routers-de-modelos", "Routers de modelos",
     "El provider router elige entre Ollama local, proveedores de nube BYOK (opencode-go, "
     "groq) y CLIs (claude, codex, opencode) segun capacidad (tools, vision), costo y "
     "latencia. El aprendizaje de routing persiste en data/router_state.json (Thompson "
     "Sampling). Si no hay nube configurada, todo corre local con Ollama.",
     ["modelos", "routing"], 6),
    ("ui", "ui-electron", "UI Electron + React",
     "La UI es React 19 + Vite + Tailwind. Electron la carga desde ui/dist en produccion y "
     "desde Vite :5174 en desarrollo (ELECTRON_IS_DEV=1). contextIsolation y sandbox activos, "
     "sin nodeIntegration. El renderer habla con el backend por HTTP (/api/*) usando la URL "
     "que expone el preload. Si la ventana queda en blanco: falta ui/dist o el backend no "
     "arranco (ver data/backend.log).",
     ["ui", "electron"], 7),
    ("config", "configuracion", "Configuracion",
     "Toda la configuracion esta en config/config.yaml (con config.schema.json para validar). "
     "Precedencia: variable de entorno > config.yaml > autodeteccion. Secretos solo por "
     "entorno (.env), nunca en el repo. Cambios de config se reflejan sin reinstalar; algunos "
     "requieren reiniciar el backend.",
     ["config"], 6),
    ("logs", "logs", "Donde mirar los logs",
     "data/backend.log: stdout/stderr del backend Python (el primer lugar a revisar). "
     "data/mcp_bundle/status.json: ultimo reporte de MCP (estado por servidor). "
     "INSTALAR-LOG.txt: resultado del bootstrap. ui/vite.log en dev. Los logs de la app "
     "Electron van a la consola del proceso principal.",
     ["logs", "troubleshooting"], 7),
    ("troubleshoot-ui", "troubleshoot-ui-no-carga", "La UI no carga o queda en blanco",
     "1) Verificar que exista ui/dist/index.html; si falta: npm run build:ui. 2) Verificar "
     "electron/dist/main.js; si falta: npm run build:electron. 3) Revisar data/backend.log: si "
     "el backend no arranco, falta una dependencia en el venv. 4) Probar el backend solo: "
     "python -m src.api.server --port 8765 y abrir http://127.0.0.1:8765. 5) El lanzador "
     "scripts\\nexus-v3-launch.vbs ya compila ambos artefactos si faltan.",
     ["troubleshooting", "ui"], 9),
    ("troubleshoot-mcp", "troubleshoot-mcp-no-responde", "Un MCP no responde",
     "1) python scripts/setup_mcp.py (sin --fix) muestra el estado por servidor. 2) Si dice "
     "FALTA, correr con --fix. 3) Si dice CLAVE, falta una variable de entorno propia del "
     "servicio (.env). 4) Probar la tool con mcp_list(server) y mcp_call: si el error es de "
     "aprobacion, agregar el patron a approval.mcp_read_only. 5) Si el servidor usa uvx y "
     "falla, revisar .venv-ft314\\Scripts\\uvx.exe (el setup lo instala).",
     ["troubleshooting", "mcp"], 9),
    ("troubleshoot-puerto", "troubleshoot-puerto-ocupado", "Puerto ocupado",
     "En modo app el backend usa un puerto libre: no hay conflicto. En modo dev el backend "
     "fijo es :8765 y Vite :5174. Si estan ocupados: netstat -ano | findstr :8765 y cerrar el "
     "proceso, o levantar con --port 0 para que elija uno libre. Ollama usa :11434.",
     ["troubleshooting", "puertos"], 5),
    ("verificacion", "verificacion-de-instalacion", "Verificar una instalacion",
     "python scripts\\verify_install.py comprueba: python del venv y dependencias, artefactos "
     "de UI y Electron, esquema y conteo del grafo, handshake del servidor MCP propio "
     "(initialize + tools/list + recall) y estado del bundle MCP. Devuelve exit 0 solo si todo "
     "pasa y escribe el detalle en pantalla. Es la forma objetiva de saber si la instalacion "
     "quedo completa, sin abrir la app.",
     ["verificacion", "instalacion"], 9),
    ("actualizaciones", "actualizaciones", "Actualizaciones",
     "El updater compara el manifiesto publicado con la version local ignorando diferencias de "
     "CRLF. El manifiesto se regenera con scripts/build_update_manifest.py y la URL se define "
     "en config (updates.manifest_url). Para actualizar un clon manual: git pull, npm install, "
     "npm run build y pip install -r requirements.txt dentro del venv.",
     ["updates"], 5),
    ("plan-mode", "plan-mode", "Plan mode",
     "En plan mode el agente no muta nada: el scope pasa a read_only y las tools de escritura "
     "quedan bloqueadas. El plan se revisa por un canal de aprobacion; sin canal, falla "
     "cerrado (no se puede pedir review).",
     ["harness", "seguridad"], 5),
    ("subagentes", "subagentes", "Sub-agentes y equipos",
     "sub_agent lanza un agente hijo continuable con su propia sesion; team_spawn crea un "
     "equipo con mailbox. Los hijos heredan scope y config pero no pueden escalar permisos: un "
     "deny del padre se hereda, un allow no. Sirven para paralelizar tareas independientes.",
     ["agentes"], 6),
]

EDGES = [
    ("que-es-nexus-v3", "estructura-del-repo", "documenta"),
    ("instalacion-rapida", "instalacion-manual", "detalla"),
    ("instalacion-rapida", "verificacion-de-instalacion", "termina_con"),
    ("instalacion-rapida", "mcp-sistema", "instala"),
    ("arranque-y-puertos", "ui-electron", "levanta"),
    ("brain-y-rutas", "grafo-bundles", "se_transfiere_con"),
    ("mcp-sistema", "mcp-aprobacion", "respeta"),
    ("mcp-sistema", "mcp-uvx-roto", "depende_de"),
    ("mcp-aprobacion", "troubleshoot-mcp-no-responde", "resuelve"),
    ("mcp-uvx-roto", "troubleshoot-mcp-no-responde", "resuelve"),
    ("troubleshoot-ui-no-carga", "logs", "mira"),
    ("troubleshoot-mcp-no-responde", "logs", "mira"),
    ("tools-registry", "scope-jail", "pasa_por"),
    ("checkpoints", "scope-jail", "protege"),
    ("compactacion-de-contexto", "sesiones", "persiste_en"),
    ("routers-de-modelos", "configuracion", "se_configura_en"),
    ("subagentes", "scope-jail", "hereda"),
    ("plan-mode", "scope-jail", "usa"),
]


def main() -> int:
    nodes = []
    for name, topic, title, content, tags, importance in NODES:
        nodes.append({
            "key": topic,
            "topic_key": topic,
            "content": content,
            "title": title,
            "category": "nexus",
            "source": "seed:nexus-v3",
            "importance": importance,
            "tags": tags,
            "content_hash": "",
            "created_at": "2026-10-26T00:00:00Z",
            "updated_at": "2026-10-26T00:00:00Z",
            "revision_count": 1,
        })
    keys = {n["key"] for n in nodes}
    for frm, to, rel in EDGES:
        assert frm in keys and to in keys, (frm, to)
    edges = [{"from": f, "to": t, "relation": r, "weight": 1.0, "note": "",
              "created_at": "2026-10-26T00:00:00Z"} for f, t, r in EDGES]
    bundle = {
        "schema": SCHEMA,
        "created_at": "2026-10-26T00:00:00Z",
        "source": {"app": "nexus-v3", "version": "seed"},
        "counts": {"nodes": len(nodes), "edges": len(edges)},
        "nodes": nodes,
        "edges": edges,
        "content_hash": _canonical_hash(nodes, edges),
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(bundle, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"escrito {OUT}  nodos={len(nodes)} aristas={len(edges)}  "
          f"{OUT.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
