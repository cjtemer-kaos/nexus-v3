# AGENTS.md — ui/src

React + Vite + Tailwind (tema oscuro `#15171B`). Reglas:

- **Vistas** en `store/app-store.ts` (type `View`) + nav en `components/Sidebar.tsx`
  + render en `App.tsx` (`viewLabels` + `{view==='x' && <Panel/>}`).
- **API** en `lib/api.ts` (`apiFetch`, base = mismo origen / proxy Vite). No hardcodees URLs.
- **Componentes UI** reutilizá `components/ui/{button,scroll-area,tabs,badge}.tsx`.
- Paleta: fondo `#15171B`, paneles `#1E2127`, borde `#2C3037`, acento `#4C9DFF`,
  verde `#3DDC84`, rojo `#FF6B6B`, ámbar `#FFB86B`, texto `#EDEEF0`, muted `#9BA1AA`.
- Vistas existentes: chat, agents(ayudantes), graph(grafo), editor, review(cambios),
  board(tareas), subagents(sub-agentes), skills(capacidades), terminal.
- **Siempre** `npm run build` antes de commitear; verificá en navegador (dev :5174).
- Sin comentarios innecesarios. Español en textos de UI.
