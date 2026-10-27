import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Search, MessageSquare, Plus, Bot, Share2, Brain, FileCode2, Terminal, GitCompare,
  ShieldCheck, ListChecks, GitBranch, Coins, KanbanSquare, Puzzle, Network, Settings,
  Loader2, CornerDownLeft, User as UserIcon,
} from 'lucide-react'
import { useAppStore } from '@/store/app-store'
import { searchApi } from '@/lib/api'
import { cn } from '@/lib/utils'

type Comando = { tipo: 'comando'; id: string; label: string; hint: string; icon: typeof Search; run: () => void }
type Sesion = { tipo: 'sesion'; id: string; label: string; hint: string }
type Hit = { tipo: 'hit'; id: string; label: string; hint: string; sessionId?: string; role: string }
type Item = Comando | Sesion | Hit

const vistas: { id: string; label: string; icon: typeof Search }[] = [
  { id: 'chat', label: 'Ir al chat', icon: MessageSquare },
  { id: 'agents', label: 'Ir a ayudantes', icon: Bot },
  { id: 'graph', label: 'Ir al grafo', icon: Share2 },
  { id: 'memory', label: 'Ir a memoria', icon: Brain },
  { id: 'editor', label: 'Ir al editor', icon: FileCode2 },
  { id: 'terminal', label: 'Ir a la terminal', icon: Terminal },
  { id: 'review', label: 'Ir a cambios', icon: GitCompare },
  { id: 'approvals', label: 'Ir a aprobaciones', icon: ShieldCheck },
  { id: 'plan', label: 'Ir al plan', icon: ListChecks },
  { id: 'worktrees', label: 'Ir a worktrees', icon: GitBranch },
  { id: 'usage', label: 'Ir a uso y costo', icon: Coins },
  { id: 'board', label: 'Ir a tareas', icon: KanbanSquare },
  { id: 'subagents', label: 'Ir a sub-agentes', icon: Network },
  { id: 'skills', label: 'Ir a capacidades', icon: Puzzle },
]

