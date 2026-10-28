import { useCallback, useEffect, useState } from 'react'
import { Gauge, Pin, PinOff, RefreshCw } from 'lucide-react'
import { cn } from '@/lib/utils'
import {
  sessionContextApi, sessionPinApi, unpinSessionApi,
  type SessionContext, type SessionPin,
} from '@/lib/api'

type Props = {
  sessionId: string
  /** Cambia para releer (cada turno, al detener el stream). */
  refreshKey?: string
  onToast?: (msg: string) => void
}

const fmt = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)} mil` : String(n))

const ORDEN: { clave: keyof SessionContext['breakdown']; label: string }[] = [
  { clave: 'system', label: 'prompt fijado' },
  { clave: 'summary', label: 'resumen' },
  { clave: 'tools', label: 'herramientas' },
  { clave: 'user', label: 'usuario' },
  { clave: 'assistant', label: 'nexus' },
  { clave: 'tool', label: 'resultados' },
]

export function ContextMeter({ sessionId, refreshKey = '', onToast }: Props) {
  const [ctx, setCtx] = useState<SessionContext | null>(null)
  const [pin, setPin] = useState<SessionPin | null>(null)
  const [open, setOpen] = useState(false)
  const [cargando, setCargando] = useState(false)
  const [soltando, setSoltando] = useState(false)

  const cargar = useCallback(async () => {
    if (!sessionId) { setCtx(null); setPin(null); return }
    setCargando(true)
    try {
      const [c, p] = await Promise.all([
        sessionContextApi(sessionId).catch(() => null),
        sessionPinApi(sessionId).catch(() => null),
      ])
      setCtx(c)
      setPin(p)
    } finally {
      setCargando(false)
    }
  }, [sessionId])

  useEffect(() => { void cargar() }, [cargar, refreshKey])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') setOpen(false) }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open])

  const soltarPin = async () => {
    if (!sessionId || soltando) return
    setSoltando(true)
    try {
      await unpinSessionApi(sessionId)
      onToast?.('pin quitado: el próximo turno vuelve a armar el prompt con lo de hoy')
      await cargar()
    } catch (e) {
      onToast?.(`no se pudo quitar el pin: ${(e as Error).message.slice(0, 140)}`)
    } finally {
      setSoltando(false)
    }
  }

  const pct = Math.max(0, Math.min(1, ctx?.pct ?? 0))
  const color = pct >= (ctx?.threshold ?? 0.75) ? '#FF6B6B' : pct >= 0.5 ? '#FFB86B' : '#3DDC84'
  const pineado = Boolean(pin?.pinned)

  return (
    <div className="relative">
      <button
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-label="Uso del contexto de la sesión"
        title={
          ctx
            ? `Contexto: ${fmt(ctx.used)} de ${fmt(ctx.limit)} tokens (${(pct * 100).toFixed(0)}%)` +
              (pineado ? ' · request fijada' : '')
            : 'Uso del contexto'
        }
        className={cn(
          'flex items-center gap-1.5 rounded-md border px-2 py-1 text-[11px] transition-colors',
          pineado
            ? 'border-[#4C9DFF]/40 bg-[#4C9DFF]/10 text-[#8CC1FF]'
            : 'border-[#2C3037] bg-[#1E2127] text-[#9BA1AA] hover:text-[#EDEEF0]',
        )}
      >
        <Gauge className="h-3.5 w-3.5" style={{ color }} />
        {ctx ? <span className="tabular-nums">{Math.round(pct * 100)}%</span> : <span>—</span>}
        {pineado && <Pin className="h-3 w-3" />}
      </button>

      {open && (
        <>
          <div className="fixed inset-0 z-[60]" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-[70] mt-1 w-[22rem] rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-xs shadow-2xl">
            <div className="flex items-center gap-2">
              <span className="font-semibold text-[#EDEEF0]">Contexto del próximo turno</span>
              <button
                onClick={() => void cargar()}
                disabled={cargando}
                aria-label="Actualizar"
                title="Actualizar"
                className="ml-auto rounded-md p-1 text-[#9BA1AA] hover:bg-[#262A31] hover:text-[#EDEEF0] disabled:opacity-40"
              >
                <RefreshCw className={cn('h-3.5 w-3.5', cargando && 'animate-spin')} />
              </button>
            </div>

            {!ctx ? (
              <div className="mt-2 text-[#9BA1AA]">sin datos todavía</div>
            ) : (
              <>
                <div className="mt-2 flex items-baseline gap-2">
                  <span className="text-sm font-semibold tabular-nums text-[#EDEEF0]">{fmt(ctx.used)}</span>
                  <span className="text-[#9BA1AA]">de {fmt(ctx.limit)} tokens</span>
                  <span className="ml-auto tabular-nums" style={{ color }}>{Math.round(pct * 100)}%</span>
                </div>
                <div className="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-[#2C3037]">
                  <div className="h-full rounded-full transition-all" style={{ width: `${pct * 100}%`, background: color }} />
                </div>
                <div className="mt-1 flex justify-between text-[10px] text-[#9BA1AA]">
                  <span>compacta a {fmt(ctx.compact_at)}</span>
                  <span>{ctx.messages_replayed}/{ctx.messages_total} mensajes en ventana ({ctx.window})</span>
                </div>

                {ctx.note && (
                  <div className="mt-2 rounded-lg border border-[#FFB86B]/30 bg-[#FFB86B]/10 px-2 py-1.5 text-[10px] text-[#FFD2A0]">
                    {ctx.note}
                  </div>
                )}

                <div className="mt-3 space-y-1">
                  {ORDEN.filter((f) => ctx.breakdown[f.clave] > 0).map((f) => (
                    <div key={f.clave} className="flex items-center gap-2">
                      <span className="w-24 shrink-0 text-[#9BA1AA]">{f.label}</span>
                      <div className="h-1 flex-1 overflow-hidden rounded-full bg-[#2C3037]">
                        <div className="h-full rounded-full bg-[#4C9DFF]/70"
                             style={{ width: `${Math.min(100, (ctx.breakdown[f.clave] / Math.max(1, ctx.used)) * 100)}%` }} />
                      </div>
                      <span className="w-14 shrink-0 text-right tabular-nums text-[#C9CDD3]">{fmt(ctx.breakdown[f.clave])}</span>
                    </div>
                  ))}
                </div>

                {ctx.heaviest.length > 0 && (
                  <div className="mt-3">
                    <div className="text-[10px] uppercase tracking-wide text-[#59616D]">Lo más pesado</div>
                    <div className="mt-1 space-y-1">
                      {ctx.heaviest.map((h, i) => (
                        <div key={i} className="flex gap-2 text-[10px]">
                          <span className="w-16 shrink-0 text-[#8CC1FF]">{h.role}</span>
                          <span className="min-w-0 flex-1 truncate text-[#9BA1AA]" title={h.preview}>{h.preview || '(vacío)'}</span>
                          <span className="shrink-0 tabular-nums text-[#C9CDD3]">{fmt(h.tokens)}</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                <div className="mt-3 border-t border-[#2C3037] pt-2">
                  <div className="flex items-center gap-2">
                    <Pin className={cn('h-3.5 w-3.5', pineado ? 'text-[#8CC1FF]' : 'text-[#59616D]')} />
                    <span className="text-[#C9CDD3]">
                      {pineado ? 'Request fijada' : 'Sin fijar'}
                    </span>
                    {pineado && pin?.hash && (
                      <span className="ml-auto font-mono text-[10px] text-[#59616D]">{pin.hash}</span>
                    )}
                  </div>
                  <div className="mt-1 text-[10px] leading-relaxed text-[#9BA1AA]">
                    {pineado
                      ? `Se reanuda con el prompt y ${pin?.tools?.length ?? 0} herramientas del primer turno. Quitá el pin para volver a armar el contexto con lo de hoy.`
                      : 'El primer turno fija el prompt y el set de herramientas; los turnos siguientes los reusan.'}
                  </div>
                  {pineado && (
                    <div className="mt-1 flex flex-wrap gap-1">
                      {(pin?.tools ?? []).slice(0, 12).map((t) => (
                        <span key={t} className="rounded border border-[#2C3037] bg-[#15171B] px-1.5 py-0.5 font-mono text-[10px] text-[#9BA1AA]">{t}</span>
                      ))}
                      {(pin?.tools?.length ?? 0) > 12 && (
                        <span className="text-[10px] text-[#59616D]">+{(pin?.tools?.length ?? 0) - 12}</span>
                      )}
                    </div>
                  )}
                  {pineado && (
                    <button
                      onClick={() => void soltarPin()}
                      disabled={soltando}
                      className="mt-2 flex items-center gap-1.5 rounded-md border border-[#2C3037] px-2 py-1 text-[10px] text-[#9BA1AA] hover:border-[#FF6B6B]/40 hover:text-[#FFA8A8] disabled:opacity-50"
                    >
                      <PinOff className="h-3 w-3" /> {soltando ? 'Quitando…' : 'Quitar pin'}
                    </button>
                  )}
                </div>
              </>
            )}
          </div>
        </>
      )}
    </div>
  )
}
