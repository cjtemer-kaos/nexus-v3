import { useCallback, useEffect, useState } from 'react'
import { useAppStore } from '@/store/app-store'
import {
  questionsApi, questionAnswerApi, questionCancelApi,
  approvalsApi, approvalApproveApi, approvalRejectApi,
  type Question, type Approval,
} from '@/lib/api'
import { Button } from '@/components/ui/button'
import { MessageCircleQuestion, ShieldCheck, Check, X, Loader2, Send } from 'lucide-react'

const uid = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`)
const hora = () => new Date().toLocaleTimeString().slice(0, 5)

export function InlineAsks() {
  const { activeSession, addMessage } = useAppStore()
  const [preguntas, setPreguntas] = useState<Question[]>([])
  const [aprobaciones, setAprobaciones] = useState<Approval[]>([])
  const [busy, setBusy] = useState<string>('')
  const [texto, setTexto] = useState<Record<string, string>>({})
  const [marcadas, setMarcadas] = useState<Record<string, string[]>>({})
  const [error, setError] = useState<string | null>(null)

  const cargar = useCallback(async () => {
    const sid = activeSession || ''
    const [qs, ap] = await Promise.allSettled([questionsApi(sid), approvalsApi(sid)])
    if (qs.status === 'fulfilled') {
      setPreguntas(qs.value.questions)
      setError(null)
    } else {
      setError((qs.reason as Error)?.message || 'no se pudieron leer las preguntas')
    }
    if (ap.status === 'fulfilled') setAprobaciones(ap.value.approvals.filter((a) => a.status === 'pending'))
  }, [activeSession])

  useEffect(() => { void cargar() }, [cargar])

  // El turno del agente queda esperando: la tarjeta aparece sola.
  useEffect(() => {
    const iv = setInterval(() => { void cargar() }, 2500)
    return () => clearInterval(iv)
  }, [cargar])

  const responder = async (q: Question, valor: string | string[]) => {
    if (busy) return
    setBusy(q.id)
    setError(null)
    try {
      // Sin eco local: la respuesta viaja en el resultado de la tool `question`
      // y el historial la muestra ahí. Antes se agregaba un mensaje de usuario y
      // el merge lo dejaba al final del hilo, después de la réplica del agente.
      await questionAnswerApi(q.id, valor)
      setPreguntas((prev) => prev.filter((x) => x.id !== q.id))
      setTexto((prev) => ({ ...prev, [q.id]: '' }))
      setMarcadas((prev) => ({ ...prev, [q.id]: [] }))
    } catch (e) {
      setError((e as Error).message.slice(0, 200))
    } finally {
      setBusy('')
    }
  }

  const cancelar = async (q: Question) => {
    if (busy) return
    setBusy(q.id)
    try {
      await questionCancelApi(q.id)
      addMessage({
        id: uid(), role: 'tool', toolName: 'question', timestamp: hora(),
        content: 'pregunta cancelada: el agente sigue sin esa decisión y tiene que decirlo',
      })
      setPreguntas((prev) => prev.filter((x) => x.id !== q.id))
    } catch (e) {
      setError((e as Error).message.slice(0, 200))
    } finally {
      setBusy('')
    }
  }

  const decidir = async (a: Approval, aprobar: boolean) => {
    if (busy) return
    setBusy(a.id)
    setError(null)
    try {
      const r = aprobar ? await approvalApproveApi(a.id) : await approvalRejectApi(a.id)
      if (!r.ok) throw new Error(r.error || 'la acción no se aplicó')
      addMessage({
        id: uid(), role: 'tool', toolName: a.tool, timestamp: hora(),
        content: `${aprobar ? 'aprobado' : 'rechazado'}: ${a.preview || a.id}`,
      })
      setAprobaciones((prev) => prev.filter((x) => x.id !== a.id))
    } catch (e) {
      setError((e as Error).message.slice(0, 200))
    } finally {
      setBusy('')
    }
  }

  if (preguntas.length === 0 && aprobaciones.length === 0) return null

  return (
    <div className="space-y-3">
      {error && (
        <div className="rounded-xl border border-[#5A2A2A] bg-[#3A1F1F]/60 px-3 py-2 text-xs text-[#FFA8A8]">{error}</div>
      )}

      {preguntas.map((q) => {
        const sel = marcadas[q.id] ?? []
        const ocupado = busy === q.id
        return (
          <div key={q.id} className="overflow-hidden rounded-xl border border-[#4C9DFF]/40 bg-[#182233]">
            <div className="flex items-center gap-2 border-b border-[#4C9DFF]/25 px-3 py-2 text-xs">
              <MessageCircleQuestion className="h-4 w-4 shrink-0 text-[#8CC1FF]" />
              <span className="font-semibold text-[#8CC1FF]">El agente necesita tu respuesta</span>
              {q.header && <span className="rounded bg-[#4C9DFF]/20 px-1.5 py-0.5 text-[10px] text-[#8CC1FF]">{q.header}</span>}
              <span className="ml-auto text-[10px] text-[#9BA1AA]">el turno está esperando</span>
            </div>
            <div className="space-y-3 p-3">
              <div className="text-sm leading-relaxed text-[#EDEEF0]">{q.question}</div>
              {q.multiple && <div className="text-[11px] text-[#FFB86B]">Podés elegir más de una opción.</div>}

              {q.options.length > 0 && (
                <div className="flex flex-col gap-2">
                  {q.options.map((op) => {
                    const on = sel.includes(op)
                    return (
                      <button
                        key={op}
                        disabled={ocupado}
                        onClick={() => {
                          if (!q.multiple) { void responder(q, op); return }
                          setMarcadas((prev) => {
                            const cur = prev[q.id] ?? []
                            return { ...prev, [q.id]: on ? cur.filter((o) => o !== op) : [...cur, op] }
                          })
                        }}
                        className={`flex items-center gap-2 rounded-lg border px-3 py-2 text-left text-sm transition-colors disabled:opacity-50 ${on ? 'border-[#4C9DFF]/60 bg-[#4C9DFF]/10 text-[#EDEEF0]' : 'border-[#2C3037] bg-[#1E2127] text-[#C9CDD3] hover:border-[#4C9DFF]/40 hover:bg-[#22262D]'}`}
                      >
                        {q.multiple
                          ? <span className={`flex h-4 w-4 shrink-0 items-center justify-center rounded border ${on ? 'border-[#4C9DFF] bg-[#4C9DFF] text-white' : 'border-[#3A3F47]'}`}>{on && <Check className="h-3 w-3" />}</span>
                          : <Check className="h-3.5 w-3.5 shrink-0 text-[#4C9DFF]" />}
                        <span>{op}</span>
                      </button>
                    )
                  })}
                  {q.multiple && (
                    <Button size="sm" className="self-start" disabled={ocupado || sel.length === 0} onClick={() => void responder(q, sel)}>
                      {ocupado ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Send className="mr-1 h-3.5 w-3.5" />} Responder ({sel.length})
                    </Button>
                  )}
                </div>
              )}

              <div className="flex items-end gap-2">
                <textarea
                  value={texto[q.id] ?? ''}
                  onChange={(e) => setTexto((prev) => ({ ...prev, [q.id]: e.target.value }))}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
                      e.preventDefault()
                      const v = (texto[q.id] ?? '').trim()
                      if (v) void responder(q, v)
                    }
                  }}
                  rows={1}
                  placeholder={q.options.length > 0 ? 'O escribí otra respuesta…' : 'Tu respuesta…'}
                  aria-label="Respuesta a la pregunta del agente"
                  className="max-h-28 min-h-[36px] flex-1 resize-none rounded-lg border border-[#2C3037] bg-[#1E2127] px-3 py-2 text-sm text-[#EDEEF0] placeholder:text-[#9BA1AA] focus:border-[#4C9DFF]/50 focus:outline-none"
                />
                <Button size="sm" disabled={ocupado || !(texto[q.id] ?? '').trim()} onClick={() => void responder(q, (texto[q.id] ?? '').trim())}>
                  {ocupado ? <Loader2 className="h-3.5 w-3.5 animate-spin" /> : <Send className="h-3.5 w-3.5" />}
                </Button>
                <Button variant="outline" size="sm" disabled={ocupado} onClick={() => void cancelar(q)} title="El agente sigue sin esa decisión y debe decirlo">
                  <X className="h-3.5 w-3.5" />
                </Button>
              </div>
            </div>
          </div>
        )
      })}

      {aprobaciones.map((a) => (
        <div key={a.id} className="overflow-hidden rounded-xl border border-[#FFB86B]/40 bg-[#2A2216]">
          <div className="flex items-center gap-2 border-b border-[#FFB86B]/25 px-3 py-2 text-xs">
            <ShieldCheck className="h-4 w-4 shrink-0 text-[#FFB86B]" />
            <span className="font-semibold text-[#FFB86B]">Aprobación pendiente</span>
            <span className="font-mono text-[#EDEEF0]">{a.tool}</span>
          </div>
          <div className="space-y-2 p-3">
            <pre className="max-h-40 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-[#C9CDD3]">{a.preview || JSON.stringify(a.args, null, 2)}</pre>
            <div className="flex gap-2">
              <Button size="sm" disabled={busy === a.id} onClick={() => void decidir(a, true)}>
                {busy === a.id ? <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" /> : <Check className="mr-1 h-3.5 w-3.5" />} Aprobar
              </Button>
              <Button variant="outline" size="sm" disabled={busy === a.id} onClick={() => void decidir(a, false)}>
                <X className="mr-1 h-3.5 w-3.5" /> Rechazar
              </Button>
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}
