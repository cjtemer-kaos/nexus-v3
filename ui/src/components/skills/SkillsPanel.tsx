import { useEffect, useState } from 'react'
import { skillsApi, toolsApi, pluginsApi, pluginsReloadApi, type Skill, type ToolInfo, type PluginInfo } from '@/lib/api'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Puzzle, Wrench, RefreshCw, Loader2, Plug } from 'lucide-react'
import { Button } from '@/components/ui/button'

export function SkillsPanel() {
  const [skills, setSkills] = useState<Skill[]>([])
  const [tools, setTools] = useState<ToolInfo[]>([])
  const [plugins, setPlugins] = useState<PluginInfo[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [reloading, setReloading] = useState(false)
  const [tab, setTab] = useState<'skills' | 'tools' | 'plugins'>('tools')

  const load = async () => {
    setLoading(true); setError(null)
    try {
      const [s, t, p] = await Promise.all([skillsApi(), toolsApi(), pluginsApi()])
      setSkills(s.skills); setTools(t.tools); setPlugins(p.plugins)
    } catch (e) {
      setError('No se pudieron cargar las capacidades: ' + (e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const reloadPlugins = async () => {
    if (reloading) return
    setReloading(true); setError(null)
    try {
      const r = await pluginsReloadApi()
      await load()
      if (r.errors?.length) {
        setError(`recargados ${r.loaded.length}/${r.available.length}; fallaron: ${r.errors.map(e => `${e.plugin} (${e.error})`).join(' · ')}`)
      }
    } catch (e) {
      setError('no se pudieron recargar los plugins: ' + (e as Error).message)
    } finally {
      setReloading(false)
    }
  }

  useEffect(() => { void load() }, [])

  const items = tab === 'skills' ? skills : tools

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-3">
        {tab === 'tools' ? <Wrench className="h-4 w-4 text-[#4C9DFF]" /> : tab === 'skills' ? <Puzzle className="h-4 w-4 text-[#B18CFF]" /> : <Plug className="h-4 w-4 text-[#3DDC84]" />}
        <span className="text-sm font-medium">Capacidades</span>
        <div className="ml-3 flex gap-1">
          <button onClick={() => setTab('tools')} className={`rounded-md px-2 py-0.5 text-xs ${tab === 'tools' ? 'bg-[#2C3037] text-[#EDEEF0]' : 'text-[#9BA1AA]'}`}>Herramientas ({tools.length})</button>
          <button onClick={() => setTab('skills')} className={`rounded-md px-2 py-0.5 text-xs ${tab === 'skills' ? 'bg-[#2C3037] text-[#EDEEF0]' : 'text-[#9BA1AA]'}`}>Skills ({skills.length})</button>
          <button onClick={() => setTab('plugins')} className={`rounded-md px-2 py-0.5 text-xs ${tab === 'plugins' ? 'bg-[#2C3037] text-[#EDEEF0]' : 'text-[#9BA1AA]'}`}>Plugins ({plugins.length})</button>
        </div>
        {tab === 'plugins' && (
          <Button variant="outline" size="sm" className="ml-auto" disabled={reloading} onClick={() => void reloadPlugins()}>
            {reloading && <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />} Recargar plugins
          </Button>
        )}
        <Button variant="ghost" size="icon" aria-label="Recargar" disabled={loading} onClick={() => void load()} className="ml-auto">
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>
      <ScrollArea className="flex-1">
        {error && (
          <div className="flex items-center gap-2 border-b border-[#FF6B6B]/30 bg-[#FF6B6B]/10 px-4 py-2 text-xs text-[#FFA8A8]">
            <span className="flex-1">{error}</span>
            <button onClick={() => void load()} className="underline hover:text-white">Reintentar</button>
          </div>
        )}
        {items.length === 0 && !error && (
          <div className="p-4 text-xs text-[#9BA1AA]">{loading ? 'Cargando…' : 'Nada para mostrar.'}</div>
        )}
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
        {tab === 'plugins' && (
          <>
            {plugins.length === 0 && !error && !loading && <div className="p-4 text-xs text-[#9BA1AA]">Sin plugins. Poné un .py en ./plugins con una función <span className="font-mono text-[#8CC1FF]">register(registry)</span>.</div>}
            {plugins.map((p) => (
              <div key={p.path} className="border-b border-[#2C3037] px-4 py-2">
                <div className="font-mono text-xs text-[#8EE6B4]">{p.name}</div>
                <div className="mt-0.5 truncate text-[10px] text-[#9BA1AA]">{p.path}</div>
              </div>
            ))}
          </>
        )}
      </ScrollArea>
    </div>
  )
}
