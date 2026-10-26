# Brain — Nexus v3

Directorio portable `./brain` (configurable vía `NEXUS_V3_BRAIN` o `brain.path` en
`config/config.yaml`).

- `knowledge_graph.db` — grafo OPCODE con FTS5. Se crea solo en el primer uso.
- `rag_index.db` — índice RAG (se genera/actualiza en runtime).

## Base de conocimiento compartida (`seed/`)

El grafo no arranca vacío: **la base de conocimiento de nexus-v3 viaja con el
producto** porque es lo que hace útiles a `recall`, al RAG, a las tools y a los
sub-agentes desde el primer arranque. Cualquier instalación nueva arranca con esa
base y después cada usuario sigue agregando lo suyo encima.

| Archivo | Qué es | Peso |
|---|---|---|
| `seed/nexus-base.nexuskg.json.gz` | Base real del grafo (conocimiento, lecciones, fixes, decisiones, arquitectura) saneada | ~3,7 MB |
| `seed/nexus-seed.nexuskg.json` | Semilla curada del producto (instalación, arquitectura, MCP, troubleshooting) | ~40 KB |

Saneado de la base (`scripts/make_base_bundle.py`):

- **redacta** secretos (API keys, tokens, bearer), emails, IPs y rutas absolutas
  de la máquina donde se generó (`D:\ias\...`, `C:\Users\<usuario>`, `/home/<usuario>`);
- **excluye** nodos de proyectos privados (servidor Minecraft, RCON, `mc_manager`);
- **no versiona nunca** `*.db` (los ids locales no son estables entre máquinas).

## Instalación nueva

```bash
python scripts/bootstrap_brain.py            # crea el esquema e importa TODA la base
python scripts/bootstrap_brain.py --dry-run  # qué haría, sin escribir
python scripts/bootstrap_brain.py --if-empty # solo si el grafo está vacío
```

- Importa todos los `seed/*.nexuskg.json` y `seed/*.nexuskg.json.gz`, con **merge**
  por `topic_key` y sin pisar lo que ya existe (`overwrite=False`).
- Deja una marca por bundle en `knowledge_meta` (`seed:<archivo>` = hash del
  contenido): la segunda corrida es instantánea, y si la base cambia de versión
  se vuelve a mergear lo nuevo.
- La primera importación (~6.800 nodos únicos) tarda unos minutos; el instalador
  lo avisa en pantalla.

## Agregar o regenerar la base

- Cambió el conocimiento del producto → `python scripts/make_base_bundle.py`
  (exporta el grafo local, sanea, fusiona la semilla curada y reescribe el `.gz`).
- Cambió solo la semilla curada → `python scripts/make_seed_bundle.py`.
- El `.gz` se genera con `mtime=0`: mismo contenido, mismo archivo (diff limpio).

## Mover conocimiento entre máquinas

No copies `*.db`. Usá bundles:

- Exportar: `GET /api/knowledge/export` → archivo `.nexuskg.json`.
- Importar: `POST /api/knowledge/import` con el bundle (`dry_run` por defecto).
- El merge es por `topic_key`: no duplica y archiva revisión al actualizar.

El `.gitignore` deja pasar `seed/*.nexuskg.json` y `seed/*.nexuskg.json.gz`.
