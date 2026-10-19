import { useAppStore } from '@/store/app-store'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Button } from '@/components/ui/button'
import { Bot, RefreshCw, Loader2, GitBranch } from 'lucide-react'

type Child = {
  child_id: string; task: string; status: string; provider: string
  mode: string; depth: number; parent_id: string; child_session_id: string; inbox_len: number
}

const STATUS: Record<string, string> = {
  begin: 'bg-[#4C9DFF]/15 text-[#8CC1FF]',
  active: 'bg-[#4C9DFF]/20 text-[#8CC1FF]',
  running: 'bg-[#4C9DFF]/20 text-[#8CC1FF]',
  completed: 'bg-[#3DDC84]/15 text-[#8EE6B4]',
  failed: 'bg-[#FF6B6B]/15 text-[#FFA8A8]',
  cancelled: 'bg-[#6B7280]/20 text-[#9BA1AA]',
  timeout: 'bg-[#FFB86B]/15 text-[#FFB86B]',
}

export function SubAgentsPanel() {
  const { liveStatus, statusError, loadStatus } = useAppStore()
  const subs = (liveStatus?.sub_agents ?? {}) as Record<string, Child[]>
  const loading = false
  const load = () => void loadStatus()

  const total = Object.values(subs).reduce((n, a) => n + a.length, 0)
  const sinConexion = !liveStatus

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-3">
        <Bot className="h-4 w-4 text-[#4C9DFF]" />
        <span className="text-sm font-medium">Sub-agentes</span>
        <span className={`ml-auto text-xs ${sinConexion ? 'text-[#FF6B6B]' : 'text-[#9BA1AA]'}`}>
          {sinConexion ? 'sin conexión' : `${total} · en vivo`}
        </span>
        <Button variant="ghost" size="icon" aria-label="Recargar" onClick={() => void load()}>
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>
      <ScrollArea className="flex-1">
        {sinConexion && (
          <div className="p-4 text-xs text-[#FFA8A8]">
            No se pudo consultar <span className="font-mono">/api/status</span>
            {statusError ? `: ${statusError}` : '.'} — esto NO significa que no haya sub-agentes.
          </div>
        )}
        {!sinConexion && total === 0 && (
          <div className="p-4 text-xs text-[#9BA1AA]">
            Sin sub-agentes. El agente los crea con la tool <span className="font-mono text-[#8CC1FF]">sub_agent</span> (mode continuable para mantenerlos vivos).
          </div>
        )}
        {Object.entries(subs).map(([sid, kids]) => (
          <div key={sid}>
            <div className="border-b border-[#2C3037] bg-[#1A1D22] px-4 py-1.5 text-[10px] uppercase tracking-wide text-[#9BA1AA]">sesión {sid}</div>
            {kids.map((c) => (
              <div key={c.child_id} className="border-b border-[#2C3037] px-4 py-2.5">
                <div className="flex items-center gap-2">
                  <span className={`rounded px-2 py-0.5 text-[10px] font-medium ${STATUS[c.status] || STATUS.cancelled}`}>{c.status}</span>
                  <span className="font-mono text-[11px] text-[#8CC1FF]">{c.child_id}</span>
                  <span className="rounded bg-[#2C3037] px-1.5 py-0.5 text-[10px] text-[#9BA1AA]">{c.provider}·{c.mode}</span>
                  {c.depth > 1 && <span className="flex items-center gap-0.5 text-[10px] text-[#9BA1AA]"><GitBranch className="h-3 w-3" />nivel {c.depth}</span>}
                  {c.inbox_len > 0 && <span className="text-[10px] text-[#FFB86B]">{c.inbox_len} mensaje(s)</span>}
                </div>
                <div className="mt-1 truncate text-xs text-[#EDEEF0]">{c.task}</div>
                <div className="truncate text-[10px] text-[#9BA1AA]">{c.child_session_id}</div>
              </div>
            ))}
          </div>
        ))}
      </ScrollArea>
    </div>
  )
}
