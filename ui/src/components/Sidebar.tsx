import { useState } from 'react'
import { MessageSquare, Share2, FileCode2, Terminal, Layers, Settings, Database, Shield, Cpu, Sparkles } from 'lucide-react'
import { useAppStore } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { SettingsDialog } from '@/components/SettingsDialog'

const nav = [
  { id: 'chat', label: 'Chat', icon: MessageSquare, desc: 'Bucle Agente TDAO' },
  { id: 'graph', label: 'Grafo', icon: Share2, desc: 'Conocimiento' },
  { id: 'editor', label: 'Editor', icon: FileCode2, desc: 'Alcance FS' },
  { id: 'terminal', label: 'Terminal', icon: Terminal, desc: 'Protegido' },
] as const

export function Sidebar() {
  const { view, setView, sessions, activeSession, setActiveSession, createSession } = useAppStore()
  const [ajustesOpen, setAjustesOpen] = useState(false)

  return (
    <aside className="hidden md:flex w-[280px] shrink-0 flex-col border-r border-white/10 bg-zinc-950">
      {/* brand */}
      <div className="px-5 py-5 border-b border-white/10">
        <div className="flex items-center gap-3">
          <div className="flex h-9 w-9 items-center justify-center rounded-xl bg-gradient-to-br from-violet-600 to-cyan-500 text-white font-bold text-sm shadow-lg">N³</div>
          <div>
            <div className="text-sm font-semibold tracking-tight">Nexus v3</div>
            <div className="text-xs text-zinc-500">Arnés · 2026</div>
          </div>
          <span className="ml-auto rounded-full bg-emerald-500/15 border border-emerald-500/20 px-2 py-0.5 text-[10px] font-medium text-emerald-400">ALFA</span>
        </div>
        <div className="mt-3 flex items-center gap-2 text-xs">
          <span className="flex items-center gap-1.5 rounded-full bg-white/[0.06] px-2.5 py-1"><Layers className="h-3 w-3 text-violet-400" /> compactación 4 capas</span>
          <span className="flex items-center gap-1 rounded-full bg-white/[0.06] px-2.5 py-1"><Shield className="h-3 w-3 text-emerald-400" /> Alcance</span>
        </div>
      </div>

      {/* nav */}
      <div className="p-3 space-y-1">
        {nav.map(item => (
          <button
            key={item.id}
            onClick={() => setView(item.id as any)}
            className={cn('flex w-full items-center gap-3 rounded-xl px-3 py-2.5 text-sm transition-all text-left', view===item.id ? 'bg-violet-600 text-white shadow' : 'text-zinc-400 hover:bg-white/[0.06] hover:text-zinc-100')}
          >
            <item.icon className="h-4 w-4" />
            <span className="font-medium">{item.label}</span>
            <span className={cn('ml-auto text-xs', view===item.id ? 'text-violet-200' : 'text-zinc-500')}>{item.desc}</span>
          </button>
        ))}
      </div>

      {/* sessions */}
      <div className="px-3 py-2 flex-1 overflow-auto">
        <div className="text-xs font-semibold tracking-widest text-zinc-500 px-2 py-2">SESIONES · WAL+FTS</div>
        <div className="space-y-1">
          {sessions.map(s=>(
            <button key={s.id} onClick={()=>setActiveSession(s.id)} className={cn('w-full text-left rounded-xl px-3 py-2.5 border text-sm cursor-pointer transition-colors', s.id===activeSession ? 'bg-white/[0.06] border-white/10 text-white' : 'border-transparent text-zinc-500 hover:bg-white/[0.04] hover:text-zinc-300')}>
              <div className="font-medium truncate">{s.title}</div>
              <div className="text-xs text-zinc-500 truncate">{s.project || s.source || 'local'} · {s.id} · FTS</div>
            </button>
          ))}
        </div>
        <button onClick={()=>createSession()} className="mt-3 w-full rounded-xl border border-dashed border-white/15 px-3 py-2 text-sm text-zinc-500 hover:text-zinc-300 hover:border-white/25">+ Nueva sesión (bifurcar)</button>
      </div>

      {/* footer */}
      <div className="border-t border-white/10 p-3 space-y-2">
        <div className="rounded-xl bg-gradient-to-br from-violet-600/20 to-cyan-500/20 border border-violet-500/20 p-3">
          <div className="flex items-center gap-2 text-xs font-semibold"><Sparkles className="h-3.5 w-3.5 text-violet-400" /> Cerebro local</div>
          <div className="mt-1 text-xs leading-relaxed text-zinc-400">Enlazado vía <span className="font-mono text-zinc-300">NEXUS_BRAIN</span> → <span className="font-mono text-zinc-300">./brain</span></div>
          <div className="mt-2 flex gap-1.5">
            <span className="rounded-full bg-white/10 px-2 py-0.5 text-xs flex items-center gap-1"><Database className="h-3 w-3" /> FTS5</span>
            <span className="rounded-full bg-white/10 px-2 py-0.5 text-xs flex items-center gap-1"><Cpu className="h-3 w-3" /> RAG 768d</span>
          </div>
        </div>
        <button aria-label="Abrir ajustes" onClick={()=>setAjustesOpen(true)} className="flex w-full items-center gap-2 rounded-xl px-3 py-2 text-sm text-zinc-500 hover:bg-white/[0.06] hover:text-zinc-200"><Settings className="h-4 w-4" /> Ajustes · config.yaml</button>
        <SettingsDialog open={ajustesOpen} onOpenChange={setAjustesOpen} />
      </div>
    </aside>
  )
}
