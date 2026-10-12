import { useCallback, useEffect, useState } from 'react'
import { useAppStore } from '@/store/app-store'
import { approvalsApi, approvalApproveApi, approvalRejectApi, approvalClearApi, type Approval } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { ShieldCheck, RefreshCw, Loader2, Check, X, Trash2 } from 'lucide-react'

export function ApprovalsPanel() {
  const { activeSession } = useAppStore()
  const [items, setItems] = useState<Approval[]>([])
  const [mode, setMode] = useState('')
  const [loading, setLoading] = useState(false)
  const [sel, setSel] = useState<Approval | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await approvalsApi(activeSession)
      setItems(r.approvals.filter((a) => a.status === 'pending'))
      setMode(r.mode)
    } catch { /* best-effort */ } finally {
      setLoading(false)
    }
  }, [activeSession])

  useEffect(() => { void load() }, [load])

  const act = async (id: string, approve: boolean) => {
    setBusy(true); setError(null)
    try {
      if (approve) await approvalApproveApi(id)
      else await approvalRejectApi(id)
      setSel(null)
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const clear = async () => {
    try { await approvalClearApi(); setSel(null); await load() }
    catch (e) { setError((e as Error).message) }
  }

  const renderPreview = (text: string) =>
    text.split('\n').map((line, i) => {
      const cls = line.startsWith('+') && !line.startsWith('+++') ? 'bg-[#3DDC84]/10 text-[#8EE6B4]'
        : line.startsWith('-') && !line.startsWith('---') ? 'bg-[#FF6B6B]/10 text-[#FFA8A8]'
        : line.startsWith('@@') ? 'text-[#8CC1FF]' : 'text-[#C9CDD3]'
      return <div key={i} className={`whitespace-pre px-2 font-mono text-xs leading-5 ${cls}`}>{line || ' '}</div>
    })

  return (
    <div className="flex h-full">
      <div className="w-80 shrink-0 border-r border-[#2C3037] flex flex-col">
        <div className="flex items-center gap-2 border-b border-[#2C3037] px-3 py-2">
          <ShieldCheck className="h-4 w-4 text-[#4C9DFF]" />
          <span className="text-sm font-medium">Aprobaciones</span>
          <span className="ml-auto text-xs text-[#9BA1AA]">{items.length}</span>
          <Button variant="ghost" size="icon" aria-label="Recargar" onClick={() => void load()}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          </Button>
        </div>
        <div className="border-b border-[#2C3037] px-3 py-1.5 text-[10px] text-[#9BA1AA]">modo auto_apply: <span className="text-[#8CC1FF]">{mode || '—'}</span></div>
        {error && <div className="border-b border-[#2C3037] bg-[#FF6B6B]/10 px-3 py-1.5 text-[10px] text-[#FFA8A8]">{error}</div>}
        <ScrollArea className="flex-1">
          {items.length === 0 && (
            <div className="p-4 text-xs text-[#9BA1AA]">
              Sin pendientes. {mode === 'approve' ? 'El agente encola write/edit acá para que los apruebes.' : 'Cambiá agent_loop.auto_apply a "approve" para revisar antes de aplicar.'}
            </div>
          )}
          {items.map((a) => (
            <button key={a.id} onClick={() => setSel(a)}
              className={`flex w-full flex-col items-start gap-0.5 border-b border-[#2C3037] px-3 py-2 text-left hover:bg-[#22262D] ${sel?.id === a.id ? 'bg-[#22262D]' : ''}`}>
              <span className="flex items-center gap-1.5 text-xs text-[#EDEEF0]">
                <span className="rounded bg-[#FFB86B]/15 px-1.5 py-0.5 text-[10px] text-[#FFB86B]">{a.tool}</span>
                <span className="truncate">{String((a.args as { path?: string }).path || '')}</span>
              </span>
              <span className="text-[10px] text-[#9BA1AA]">{a.id} · sesión {a.session_id}</span>
            </button>
          ))}
        </ScrollArea>
        <div className="border-t border-[#2C3037] p-2">
          <Button variant="ghost" size="sm" className="w-full" onClick={() => void clear()}>
            <Trash2 className="mr-1 h-3.5 w-3.5" /> Limpiar resueltas
          </Button>
        </div>
      </div>

      <div className="flex flex-1 flex-col overflow-hidden">
        {sel ? (
          <>
            <div className="flex items-center gap-2 border-b border-[#2C3037] px-3 py-2">
              <span className="font-mono text-xs text-[#EDEEF0]">{sel.tool} · {String((sel.args as { path?: string }).path || '')}</span>
              <div className="ml-auto flex gap-2">
                <Button variant="outline" size="sm" disabled={busy} onClick={() => void act(sel.id, false)}><X className="mr-1 h-3.5 w-3.5" /> Rechazar</Button>
                <Button size="sm" disabled={busy} onClick={() => void act(sel.id, true)}><Check className="mr-1 h-3.5 w-3.5" /> Aprobar</Button>
              </div>
            </div>
            <ScrollArea className="flex-1 bg-[#15171B]"><div className="py-2">{renderPreview(sel.preview)}</div></ScrollArea>
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center text-sm text-[#9BA1AA]">Elegí una propuesta para ver el cambio y aprobar/rechazar.</div>
        )}
      </div>
    </div>
  )
}
