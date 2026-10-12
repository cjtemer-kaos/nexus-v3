import { useCallback, useEffect, useState } from 'react'
import { worktreesApi, worktreeAddApi, worktreeRemoveApi, type Worktree } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { GitBranch, RefreshCw, Plus, Trash2, Loader2 } from 'lucide-react'

export function WorktreesPanel() {
  const [items, setItems] = useState<Worktree[]>([])
  const [loading, setLoading] = useState(false)
  const [branch, setBranch] = useState('')
  const [path, setPath] = useState('')
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const r = await worktreesApi()
      setItems(r.worktrees)
    } catch (e) { setError((e as Error).message) } finally { setLoading(false) }
  }, [])

  useEffect(() => { void load() }, [load])

  const add = async () => {
    const b = branch.trim(); const p = path.trim()
    if (!b || !p) { setError('branch y path son requeridos'); return }
    try {
      const r = await worktreeAddApi({ branch: b, path: p })
      if (!r.ok) setError(r.output || 'no se pudo crear')
      setBranch(''); setPath('')
      await load()
    } catch (e) { setError((e as Error).message) }
  }

  const remove = async (wt: Worktree) => {
    if (!confirm(`¿Quitar el worktree ${wt.path}?`)) return
    try { await worktreeRemoveApi({ path: wt.path }); await load() }
    catch (e) { setError((e as Error).message) }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-3">
        <GitBranch className="h-4 w-4 text-[#4C9DFF]" />
        <span className="text-sm font-medium">Worktrees</span>
        <span className="ml-auto text-xs text-[#9BA1AA]">{items.length}</span>
        <Button variant="ghost" size="icon" aria-label="Recargar" onClick={() => void load()}>
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-2 border-b border-[#2C3037] px-4 py-2">
        <input value={branch} onChange={(e) => setBranch(e.target.value)} placeholder="rama (ej: feature/x)"
          className="w-48 rounded-lg border border-[#2C3037] bg-[#1E2127] px-3 py-1.5 text-sm focus:outline-none focus:border-[#4C9DFF]/50" />
        <input value={path} onChange={(e) => setPath(e.target.value)} placeholder="ruta destino (ej: ./wt/feature-x)"
          className="flex-1 min-w-52 rounded-lg border border-[#2C3037] bg-[#1E2127] px-3 py-1.5 text-sm focus:outline-none focus:border-[#4C9DFF]/50" />
        <Button variant="outline" size="sm" onClick={() => void add()}><Plus className="mr-1 h-3.5 w-3.5" /> Crear</Button>
      </div>

      {error && <div className="border-b border-[#2C3037] bg-[#FF6B6B]/10 px-4 py-2 text-xs text-[#FFA8A8]">{error}</div>}

      <ScrollArea className="flex-1">
        {items.length === 0 && <div className="p-4 text-xs text-[#9BA1AA]">Sin worktrees. Creá uno para que el agente trabaje aislado en una rama.</div>}
        {items.map((wt) => (
          <div key={wt.path} className="flex items-center gap-3 border-b border-[#2C3037] px-4 py-2.5">
            <div className="min-w-0 flex-1">
              <div className="truncate font-mono text-xs text-[#EDEEF0]">{wt.path}</div>
              <div className="truncate text-[10px] text-[#9BA1AA]">{wt.branch || '(detached)'}{wt.head ? ` · ${wt.head.slice(0, 8)}` : ''}</div>
            </div>
            <Button variant="ghost" size="sm" onClick={() => void remove(wt)} title="Quitar"><Trash2 className="h-3.5 w-3.5 text-[#FFA8A8]" /></Button>
          </div>
        ))}
      </ScrollArea>
    </div>
  )
}
