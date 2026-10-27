import { useAppStore } from '@/store/app-store'
import { Plus, X, PanelLeft } from 'lucide-react'
import { cn } from '@/lib/utils'

const corto = (t: string, max = 34) => {
  const s = (t || 'Sesión').replace(/\s+/g, ' ').trim()
  return s.length > max ? s.slice(0, max - 1) + '…' : s
}

export function SessionTabs() {
  const { sessions, activeSession, setActiveSession, openTabs, closeSessionTab, createSession, setView } = useAppStore()
  const porId = new Map(sessions.map((s) => [s.id, s]))
  const abiertas = openTabs.filter((id) => porId.has(id))

  return (
    <div className="flex h-full items-stretch gap-1">
      <button
        onClick={() => setView('chat')}
        title="Ir al chat"
        aria-label="Ir al chat"
        className="my-1.5 flex w-7 shrink-0 items-center justify-center rounded-md text-[#9BA1AA] hover:bg-[#22262D] hover:text-[#EDEEF0]"
      >
        <PanelLeft className="h-4 w-4" />
      </button>

      <div className="flex min-w-0 flex-1 items-stretch gap-1 overflow-x-auto">
        {abiertas.length === 0 && (
          <span className="flex items-center px-2 text-xs text-[#59616D]">Abrí una sesión desde el panel de la izquierda</span>
        )}
        {abiertas.map((id) => {
          const s = porId.get(id)!
          const activa = id === activeSession
          return (
            <div
              key={id}
              className={cn(
                'group my-1.5 flex min-w-0 max-w-[240px] shrink-0 items-center gap-1.5 rounded-md px-2 text-xs',
                activa ? 'bg-[#262A31] text-[#EDEEF0]' : 'text-[#9BA1AA] hover:bg-[#1E2127] hover:text-[#EDEEF0]',
              )}
            >
              <button
                onClick={() => { setView('chat'); setActiveSession(id) }}
                title={`${s.title}${s.project ? ` · ${s.project}` : ''}`}
                className="min-w-0 flex-1 truncate py-1.5 text-left"
              >
                {corto(s.title)}
              </button>
              <button
                onClick={() => closeSessionTab(id)}
                aria-label={`Cerrar pestaña ${s.title}`}
                title="Cerrar pestaña (la sesión sigue guardada)"
                className="shrink-0 rounded p-0.5 text-[#59616D] opacity-0 transition-opacity hover:bg-[#33383F] hover:text-[#EDEEF0] group-hover:opacity-100 focus:opacity-100"
              >
                <X className="h-3 w-3" />
              </button>
            </div>
          )
        })}
        <button
          onClick={() => { setView('chat'); createSession() }}
          title="Sesión nueva"
          aria-label="Sesión nueva"
          className="my-1.5 flex w-8 shrink-0 items-center justify-center rounded-md text-[#9BA1AA] hover:bg-[#22262D] hover:text-[#EDEEF0]"
        >
          <Plus className="h-4 w-4" />
        </button>
      </div>
    </div>
  )
}
