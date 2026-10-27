import { useEffect, useState } from 'react'
import { useAppStore } from '@/store/app-store'
import { agentsApi, type Agent } from '@/lib/api'
import { Bot } from 'lucide-react'

export function AgentSelector({ compact = false }: { compact?: boolean }) {
  const { activeAgent, setActiveAgent, view } = useAppStore()
  const [agents, setAgents] = useState<Agent[]>([])
  const [err, setErr] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    agentsApi().then((r) => { if (alive) { setAgents(r.agents); setErr(null) } })
      .catch((e) => { if (alive) setErr(String(e).slice(0, 160)) })
    return () => { alive = false }
  }, [view])

  if (compact) {
    return (
      <div className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs text-[#9BA1AA] hover:bg-[#262A31] hover:text-[#EDEEF0]"
        title={err ? `No se pudieron listar los ayudantes: ${err}` : 'Ayudante activo (LLM + prompt propios)'}>
        <Bot className="h-3.5 w-3.5 shrink-0 text-[#4C9DFF]" />
        <select
          value={activeAgent}
          onChange={(e) => setActiveAgent(e.target.value)}
          aria-label="Elegir ayudante"
          className="max-w-[120px] cursor-pointer bg-transparent text-xs text-[#9BA1AA] focus:outline-none"
        >
          {agents.length === 0 && <option value="default" className="bg-[#1E2127]">Nexus (default)</option>}
          {agents.map((a) => (
            <option key={a.id} value={a.id} className="bg-[#1E2127]">
              {a.emoji ? a.emoji + ' ' : ''}{a.name}{a.builtin ? ' (default)' : ''}
            </option>
          ))}
        </select>
      </div>
    )
  }

  return (
    <div className="flex items-center gap-1.5 rounded-xl border border-[#2C3037] bg-[#1E2127] px-2 py-1.5"
      title={err ? `No se pudieron listar los ayudantes: ${err}` : 'Ayudante activo (LLM + prompt propios)'}>
      <Bot className="h-3.5 w-3.5 text-[#4C9DFF]" />
      {err && <span className="text-[10px] text-[#FFB86B]" aria-label="Error al listar ayudantes">⚠</span>}
      <select
        value={activeAgent}
        onChange={(e) => setActiveAgent(e.target.value)}
        aria-label="Elegir ayudante"
        className="max-w-[150px] bg-transparent text-xs text-[#EDEEF0] focus:outline-none"
      >
        {agents.length === 0 && <option value="default" className="bg-[#1E2127]">Nexus (default)</option>}
        {agents.map((a) => (
          <option key={a.id} value={a.id} className="bg-[#1E2127]">
            {a.emoji ? a.emoji + ' ' : ''}{a.name}{a.builtin ? ' (default)' : ''}
          </option>
        ))}
      </select>
    </div>
  )
}
