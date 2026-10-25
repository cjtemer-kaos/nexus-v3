import { useCallback, useEffect, useState } from 'react'
import { useAppStore } from '@/store/app-store'
import {
  memoryAuditApi,
  memoryVerdictApi,
  permissionLevelsApi,
  permissionLevelSetApi,
  type LevelsInfo,
  type MemoryAudit,
  type MemoryTopNode,
  type MemoryVerdict,
} from '@/lib/api'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { Brain, RefreshCw, Loader2, ShieldCheck, Lock, Unlock, Wrench } from 'lucide-react'

const VERDICTS: { id: MemoryVerdict; label: string; hint: string; className: string }[] = [
  { id: 'helpful', label: 'útil', hint: 'esta memoria ayudó: sube en el ranking', className: 'hover:border-[#3DDC84]/60 hover:text-[#3DDC84]' },
  { id: 'outdated', label: 'vieja', hint: 'quedó desactualizada: baja pero sigue disponible', className: 'hover:border-[#FFB86B]/60 hover:text-[#FFB86B]' },
  { id: 'harmful', label: 'daña', hint: 'induce a error: se hunde en el ranking', className: 'hover:border-[#FF8A65]/60 hover:text-[#FF8A65]' },
  { id: 'wrong', label: 'falsa', hint: 'afirmación falsa: deja de viajar al prompt', className: 'hover:border-[#FF6B6B]/60 hover:text-[#FF6B6B]' },
]

function standingColor(standing: number) {
  if (standing < 0.6) return 'text-[#FF6B6B]'
  if (standing < 1) return 'text-[#FFB86B]'
  if (standing > 1) return 'text-[#3DDC84]'
  return 'text-[#9BA1AA]'
}

