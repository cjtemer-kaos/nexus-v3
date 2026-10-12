import { useEffect, useState } from 'react'
import { skillsApi, toolsApi, type Skill, type ToolInfo } from '@/lib/api'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Puzzle, Wrench, RefreshCw, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'

export function SkillsPanel() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [loading, setLoading] = useState(false)
  const [tab, setTab] = useState<'skills' | 'tools'>('tools')

  const load = async () => {
    setLoading(true)
    try {
      const [s, t] = await Promise.all([skillsApi(), toolsApi()])
      setSkills(s.skills); setTools(t.tools)
    } catch {
      // best-effort
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const items = tab === 'skills' ? skills : tools

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-3">
        {tab === 'tools' ? <Wrench className="h-4 w-4 text-[#4C9DFF]" /> : <Puzzle className="h-4 w-4 text-[#B18CFF]" />}
        <span className="text-sm font-medium">Capacidades</span>
        <div className="ml-3 flex gap-1">
          <button onClick={() => setTab('tools')} className={`rounded-md px-2 py-0.5 text-xs ${tab === 'tools' ? 'bg-[#2C3037] text-[#EDEEF0]' : 'text-[#9BA1AA]'}`}>Herramientas ({tools.length})</button>
          <button onClick={() => setTab('skills')} className={`rounded-md px-2 py-0.5 text-xs ${tab === 'skills' ? 'bg-[#2C3037] text-[#EDEEF0]' : 'text-[#9BA1AA]'}`}>Skills ({skills.length})</button>
        </div>
        <Button variant="ghost" size="icon" aria-label="Recargar" onClick={() => void load()} className="ml-auto">
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>
      <ScrollArea className="flex-1">
        {items.length === 0 && <div className="p-4 text-xs text-[#9BA1AA]">Nada para mostrar.</div>}
        {tab === 'tools' && tools.map((t) => (
          <div key={t.name} className="border-b border-[#2C3037] px-4 py-2">
            <div className="flex items-center gap-2">
              <span className="font-mono text-xs text-[#8CC1FF]">{t.name}</span>
              <span className="rounded bg-[#2C3037] px-1.5 py-0.5 text-[10px] text-[#9BA1AA]">{t.category}</span>
            </div>
            <div className="mt-0.5 text-xs text-[#9BA1AA]">{t.description}</div>
          </div>
        ))}
        {tab === 'skills' && skills.map((s) => (
          <div key={s.path} className="border-b border-[#2C3037] px-4 py-2">
            <div className="font-mono text-xs text-[#C9B6FF]">{s.name}</div>
            <div className="mt-0.5 text-xs text-[#9BA1AA]">{s.description || '(sin descripción)'}</div>
          </div>
        ))}
      </ScrollArea>
    </div>
  )
}
