import { useEffect, useState } from 'react'
import { MessageSquare, Share2, FileCode2, Terminal, Layers, Settings, Database, Shield, Cpu, Sparkles, Bot, Trash2, GitCompare, KanbanSquare, Puzzle, Network, ShieldCheck, ListChecks, GitBranch, Coins, ChevronDown, ChevronRight } from 'lucide-react'
import { useAppStore } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { SettingsDialog } from '@/components/SettingsDialog'

const primary = [
  { id: 'chat', label: 'Chat', icon: MessageSquare, desc: 'Bucle TDAO' },
  { id: 'agents', label: 'Ayudantes', icon: Bot, desc: 'LLM por asistente' },
  { id: 'graph', label: 'Grafo', icon: Share2, desc: 'Conocimiento' },
  { id: 'editor', label: 'Editor', icon: FileCode2, desc: 'Alcance FS' },
  { id: 'terminal', label: 'Terminal', icon: Terminal, desc: 'Protegido' },
] as const

const more = [
  { id: 'review', label: 'Cambios', icon: GitCompare, desc: 'Checkpoints y diff' },
  { id: 'approvals', label: 'Aprobaciones', icon: ShieldCheck, desc: 'Gate de permisos' },
  { id: 'plan', label: 'Plan', icon: ListChecks, desc: 'Plan editable' },
  { id: 'worktrees', label: 'Worktrees', icon: GitBranch, desc: 'Aislamiento git' },
  { id: 'usage', label: 'Uso', icon: Coins, desc: 'Tokens y costo' },
  { id: 'board', label: 'Tareas', icon: KanbanSquare, desc: 'Board + claims' },
  { id: 'subagents', label: 'Sub-agentes', icon: Network, desc: 'Delegación' },
  { id: 'skills', label: 'Capacidades', icon: Puzzle, desc: 'Tools y skills' },
] as const

type NavItem = { id: string; label: string; icon: typeof MessageSquare; desc: string }

function NavButton({ item, active, onClick, indented }: { item: NavItem; active: boolean; onClick: () => void; indented?: boolean }) {
  const Icon = item.icon
  return (
    <button
      onClick={onClick}
      title={`${item.label} · ${item.desc}`}
      className={cn('flex w-full items-center gap-3 rounded-lg py-2 text-sm transition-colors text-left',
        indented ? 'pl-4 pr-3' : 'px-3',
        active ? 'bg-[#0B62D6] text-white shadow' : 'text-[#9BA1AA] hover:bg-[#22262D] hover:text-[#EDEEF0]')}
    >
      <Icon className="h-4 w-4 shrink-0" />
      <span className="font-medium truncate">{item.label}</span>
      {!indented && <span className={cn('ml-auto text-[11px] truncate', active ? 'text-[#B9DBFF]' : 'text-[#6B7280]')}>{item.desc}</span>}
    </button>
  )
}