function Ladder({ info, session, onChanged }: { info: LevelsInfo; session: string; onChanged: () => void }) {
  const [busy, setBusy] = useState<number | null>(null)
  const [error, setError] = useState<string | null>(null)

  const pick = async (level: number | null) => {
    if (!session) { setError('elegí una sesión primero'); return }
    setBusy(level ?? -1); setError(null)
    try {
      await permissionLevelSetApi(session, level)
      onChanged()
    } catch (e) {
      setError(String(e).slice(0, 200))
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3">
      <div className="flex flex-wrap items-center gap-2">
        <ShieldCheck className="h-4 w-4 text-[#4C9DFF]" />
        <span className="text-xs font-semibold">Peldaño de esta sesión</span>
        <Badge variant={info.session_level === null ? 'muted' : 'warning'}>
          {info.session_level === null ? 'sin override' : `#${info.session_level} ${info.effective_name}`}
        </Badge>
        <span className="text-[11px] text-[#9BA1AA]">
          preset guardado: <span className="font-mono">{info.preset}</span> (#{info.preset_level})
        </span>
        {info.session_level !== null && (
          <button onClick={() => void pick(null)} disabled={busy !== null}
            className="ml-auto text-[11px] text-[#8CC1FF] underline decoration-dotted hover:text-white disabled:opacity-50">
            volver al preset
          </button>
        )}
      </div>

      <div className="mt-2 grid gap-1.5 sm:grid-cols-5">
        {info.levels.map((step) => {
          const active = info.session_level === step.level
          const isPreset = info.session_level === null && info.preset_level === step.level
          return (
            <button key={step.level} onClick={() => void pick(step.level)} disabled={busy !== null}
              title={step.hint}
              className={`rounded-lg border px-2 py-1.5 text-left transition-colors disabled:opacity-60 ${
                active ? 'border-[#4C9DFF]/70 bg-[#0B62D6]/15'
                  : isPreset ? 'border-[#3DDC84]/40 bg-[#3DDC84]/5'
                    : 'border-[#2C3037] hover:border-[#4C9DFF]/40'}`}>
              <div className="flex items-center gap-1 text-[11px] font-semibold">
                <span className="font-mono">#{step.level}</span>
                <span className="truncate">{step.name}</span>
                {busy === step.level ? <Loader2 className="ml-auto h-3 w-3 animate-spin" />
                  : step.tools ? <Wrench className="ml-auto h-3 w-3 text-[#9BA1AA]" />
                    : <Lock className="ml-auto h-3 w-3 text-[#9BA1AA]" />}
              </div>
              <div className="mt-0.5 text-[10px] leading-tight text-[#9BA1AA]">{step.hint}</div>
              <div className="mt-1 flex items-center gap-1 text-[9px] text-[#9BA1AA]">
                <span className="rounded bg-[#22262D] px-1 py-0.5 font-mono">{step.preset}</span>
                {step.confirm_mutating && <span className="rounded bg-[#FFB86B]/15 px-1 py-0.5 text-[#FFB86B]">confirm</span>}
              </div>
            </button>
          )
        })}
      </div>
      {!session && <div className="mt-2 text-[11px] text-[#FFB86B]">Sin sesión activa: el peldaño se fija por sesión.</div>}
      {error && <div className="mt-2 text-[11px] text-[#FFA8A8]">{error}</div>}
    </div>
  )
}

function TopRow({ node, onVerdict }: { node: MemoryTopNode; onVerdict: (n: MemoryTopNode, v: MemoryVerdict) => void }) {
  return (
    <div className="rounded-lg border border-[#2C3037] bg-[#1E2127] px-3 py-2">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-mono text-[10px] text-[#9BA1AA]">#{node.node_id}</span>
        <span className="min-w-0 flex-1 truncate">{node.title || <span className="text-[#9BA1AA]">(sin título en el grafo)</span>}</span>
        <span className="text-[11px] text-[#9BA1AA]">{node.recalls}× recuperada</span>
        {node.fail_rate !== null && (
          <span className={`text-[11px] ${node.fail_rate >= 0.5 ? 'text-[#FF6B6B]' : 'text-[#9BA1AA]'}`}>
            falla {Math.round(node.fail_rate * 100)}%
          </span>
        )}
        <span className={`font-mono text-[11px] ${standingColor(node.standing)}`} title="standing: 1.0 neutro, <1 castigada, >1 premiada">
          {node.standing.toFixed(2)}
        </span>
      </div>
      <div className="mt-1.5 flex flex-wrap gap-1">
        {VERDICTS.map((v) => (
          <button key={v.id} onClick={() => onVerdict(node, v.id)} title={v.hint}
            className={`rounded border border-[#2C3037] px-1.5 py-0.5 text-[10px] text-[#9BA1AA] transition-colors ${v.className}`}>
            {v.label}
          </button>
        ))}
      </div>
    </div>
  )
}

export function MemoryPanel() {
  const { activeSession } = useAppStore()
  const [days, setDays] = useState(30)
  const [report, setReport] = useState<MemoryAudit | null>(null)
  const [levels, setLevels] = useState<LevelsInfo | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try {
      const [audit, ladder] = await Promise.all([
        memoryAuditApi(days, 20),
        permissionLevelsApi(activeSession),
      ])
      setReport(audit); setLevels(ladder)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [days, activeSession])

  useEffect(() => { void load() }, [load])

  const verdict = async (node: MemoryTopNode, v: MemoryVerdict) => {
    setNotice(null)
    try {
      const res = await memoryVerdictApi(node.node_id, v, `desde UI: ${node.title || node.node_id}`)
      setNotice(`#${node.node_id} → ${v}: standing ${res.standing.toFixed(2)}`)
      setReport((prev) => prev && {
        ...prev,
        top_recalled: prev.top_recalled.map((n) => n.node_id === node.node_id ? { ...n, standing: res.standing } : n),
      })
    } catch (e) {
      setNotice(`no se pudo guardar el veredicto: ${String(e).slice(0, 160)}`)
    }
  }

  const totals = report?.totals
  const statuses = Object.entries(report?.outcomes_by_status ?? {})

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-3">
        <Brain className="h-4 w-4 text-[#4C9DFF]" />
        <span className="text-sm font-medium">Memoria y alcance</span>
        <div className="ml-3 flex gap-1">
          {[7, 30, 90].map((d) => (
            <button key={d} onClick={() => setDays(d)}
              className={`rounded-md px-2 py-0.5 text-xs ${days === d ? 'bg-[#2C3037] text-[#EDEEF0]' : 'text-[#9BA1AA] hover:text-[#EDEEF0]'}`}>
              {d} d
            </button>
          ))}
        </div>
        <Button variant="ghost" size="icon" aria-label="Recargar" className="ml-auto" disabled={loading} onClick={() => void load()}>
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>

      {error && (
        <div className="flex items-center gap-2 border-b border-[#FF6B6B]/30 bg-[#FF6B6B]/10 px-4 py-2 text-xs text-[#FFA8A8]">
          <span className="flex-1">No se pudo leer la memoria: {error}</span>
          <button onClick={() => void load()} className="underline hover:text-white">Reintentar</button>
        </div>
      )}
      {notice && (
        <div className="border-b border-[#2C3037] bg-[#1E2127] px-4 py-1.5 text-[11px] text-[#9BA1AA]">{notice}</div>
      )}

      <ScrollArea className="flex-1">
        <div className="space-y-4 p-4">
          {levels ? <Ladder info={levels} session={activeSession} onChanged={() => void load()} />
            : !error && <div className="text-xs text-[#9BA1AA]">Cargando peldaños…</div>}

          <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
            <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3">
              <div className="text-[11px] text-[#9BA1AA]">Nodos inyectados</div>
              <div className="text-xl font-semibold">{(totals?.recalls ?? 0).toLocaleString()}</div>
            </div>
            <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3">
              <div className="text-[11px] text-[#9BA1AA]">Turnos con memoria</div>
              <div className="text-xl font-semibold">{totals?.turns_with_memory ?? 0}</div>
            </div>
            <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3">
              <div className="text-[11px] text-[#9BA1AA]">Turnos cerrados</div>
              <div className="text-xl font-semibold">{totals?.with_outcome ?? 0}</div>
            </div>
            <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3">
              <div className="text-[11px] text-[#9BA1AA]">Veredictos</div>
              <div className="text-xl font-semibold">{totals?.verdicts ?? 0}</div>
            </div>
          </div>

          {statuses.length > 0 && (
            <div className="flex flex-wrap items-center gap-1.5 text-[11px] text-[#9BA1AA]">
              <span>Resultado de los turnos:</span>
              {statuses.map(([status, n]) => (
                <Badge key={status} variant={status === 'ok' ? 'success' : 'secondary'}>{status} · {n}</Badge>
              ))}
            </div>
          )}

          <div>
            <div className="text-[11px] uppercase tracking-widest text-[#9BA1AA]">Memoria que más viajó</div>
            <div className="mt-2 space-y-1.5">
              {(report?.top_recalled ?? []).length === 0 && !loading && (
                <div className="text-xs text-[#9BA1AA]">
                  {report && report.days ? 'Sin recalls registrados en la ventana: la memoria se anota por turno.' : 'Sin datos aún…'}
                </div>
              )}
              {(report?.top_recalled ?? []).map((n) => <TopRow key={n.node_id} node={n} onVerdict={verdict} />)}
            </div>
          </div>

          <div>
            <div className="text-[11px] uppercase tracking-widest text-[#9BA1AA]">
              Castigadas por veredictos {report && <span className="normal-case tracking-normal">(no viajan con standing &lt; {report.min_standing.toFixed(2)})</span>}
            </div>
            <div className="mt-2 space-y-1.5">
              {(report?.penalized ?? []).length === 0 && <div className="text-xs text-[#9BA1AA]">Ninguna memoria por debajo del piso.</div>}
              {(report?.penalized ?? []).map((n) => (
                <div key={n.node_id} className="flex items-center gap-2 rounded-lg border border-[#FF6B6B]/25 bg-[#FF6B6B]/5 px-3 py-1.5 text-xs">
                  <Unlock className="h-3 w-3 text-[#FF6B6B]" />
                  <span className="font-mono text-[10px] text-[#9BA1AA]">#{n.node_id}</span>
                  <span className="min-w-0 flex-1 truncate">{n.title || '(sin título en el grafo)'}</span>
                  <span className="font-mono text-[#FF6B6B]">{n.standing.toFixed(2)}</span>
                </div>
              ))}
            </div>
          </div>

          {report?.db && <div className="text-[10px] text-[#9BA1AA]">ledger: <span className="font-mono">{report.db}</span></div>}
        </div>
      </ScrollArea>
    </div>
  )
}
