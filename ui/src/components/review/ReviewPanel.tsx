import { useCallback, useEffect, useState } from 'react'
import { useAppStore } from '@/store/app-store'
import { checkpointsApi, checkpointDiffApi, checkpointRestoreApi, type Checkpoint } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { GitCompare, RefreshCw, RotateCcw, Loader2, FileText } from 'lucide-react'

export function ReviewPanel() {
  const { activeSession } = useAppStore()
  const [items, setItems] = useState<Checkpoint[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [sel, setSel] = useState<Checkpoint | null>(null)
  const [diff, setDiff] = useState<string>('')
  const [busy, setBusy] = useState(false)

  const load = useCallback(async () => {
    if (!activeSession) return
    setLoading(true); setError(null)
    try {
      const r = await checkpointsApi(activeSession)
      setItems([...r.checkpoints].reverse())
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [activeSession])

  useEffect(() => { void load() }, [load])

  const open = useCallback(async (c: Checkpoint) => {
    setSel(c); setDiff('')
    try {
      const r = await checkpointDiffApi(c.id, activeSession)
      setDiff(r.ok ? (r.diff || '(sin cambios respecto al archivo actual)') : `error: ${r.error}`)
    } catch (e) {
      setDiff(`error: ${(e as Error).message}`)
    }
  }, [activeSession])

  const restore = useCallback(async (c: Checkpoint) => {
    if (!confirm(`¿Restaurar ${c.path} a esta versión?`)) return
    setBusy(true)
    try {
      const r = await checkpointRestoreApi(activeSession, c.id)
      if (!r.ok) setError(r.error || 'falló')
      await open(c)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }, [activeSession, open])

  const renderDiff = (text: string) =>
    text.split('\n').map((line, i) => {
      const cls = line.startsWith('+++') || line.startsWith('---')
        ? 'text-[#9BA1AA]'
        : line.startsWith('+') ? 'bg-[#3DDC84]/10 text-[#8EE6B4]'
        : line.startsWith('-') ? 'bg-[#FF6B6B]/10 text-[#FFA8A8]'
        : line.startsWith('@@') ? 'text-[#8CC1FF]'
        : 'text-[#C9CDD3]'
      return (
        <div key={i} className={`whitespace-pre px-2 font-mono text-xs leading-5 ${cls}`}>{line || ' '}</div>
      )
    })

  return (
    <div className="flex h-full">
      <div className="w-80 shrink-0 border-r border-[#2C3037] flex flex-col">
        <div className="flex items-center gap-2 border-b border-[#2C3037] px-3 py-2">
          <GitCompare className="h-4 w-4 text-[#4C9DFF]" />
          <span className="text-sm font-medium">Cambios</span>
          <span className="ml-auto text-xs text-[#9BA1AA]">{items.length}</span>
          <Button variant="ghost" size="icon" aria-label="Recargar" onClick={() => void load()}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          </Button>
        </div>
        {error && <div className="border-b border-[#2C3037] bg-[#FF6B6B]/10 px-3 py-2 text-xs text-[#FFA8A8]">{error}</div>}
        <ScrollArea className="flex-1">
          {items.length === 0 && (
            <div className="p-4 text-xs text-[#9BA1AA]">
              Sin checkpoints. Se crean automáticamente antes de cada escritura/edición del agente.
            </div>
          )}
          {items.map((c) => (
            <button
              key={c.id}
              onClick={() => void open(c)}
              className={`flex w-full flex-col items-start gap-0.5 border-b border-[#2C3037] px-3 py-2 text-left hover:bg-[#22262D] ${sel?.id === c.id ? 'bg-[#22262D]' : ''}`}
            >
              <span className="flex items-center gap-1.5 text-xs text-[#EDEEF0]">
                <FileText className="h-3 w-3 shrink-0 text-[#9BA1AA]" />
                <span className="truncate">{c.path.split(/[\\/]/).pop()}</span>
              </span>
              <span className="truncate pl-4 text-[10px] text-[#9BA1AA]">{c.path}</span>
              <span className="pl-4 text-[10px] text-[#6B7280]">{c.ts}</span>
            </button>
          ))}
        </ScrollArea>
      </div>

      <div className="flex flex-1 flex-col overflow-hidden">
        {sel ? (
          <>
            <div className="flex items-center gap-2 border-b border-[#2C3037] px-3 py-2">
              <span className="truncate font-mono text-xs text-[#EDEEF0]">{sel.path}</span>
              <span className="ml-auto text-[10px] text-[#9BA1AA]">{sel.id}</span>
              <Button variant="outline" size="sm" disabled={busy} onClick={() => void restore(sel)}>
                <RotateCcw className="mr-1 h-3.5 w-3.5" /> Restaurar
              </Button>
            </div>
            <ScrollArea className="flex-1 bg-[#15171B]">
              <div className="py-2">{renderDiff(diff)}</div>
            </ScrollArea>
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-sm text-[#9BA1AA]">
            Elegí un checkpoint para ver el diff contra el archivo actual.
          </div>
        )}
      </div>
    </div>
  )
}
