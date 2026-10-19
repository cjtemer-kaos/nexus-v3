import { useCallback, useEffect, useState } from 'react'
import { boardApi, boardClaimApi, boardCreateApi, boardUpdateApi, type BoardTask } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { KanbanSquare, RefreshCw, Plus, Check, Hand, Loader2 } from 'lucide-react'

const STATUS: Record<string, string> = {
  open: 'bg-[#4C9DFF]/15 text-[#8CC1FF]',
  claimed: 'bg-[#FFB86B]/15 text-[#FFB86B]',
  in_progress: 'bg-[#FFB86B]/20 text-[#FFD2A1]',
  done: 'bg-[#3DDC84]/15 text-[#8EE6B4]',
  failed: 'bg-[#FF6B6B]/15 text-[#FFA8A8]',
  blocked: 'bg-[#B18CFF]/15 text-[#C9B6FF]',
  cancelled: 'bg-[#6B7280]/20 text-[#9BA1AA]',
}

const STATUS_LABEL: Record<string, string> = {
  open: 'abierta',
  claimed: 'reclamada',
  in_progress: 'en curso',
  done: 'hecha',
  failed: 'falló',
  blocked: 'bloqueada',
  cancelled: 'cancelada',
}

export function BoardPanel() {
  const [tasks, setTasks] = useState<BoardTask[]>([])
  const [loading, setLoading] = useState(false)
  const [title, setTitle] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busyId, setBusyId] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const r = await boardApi()
      setTasks(r.tasks)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => { void load() }, [load])

  const create = async () => {
    const t = title.trim()
    if (!t || creating) return
    setCreating(true); setError(null)
    try {
      await boardCreateApi({ title: t })
      setTitle('')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setCreating(false)
      await load()
    }
  }

  const claim = async (id: string) => {
    if (busyId) return
    setBusyId(id); setError(null)
    try {
      const r = await boardClaimApi(id)
      if (!r.ok) setError(r.error || 'no se pudo reclamar')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyId(null)
      await load()
    }
  }

  const done = async (id: string) => {
    if (busyId) return
    setBusyId(id); setError(null)
    try {
      const r = await boardUpdateApi(id, { status: 'done' })
      if (!r.ok) setError(r.error || 'no se pudo completar')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusyId(null)
      await load()
    }
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-3">
        <KanbanSquare className="h-4 w-4 text-[#4C9DFF]" />
        <span className="text-sm font-medium">Board de tareas</span>
        <span className="ml-auto text-xs text-[#9BA1AA]">{tasks.length}</span>
        <Button variant="ghost" size="icon" aria-label="Recargar" onClick={() => void load()}>
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>

      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-2">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') void create() }}
          placeholder="Nueva tarea… (Enter)"
          className="flex-1 rounded-lg border border-[#2C3037] bg-[#1E2127] px-3 py-1.5 text-sm focus:outline-none focus:border-[#4C9DFF]/50"
        />
        <Button variant="outline" size="sm" disabled={creating} onClick={() => void create()}>
          {creating ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Plus className="mr-1 h-3.5 w-3.5" />} Crear
        </Button>
      </div>

      {error && <div className="border-b border-[#2C3037] bg-[#FF6B6B]/10 px-4 py-2 text-xs text-[#FFA8A8]">{error}</div>}

      <ScrollArea className="flex-1">
        {tasks.length === 0 && <div className="p-4 text-xs text-[#9BA1AA]">Sin tareas. Creá una arriba.</div>}
        {tasks.map((t) => (
          <div key={t.id} className="flex items-center gap-3 border-b border-[#2C3037] px-4 py-2.5">
            <span className={`shrink-0 rounded px-2 py-0.5 text-[10px] font-medium ${STATUS[t.status] || STATUS.cancelled}`}>{STATUS_LABEL[t.status] || t.status}</span>
            <div className="min-w-0 flex-1">
              <div className="truncate text-sm text-[#EDEEF0]">{t.title}</div>
              <div className="truncate text-[10px] text-[#9BA1AA]">
                {t.id}{t.assignee ? ` · ${t.assignee}` : ''}{t.deps.length ? ` · deps: ${t.deps.join(',')}` : ''}{t.priority !== 3 ? ` · P${t.priority}` : ''}
              </div>
            </div>
            {t.status !== 'done' && t.status !== 'cancelled' && (
              <>
                <Button variant="ghost" size="sm" disabled={busyId === t.id} onClick={() => void claim(t.id)} title="Reclamar"><Hand className="h-3.5 w-3.5" /></Button>
                <Button variant="ghost" size="sm" disabled={busyId === t.id} onClick={() => void done(t.id)} title="Completar">{busyId === t.id ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Check className="h-3.5 w-3.5 text-[#3DDC84]" />}</Button>
              </>
            )}
          </div>
        ))}
      </ScrollArea>
    </div>
  )
}