export function CommandPalette({ onAjustes }: { onAjustes: () => void }) {
  const { setView, sessions, activeSession, setActiveSession, createSession, loadMessages } = useAppStore()
  const [abierta, setAbierta] = useState(false)
  const [q, setQ] = useState('')
  const [sel, setSel] = useState(0)
  const [hits, setHits] = useState<Hit[]>([])
  const [buscando, setBuscando] = useState(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const listaRef = useRef<HTMLDivElement>(null)

  const abrir = useCallback(() => { setAbierta(true); setQ(''); setSel(0) }, [])
  const cerrar = useCallback(() => { setAbierta(false); setHits([]) }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const k = e.key.toLowerCase()
      if ((e.metaKey || e.ctrlKey) && k === 'k') { e.preventDefault(); abierta ? cerrar() : abrir(); return }
      if (abierta && k === 'escape') { e.preventDefault(); cerrar() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [abierta, abrir, cerrar])

  useEffect(() => {
    const abrirPaleta = () => abrir()
    window.addEventListener('nexus:palette', abrirPaleta)
    return () => window.removeEventListener('nexus:palette', abrirPaleta)
  }, [abrir])

  useEffect(() => {
    if (abierta) requestAnimationFrame(() => inputRef.current?.focus())
  }, [abierta])

  // Búsqueda en el historial (FTS5): solo con 2+ caracteres, con debounce.
  useEffect(() => {
    const term = q.trim()
    if (!abierta || term.length < 2) { setHits([]); setBuscando(false); return }
    let vivo = true
    setBuscando(true)
    const t = setTimeout(() => {
      searchApi(term)
        .then((rs) => {
          if (!vivo) return
          setHits((rs || [])
            .filter((r) => r.role === 'user' || r.role === 'assistant')
            .slice(0, 8)
            .map((r, i) => ({
              tipo: 'hit' as const, id: `hit-${i}`, role: r.role,
              label: r.content.replace(/\s+/g, ' ').trim().slice(0, 120) || '(vacío)',
              hint: `sesión ${r.session_id || ''}`.trim(), sessionId: r.session_id,
            })))
        })
        .catch(() => { if (vivo) setHits([]) })
        .finally(() => { if (vivo) setBuscando(false) })
    }, 220)
    return () => { vivo = false; clearTimeout(t) }
  }, [q, abierta])

  const comandos = useMemo<Comando[]>(() => {
    const fijos: Comando[] = [
      ...vistas.map((v) => ({
        tipo: 'comando' as const, id: `vista-${v.id}`, label: v.label, icon: v.icon,
        hint: v.id === 'chat' ? 'la vista principal' : '',
        run: () => { setView(v.id as never); cerrar() },
      })),
      {
        tipo: 'comando', id: 'nueva', label: 'Nueva sesión', icon: Plus, hint: 'Ctrl+N',
        run: () => { createSession(); setView('chat'); cerrar() },
      },
      {
        tipo: 'comando', id: 'ajustes', label: 'Abrir ajustes', icon: Settings, hint: 'config.yaml',
        run: () => { onAjustes(); cerrar() },
      },
    ]
    const term = q.trim().toLowerCase()
    if (!term) return fijos
    return fijos.filter((c) => c.label.toLowerCase().includes(term))
  }, [q, setView, createSession, cerrar, onAjustes])

  const sesiones = useMemo<Sesion[]>(() => {
    const term = q.trim().toLowerCase()
    const base = term
      ? sessions.filter((s) => `${s.title} ${s.id} ${s.project || ''}`.toLowerCase().includes(term))
      : sessions
    return base.slice(0, term ? 8 : 6).map((s) => ({
      tipo: 'sesion' as const, id: s.id,
      label: s.title || 'Sesión sin título',
      hint: s.id === activeSession ? 'abierta' : (s.project || s.id.slice(0, 12)),
    }))
  }, [sessions, q, activeSession])

  const items = useMemo<Item[]>(() => [...comandos, ...sesiones, ...hits], [comandos, sesiones, hits])

  useEffect(() => { setSel(0) }, [q])

  useEffect(() => {
    const el = listaRef.current?.querySelector('[data-sel="1"]')
    el?.scrollIntoView({ block: 'nearest' })
  }, [sel])

  const elegir = useCallback((it: Item) => {
    if (it.tipo === 'comando') { it.run(); return }
    const sid = it.tipo === 'sesion' ? it.id : it.sessionId
    if (sid) { setView('chat'); setActiveSession(sid); void loadMessages(sid) }
    cerrar()
  }, [setView, setActiveSession, loadMessages, cerrar])

  if (!abierta) return null

  let indice = -1
  const seccion = (titulo: string, lista: Item[]) => lista.length > 0 && (
    <div key={titulo}>
      <div className="px-3 pb-1 pt-2 text-[10px] uppercase tracking-wide text-[#59616D]">{titulo}</div>
      {lista.map((it) => {
        indice += 1
        const i = indice
        const activo = i === sel
        const Icon = it.tipo === 'comando' ? it.icon
          : it.tipo === 'sesion' ? MessageSquare : UserIcon
        return (
          <button
            key={`${it.tipo}-${it.id}`}
            data-sel={activo ? '1' : '0'}
            onMouseEnter={() => setSel(i)}
            onClick={() => elegir(it)}
            className={cn('flex w-full items-center gap-2.5 rounded-lg px-3 py-2 text-left text-sm',
              activo ? 'bg-[#262A31] text-[#EDEEF0]' : 'text-[#C9CDD3] hover:bg-[#22262D]')}
          >
            <Icon className={cn('h-4 w-4 shrink-0', it.tipo === 'hit' ? 'text-[#FFB86B]/80' : 'text-[#9BA1AA]')} />
            <span className="min-w-0 flex-1 truncate">{it.label}</span>
            {it.tipo === 'hit' && <span className="shrink-0 rounded bg-[#2C3037] px-1.5 py-0.5 text-[10px] text-[#9BA1AA]">{it.role}</span>}
            {it.hint && <span className="shrink-0 truncate text-[11px] text-[#59616D]">{it.hint}</span>}
          </button>
        )
      })}
    </div>
  )

  return (
    <>
      <div className="fixed inset-0 z-[90] bg-black/50" onClick={cerrar} />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Paleta de comandos"
        className="fixed left-1/2 top-[14%] z-[91] w-[min(92vw,620px)] -translate-x-1/2 overflow-hidden rounded-xl border border-[#2C3037] bg-[#1A1D23] shadow-2xl"
      >
        <div className="flex items-center gap-2 border-b border-[#2C3037] px-3 py-2.5">
          <Search className="h-4 w-4 shrink-0 text-[#9BA1AA]" />
          <input
            ref={inputRef}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'ArrowDown') { e.preventDefault(); setSel((s) => Math.min(items.length - 1, s + 1)) }
              if (e.key === 'ArrowUp') { e.preventDefault(); setSel((s) => Math.max(0, s - 1)) }
              if (e.key === 'Enter' && items[sel]) { e.preventDefault(); elegir(items[sel]) }
            }}
            placeholder="Buscar sesiones, mensajes o escribir un comando…"
            aria-label="Buscar o comando"
            className="min-w-0 flex-1 bg-transparent text-sm text-[#EDEEF0] placeholder:text-[#59616D] focus:outline-none"
          />
          {buscando && <Loader2 className="h-3.5 w-3.5 shrink-0 animate-spin text-[#4C9DFF]" />}
          <kbd className="shrink-0 rounded border border-[#2C3037] bg-[#22262D] px-1.5 py-0.5 text-[10px] text-[#9BA1AA]">Esc</kbd>
        </div>

        <div ref={listaRef} className="max-h-[52vh] overflow-y-auto p-1.5">
          {items.length === 0 && (
            <div className="px-3 py-6 text-center text-xs text-[#9BA1AA]">
              {q.trim().length >= 2 ? `Sin resultados para “${q.trim()}”` : 'Escribí para buscar'}
            </div>
          )}
          {seccion('Comandos', comandos)}
          {seccion('Sesiones', sesiones)}
          {seccion('Mensajes', hits)}
        </div>

        <div className="flex items-center gap-3 border-t border-[#2C3037] px-3 py-1.5 text-[10px] text-[#59616D]">
          <span className="flex items-center gap-1"><kbd className="rounded border border-[#2C3037] px-1">↑</kbd><kbd className="rounded border border-[#2C3037] px-1">↓</kbd> moverse</span>
          <span className="flex items-center gap-1"><CornerDownLeft className="h-3 w-3" /> abrir</span>
          <span className="ml-auto">Ctrl N sesión · Ctrl W cerrar · Ctrl B panel · Ctrl K buscar</span>
        </div>
      </div>
    </>
  )
}
