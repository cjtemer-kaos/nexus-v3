# Nexus v3 UI — 2026 Harness Frontend

**Stack:** React 19 + Vite 6 + Tailwind CSS 4 + shadcn (Radix) + Zustand + Monaco + Framer Motion

Diseño fresco, no reciclado de supernexus-v2. Estética de arnés oscura con violeta/cian, vidrio, bento.

## Funcionalidades (BLUEPRINT §7)

- **Chat** — Simulación streaming TDAO, resultados de herramientas (GuardiaAlcance), copiar, compositor con Shift+Enter
- **Grafo** — Disposición por fuerzas en Canvas (12 nodos / 13 aristas), filtrar, clic para enfocar, leyenda arnés/cerebro/proveedor
- **Editor** — Monaco (vs-dark), explorador de archivos con aviso de jaula FS de Alcance, multi-pestañas, buscar, indicador guardado, barra estado
- **Terminal** — Simulación shell protegida, aviso Alcance.verificar, historial comandos, copiar/limpiar
- **Selector Modelo** — Enrutador Thompson/VRAM 12GB, grupos proveedores (llamacpp/ollama/nube), puntos estado en vivo

## Desarrollo

```bash
npm install
npm run dev     # :5174 with proxy /api -> :9000, /v1 -> :8080
npm run build   # tsc + vite
npm run preview # serve dist :5174
```

## Cableado al arnés

- Chat: `POST /api/chat` (RegistroSesión + BucleAgente)
- Grafo: `GET /api/grafo` (knowledge_graph.db)
- Editor: FS vía Alcance `raíces_permitidas: ./workspace`
- Terminal: `POST /api/herramientas/ejecutar` (protegido)
- Modelos: `GET /v1/models` + `EnrutadorProveedores.auto_detectar()`

Config: `../config/config.yaml` — nada hardcodeado.

## Build verificado

```
vite v6.4.3 — 1657 modules — 288 kB js (gzip 89 kB) — 45 kB css
Preview 200 OK — screenshots captured via Playwright (chat, graph, editor, terminal)
```
