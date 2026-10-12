import { useState } from 'react'
import { MessageSquare, Share2, FileCode2, Terminal, Layers, Settings, Database, Shield, Cpu, Sparkles, Bot, Trash2, GitCompare, KanbanSquare, Puzzle, Network, ShieldCheck, ListChecks } from 'lucide-react'
import { useAppStore } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { SettingsDialog } from '@/components/SettingsDialog'

const nav = [
  { id: 'chat', label: 'Chat', icon: MessageSquare, desc: 'Bucle Agente TDAO' },
  { id: 'agents', label: 'Ayudantes', icon: Bot, desc: 'LLM por asistente' },
  { id: 'graph', label: 'Grafo', icon: Share2, desc: 'Conocimiento' },
  { id: 'editor', label: 'Editor', icon: FileCode2, desc: 'Alcance FS' },
  { id: 'review', label: 'Cambios', icon: GitCompare, desc: 'Checkpoints y diff' },
  { id: 'approvals', label: 'Aprobaciones', icon: ShieldCheck, desc: 'Gate de permisos' },
  { id: 'plan', label: 'Plan', icon: ListChecks, desc: 'Plan editable' },
  { id: 'board', label: 'Tareas', icon: KanbanSquare, desc: 'Board + claims' },
  { id: 'subagents', label: 'Sub-agentes', icon: Network, desc: 'Delegación en vivo' },
  { id: 'skills', label: 'Capacidades', icon: Puzzle, desc: 'Tools y skills' },
  { id: 'terminal', label: 'Terminal', icon: Terminal, desc: 'Protegido' },
] as const

export function Sidebar() {
  const { view, setView, sessions, activeSession, setActiveSession, createSession, deleteSession } = useAppStore()
  const [ajustesOpen, setAjustesOpen] = useState(false)

  return (
    <aside className="hidden md:flex w-[280px] shrink-0 flex-col border-r border-[#2C3037] bg-[#15171B]">
      {/* brand */}
      <div className="px-5 py-5 border-b border-[#2C3037]">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-[#0B62D6] text-white font-bold text-sm shadow-lg">N³</div>
          <div>
            <div className="text-sm font-semibold tracking-tight">Nexus v3</div>
            <div className="text-xs text-[#9BA1AA]">Arnés · 2026</div>
          </div>
          <span className="ml-auto rounded-full bg-[#3DDC84]/15 border border-[#3DDC84]/20 px-2 py-0.5 text-[10px] font-medium text-[#3DDC84]">ALFA</span>
        </div>
        <div className="mt-3 flex items-center gap-2 text-xs">
          <span className="flex items-center gap-1.5 rounded-full bg-[#22262D] px-2.5 py-1"><Layers className="h-3 w-3 text-[#4C9DFF]" /> compactación 4 capas</span>
          <span className="flex items-center gap-1 rounded-full bg-[#22262D] px-2.5 py-1"><Shield className="h-3 w-3 text-[#3DDC84]" /> Alcance</span>
        </div>
      </div>

      {/* nav */}
      <div className="p-3 space-y-1">
        {nav.map(item => (
          <button
            key={item.id}
            onClick={() => setView(item.id as any)}
            className={cn('flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition-all text-left', view===item.id ? 'bg-[#0B62D6] text-white shadow' : 'text-[#9BA1AA] hover:bg-[#22262D] hover:text-[#EDEEF0]')}
          >
            <item.icon className="h-4 w-4" />
            <span className="font-medium">{item.label}</span>
            <span className={cn('ml-auto text-xs', view===item.id ? 'text-[#B9DBFF]' : 'text-[#9BA1AA]')}>{item.desc}</span>
          </button>
        ))}
      </div>

      {/* sessions */}
      <div className="px-3 py-2 flex-1 overflow-auto">
        <div className="text-xs font-semibold tracking-widest text-[#9BA1AA] px-2 py-2">SESIONES · WAL+FTS</div>
        <div className="space-y-1">
          {sessions.map(s=>(
            <div key={s.id} className="group relative">
              <button onClick={()=>setActiveSession(s.id)} className={cn('w-full text-left rounded-xl px-3 py-2.5 border text-sm cursor-pointer transition-colors', s.id===activeSession ? 'bg-[#22262D] border-[#2C3037] text-white' : 'border-transparent text-[#9BA1AA] hover:bg-[#1E2127] hover:text-[#EDEEF0]')}>
                <div className="font-medium truncate pr-6">{s.title}</div>
                <div className="text-xs text-[#9BA1AA] truncate">{s.project || s.source || 'local'} · {s.id} · FTS</div>
              </button>
              <button
                aria-label={`Borrar sesión ${s.title}`}
                title="Borrar sesión"
                onClick={(e)=>{ e.stopPropagation(); if(confirm(`¿Borrar la sesión "${s.title}"? No se puede deshacer.`)) deleteSession(s.id) }}
                className="absolute right-1.5 top-2 rounded-lg p-1.5 text-[#9BA1AA] opacity-0 group-hover:opacity-100 focus:opacity-100 hover:bg-[#2C3037] hover:text-[#FF6259] transition-opacity"
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))}
        </div>
        <button onClick={()=>createSession()} className="mt-3 w-full rounded-xl border border-dashed border-[#3A4049] px-3 py-2 text-sm text-[#9BA1AA] hover:text-[#EDEEF0] hover:border-[#3A4049]">+ Nueva sesión (bifurcar)</button>
      </div>

      {/* footer */}
      <div className="border-t border-[#2C3037] p-3 space-y-2">
        <div className="rounded-xl bg-[#1E2127] border border-[#4C9DFF]/20 p-3">
          <div className="flex items-center gap-2 text-xs font-semibold"><Sparkles className="h-3.5 w-3.5 text-[#4C9DFF]" /> Cerebro local</div>
          <div className="mt-1 text-xs leading-relaxed text-[#9BA1AA]">Enlazado vía <span className="font-mono text-[#EDEEF0]">NEXUS_BRAIN</span> → <span className="font-mono text-[#EDEEF0]">./brain</span></div>
          <div className="mt-2 flex gap-1.5">
            <span className="rounded-full bg-[#2C3037] px-2 py-0.5 text-xs flex items-center gap-1"><Database className="h-3 w-3" /> FTS5</span>
            <span className="rounded-full bg-[#2C3037] px-2 py-0.5 text-xs flex items-center gap-1"><Cpu className="h-3 w-3" /> RAG 768d</span>
          </div>
        </div>
        <button aria-label="Abrir ajustes" onClick={()=>setAjustesOpen(true)} className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-sm text-[#9BA1AA] hover:bg-[#22262D] hover:text-[#EDEEF0]"><Settings className="h-4 w-4" /> Ajustes · config.yaml</button>
        <SettingsDialog open={ajustesOpen} onOpenChange={setAjustesOpen} />
      </div>
    </aside>
  )
}
