import { useCallback, useEffect, useState } from 'react'
import { useAppStore } from '@/store/app-store'
import { questionsApi, questionAnswerApi, questionCancelApi, questionClearApi, type Question } from '@/lib/api'
import { Button } from '@/components/ui/button'
import { ScrollArea } from '@/components/ui/scroll-area'
import { MessageCircleQuestion, RefreshCw, Loader2, Check, X, Trash2, Send } from 'lucide-react'

const edad = (ts: number): string => {
  const s = Math.max(0, Math.round(Date.now() / 1000 - ts))
  if (s < 60) return `hace ${s}s`
  if (s < 3600) return `hace ${Math.floor(s / 60)}m`
  return `hace ${Math.floor(s / 3600)}h`
}

export function QuestionsPanel() {
  const { activeSession } = useAppStore()
  const [items, setItems] = useState<Question[]>([])
  const [loading, setLoading] = useState(false)
  const [selId, setSelId] = useState<string>('')
  const [texto, setTexto] = useState('')
  const [marcadas, setMarcadas] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [clearing, setClearing] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await questionsApi(activeSession)
      setItems(r.questions)
      setError(null)
      setSelId((prev) => (prev && r.questions.some((q) => q.id === prev)) ? prev : (r.questions[0]?.id ?? ''))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [activeSession])

  useEffect(() => { void load() }, [load])

  // El turno del agente queda esperando: si el usuario tarda, la pregunta
  // aparece sola sin tener que recargar a mano.
  useEffect(() => {
    const iv = setInterval(() => { void load() }, 3000)
    return () => clearInterval(iv)
  }, [load])

  const sel = items.find((q) => q.id === selId) ?? null

  useEffect(() => { setTexto(''); setMarcadas([]); setError(null) }, [selId])

  const responder = async (valor: string | string[]) => {
    if (!sel || busy) return
    setBusy(true); setError(null)
    try {
      await questionAnswerApi(sel.id, valor)
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const cancelar = async () => {
    if (!sel || busy) return
    setBusy(true); setError(null)
    try {
      await questionCancelApi(sel.id)
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const limpiar = async () => {
    if (clearing) return
    setClearing(true); setError(null)
    try {
      await questionClearApi()
      setSelId('')
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setClearing(false)
    }
  }

  const toggle = (op: string) => {
    setMarcadas((prev) => prev.includes(op) ? prev.filter((o) => o !== op) : [...prev, op])
  }

  return (
    <div className="flex h-full">
      <div className="w-80 shrink-0 border-r border-[#2C3037] flex flex-col">
        <div className="flex items-center gap-2 border-b border-[#2C3037] px-3 py-2">
          <MessageCircleQuestion className="h-4 w-4 text-[#4C9DFF]" />
          <span className="text-sm font-medium">Preguntas</span>
          <span className="ml-auto text-xs text-[#9BA1AA]">{items.length}</span>
          <Button variant="ghost" size="icon" aria-label="Recargar" disabled={loading} onClick={() => void load()}>
            {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
          </Button>
        </div>
        {error && <div className="border-b border-[#2C3037] bg-[#FF6B6B]/10 px-3 py-1.5 text-[10px] text-[#FFA8A8]">{error}</div>}
        <ScrollArea className="flex-1">
          {items.length === 0 && !loading && (
            <div className="p-4 text-xs text-[#9BA1AA]">
              Sin preguntas pendientes. Cuando el agente necesite una decisión tuya (elegir entre caminos,
              un dato que solo vos tenés) la deja acá y el turno espera la respuesta.
            </div>
          )}
          {items.map((q) => (
            <button key={q.id} onClick={() => setSelId(q.id)}
              className={`flex w-full flex-col items-start gap-0.5 border-b border-[#2C3037] px-3 py-2 text-left hover:bg-[#22262D] ${selId === q.id ? 'bg-[#22262D]' : ''}`}>
              <span className="flex w-full items-center gap-1.5">
                {q.header && <span className="shrink-0 rounded bg-[#4C9DFF]/15 px-1.5 py-0.5 text-[10px] text-[#8CC1FF]">{q.header}</span>}
                <span className="truncate text-xs text-[#EDEEF0]">{q.question}</span>
              </span>
              <span className="text-[10px] text-[#9BA1AA]">
                {q.options.length > 0 ? `${q.options.length} opción(es)` : 'respuesta libre'} · {edad(q.created_at)}
              </span>
            </button>
          ))}
        </ScrollArea>
        <div className="border-t border-[#2C3037] p-2">
          <Button variant="ghost" size="sm" className="w-full" disabled={clearing} onClick={() => void limpiar()}>
            {clearing ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Trash2 className="mr-1 h-3.5 w-3.5" />} Limpiar resueltas
          </Button>
        </div>
      </div>

      <div className="flex flex-1 flex-col overflow-hidden">
        {sel ? (
          <>
            <div className="flex items-center gap-2 border-b border-[#2C3037] px-3 py-2">
              <span className="truncate font-mono text-xs text-[#EDEEF0]">{sel.id}</span>
              <span className="shrink-0 text-[10px] text-[#9BA1AA]">sesión {sel.session_id || '—'} · {edad(sel.created_at)}</span>
              <div className="ml-auto flex gap-2">
                <Button variant="outline" size="sm" disabled={busy} onClick={() => void cancelar()}>
                  <X className="mr-1 h-3.5 w-3.5" /> Cancelar
                </Button>
              </div>
            </div>
            <ScrollArea className="flex-1 bg-[#15171B]">
              <div className="space-y-3 p-4">
                {sel.header && <div className="text-xs uppercase tracking-wide text-[#9BA1AA]">{sel.header}</div>}
                <div className="text-sm leading-relaxed text-[#EDEEF0]">{sel.question}</div>
                {sel.multiple && <div className="text-[11px] text-[#FFB86B]">Podés elegir más de una opción.</div>}

                {sel.options.length > 0 && (
                  <div className="flex flex-col gap-2 pt-1">
                    {sel.options.map((op) => {
                      const on = marcadas.includes(op)
                      return (
                        <button key={op} onClick={() => (sel.multiple ? toggle(op) : void responder(op))} disabled={busy}
                          className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors disabled:opacity-50 ${on ? 'border-[#4C9DFF]/60 bg-[#4C9DFF]/10 text-[#EDEEF0]' : 'border-[#2C3037] bg-[#1E2127] text-[#C9CDD3] hover:border-[#4C9DFF]/40 hover:bg-[#22262D]'}`}>
                          {sel.multiple
                            ? <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${on ? 'border-[#4C9DFF] bg-[#4C9DFF] text-white' : 'border-[#3A3F47]'}`}>{on && <Check className="h-3 w-3" />}</span>
                            : <Check className="h-3.5 w-3.5 shrink-0 text-[#4C9DFF]" />}
                          <span>{op}</span>
                        </button>
                      )
                    })}
                    {sel.multiple && (
                      <Button size="sm" className="self-start" disabled={busy || marcadas.length === 0} onClick={() => void responder(marcadas)}>
                        {busy ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Send className="mr-1 h-3.5 w-3.5" />} Responder ({marcadas.length})
                      </Button>
                    )}
                  </div>
                )}

                <div className="pt-2">
                  <div className="mb-1 text-[11px] text-[#9BA1AA]">
                    {sel.options.length > 0 ? 'O escribí otra respuesta:' : 'Tu respuesta:'}
                  </div>
                  <textarea
                    value={texto}
                    onChange={(e) => setTexto(e.target.value)}
                    onKeyDown={(e) => { if (e.key === 'Enter' && (e.ctrlKey || e.metaKey) && texto.trim()) { e.preventDefault(); void responder(texto.trim()) } }}
                    rows={3}
                    placeholder="Escribí la respuesta y enviá (Ctrl+Enter)…"
                    className="w-full resize-none rounded-lg border border-[#2C3037] bg-[#1E2127] px-3 py-2 text-sm text-[#EDEEF0] placeholder:text-[#9BA1AA] focus:border-[#4C9DFF]/50 focus:outline-none"
                  />
                  <div className="mt-2 flex items-center gap-2">
                    <Button size="sm" disabled={busy || !texto.trim()} onClick={() => void responder(texto.trim())}>
                      {busy ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Send className="mr-1 h-3.5 w-3.5" />} Enviar
                    </Button>
                    <span className="text-[10px] text-[#9BA1AA]">El turno del agente sigue apenas respondés.</span>
                  </div>
                </div>
              </div>
            </ScrollArea>
          </>
        ) : (
          <div className="flex flex-1 items-center justify-center px-6 text-center text-sm text-[#9BA1AA]">
            Sin preguntas pendientes: el agente no está esperando ninguna decisión tuya.
          </div>
        )}
      </div>
    </div>
  )
}