export function Sidebar() {
  const { view, setView, sessions, activeSession, setActiveSession, createSession, deleteSession } = useAppStore()
  const [ajustesOpen, setAjustesOpen] = useState(false)
  const inMore = more.some(i => i.id === view)
  const [moreOpen, setMoreOpen] = useState(false)
  useEffect(() => { if (inMore) setMoreOpen(true) }, [inMore])

  return (
    <aside className="hidden sm:flex w-[260px] shrink-0 min-h-0 flex-col overflow-hidden border-r border-[#2C3037] bg-[#15171B]">
      {/* brand */}
      <div className="shrink-0 px-4 py-4 border-b border-[#2C3037]">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-[#0B62D6] text-white font-bold text-sm shadow-lg">N³</div>
          <div className="min-w-0">
            <div className="text-sm font-semibold tracking-tight truncate">Nexus v3</div>
            <div className="text-xs text-[#9BA1AA] truncate">Arnés · 2026</div>
          </div>
          <span className="ml-auto rounded-full bg-[#3DDC84]/15 border border-[#3DDC84]/20 px-2 py-0.5 text-[10px] font-medium text-[#3DDC84]">ALFA</span>
        </div>
      </div>

      {/* nav + sesiones en un solo scroll (footer siempre visible, sin desbordes al hacer zoom) */}
      <div className="flex-1 min-h-0 overflow-y-auto">
        <div className="px-2.5 py-2 space-y-0.5">
          {primary.map(item => (
            <NavButton key={item.id} item={item} active={view === item.id} onClick={() => setView(item.id as any)} />
          ))}

          <button
            onClick={() => setMoreOpen(v => !v)}
            className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-[#9BA1AA] hover:bg-[#22262D] hover:text-[#EDEEF0] transition-colors"
            aria-expanded={moreOpen}
          >
            {moreOpen ? <ChevronDown className="h-4 w-4" /> : <ChevronRight className="h-4 w-4" />}
            <span className="font-medium">Más herramientas</span>
            <span className="ml-auto rounded-full bg-[#22262D] px-2 py-0.5 text-[10px] text-[#9BA1AA]">{more.length}</span>
          </button>

          {moreOpen && (
            <div className="space-y-0.5 border-l border-[#2C3037] ml-4 pl-0.5">
              {more.map(item => (
                <NavButton key={item.id} item={item} active={view === item.id} onClick={() => setView(item.id as any)} indented />
              ))}
            </div>
          )}
        </div>

        <div className="px-2.5 py-2 border-t border-[#2C3037]">
          <div className="flex items-center gap-2 px-2 py-2">
            <span className="text-[11px] font-semibold tracking-widest text-[#9BA1AA]">SESIONES</span>
            <span className="text-[10px] text-[#6B7280]">WAL+FTS</span>
            <span className="ml-auto text-[10px] text-[#6B7280]">{sessions.length}</span>
          </div>
          <div className="space-y-1">
            {sessions.length === 0 && <div className="px-2 py-1 text-xs text-[#6B7280]">Sin sesiones todavía.</div>}
            {sessions.map(s => (
              <div key={s.id} className="group relative">
                <button
                  onClick={() => setActiveSession(s.id)}
                  className={cn('w-full text-left rounded-lg px-3 py-2 border text-sm cursor-pointer transition-colors',
                    s.id === activeSession ? 'bg-[#22262D] border-[#2C3037] text-white' : 'border-transparent text-[#9BA1AA] hover:bg-[#1E2127] hover:text-[#EDEEF0]')}
                >
                  <div className="font-medium truncate pr-6">{s.title}</div>
                  <div className="text-[11px] text-[#9BA1AA] truncate">{s.project || s.source || 'local'} · {s.id}</div>
                </button>
                <button
                  aria-label={`Borrar sesión ${s.title}`}
                  title="Borrar sesión"
                  onClick={(e) => { e.stopPropagation(); if (confirm(`¿Borrar la sesión "${s.title}"? No se puede deshacer.`)) deleteSession(s.id) }}
                  className="absolute right-1.5 top-2 rounded-lg p-1.5 text-[#9BA1AA] opacity-0 group-hover:opacity-100 focus:opacity-100 hover:bg-[#2C3037] hover:text-[#FF6259] transition-opacity"
                >
                  <Trash2 className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
          <button onClick={() => createSession()} className="mt-3 w-full rounded-lg border border-dashed border-[#3A4049] px-3 py-2 text-sm text-[#9BA1AA] hover:text-[#EDEEF0] hover:border-[#4C9DFF]/40 transition-colors">+ Nueva sesión (bifurcar)</button>
        </div>
      </div>

      {/* footer */}
      <div className="shrink-0 border-t border-[#2C3037] p-2.5 space-y-2">
        <div className="rounded-xl bg-[#1E2127] border border-[#4C9DFF]/20 p-3">
          <div className="flex items-center gap-2 text-xs font-semibold"><Sparkles className="h-3.5 w-3.5 text-[#4C9DFF]" /> Cerebro local</div>
          <div className="mt-1 text-[11px] leading-relaxed text-[#9BA1AA]">Enlazado vía <span className="font-mono text-[#EDEEF0]">NEXUS_BRAIN</span> → <span className="font-mono text-[#EDEEF0]">./brain</span></div>
          <div className="mt-2 flex gap-1.5">
            <span className="rounded-full bg-[#2C3037] px-2 py-0.5 text-[10px] flex items-center gap-1"><Database className="h-3 w-3" /> FTS5</span>
            <span className="rounded-full bg-[#2C3037] px-2 py-0.5 text-[10px] flex items-center gap-1"><Cpu className="h-3 w-3" /> RAG 768d</span>
          </div>
        </div>
        <button aria-label="Abrir ajustes" onClick={() => setAjustesOpen(true)} className="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-sm text-[#9BA1AA] hover:bg-[#22262D] hover:text-[#EDEEF0]"><Settings className="h-4 w-4" /> Ajustes · config.yaml</button>
        <SettingsDialog open={ajustesOpen} onOpenChange={setAjustesOpen} />
      </div>
    </aside>
  )
}
