import * as Dialog from '@radix-ui/react-dialog'
import { ShieldAlert, X, RefreshCw, Eraser, CheckCircle2 } from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import { useAppStore } from '@/store/app-store'
import {
  purgeSessionsApi,
  sessionAuditApi,
  type AuditoriaSesiones,
  type HallazgoAuditoria,
  type PlanPurga,
} from '@/lib/api'

const ETIQUETAS: Record<string, string> = {
  orphan_messages: 'Mensajes sin sesión',
  dangling_parent: 'Sesión con padre borrado',
  fts_mismatch: 'Índice de búsqueda desincronizado',
  empty_sessions: 'Sesiones sin mensajes',
  orphan_compaction_state: 'Estado de compactación huérfano',
  orphan_todos: 'Listas de tareas huérfanas',
  orphan_goals: 'Objetivos huérfanos',
  orphan_approval_grants: 'Permisos de sesiones borradas',
  orphan_checkpoints: 'Checkpoints de sesiones borradas',
  orphan_git_checkpoints: 'Repos git de sesiones borradas',
  orphan_spill: 'Volcados de herramientas huérfanos',
  legacy_spill_files: 'Volcados viejos sin dueño',
  orphan_events: 'Eventos de sesiones borradas',
}

const NOMBRES_PLAN: Record<string, string> = {
  messages: 'mensajes',
  todos: 'listas de tareas',
  goals: 'objetivos',
  compaction_state: 'estados de compactación',
  session_fts: 'filas de búsqueda',
  approval_grants: 'permisos',
  checkpoints: 'carpetas de checkpoints',
  git_checkpoints: 'repos de checkpoints',
  spill: 'volcados de herramientas',
  spill_legacy: 'volcados viejos',
  events: 'eventos',
}

function kb(bytes: number) {
  if (!bytes) return '0 KB'
  return bytes >= 1024 * 1024
    ? `${(bytes / (1024 * 1024)).toFixed(1)} MB`
    : `${Math.round(bytes / 1024)} KB`
}

function color(sev: HallazgoAuditoria['severity']) {
  if (sev === 'error') return '#FF6B6B'
  if (sev === 'warn') return '#FFB86B'
  return '#9BA1AA'
}

function FilaHallazgo({ h }: { h: HallazgoAuditoria }) {
  return (
    <div className="flex items-start gap-2 rounded-lg border border-[#2C3037] bg-[#15171B] px-3 py-2">
      <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full" style={{ background: color(h.severity) }} />
      <div className="min-w-0 flex-1">
        <div className="flex items-baseline gap-2">
          <span className="text-xs font-medium text-[#EDEEF0]">{ETIQUETAS[h.code] ?? h.code}</span>
          <span className="ml-auto font-mono text-[11px] tabular-nums" style={{ color: color(h.severity) }}>
            {h.count}
          </span>
        </div>
        <p className="mt-0.5 text-[11px] leading-snug text-[#9BA1AA]">{h.detail}</p>
      </div>
    </div>
  )
}

export function AuditDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const loadSessions = useAppStore((s) => s.loadSessions)
  const [audit, setAudit] = useState<AuditoriaSesiones | null>(null)
  const [plan, setPlan] = useState<PlanPurga | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [aviso, setAviso] = useState<string | null>(null)
  const [cargando, setCargando] = useState(false)
  const [limpiando, setLimpiando] = useState(false)
  const [incluirEventos, setIncluirEventos] = useState(false)
  const [incluirViejos, setIncluirViejos] = useState(false)

  const cargar = useCallback(async () => {
    setCargando(true)
    setError(null)
    setAviso(null)
    try {
      setAudit(await sessionAuditApi())
      setPlan(null)
    } catch (e) {
      setError((e as Error).message ?? String(e))
    } finally {
      setCargando(false)
    }
  }, [])

  useEffect(() => {
    if (open) void cargar()
  }, [open, cargar])

  const revisar = async () => {
    setCargando(true)
    setError(null)
    setAviso(null)
    try {
      const p = await purgeSessionsApi({ events: incluirEventos, legacySpill: incluirViejos })
      setPlan(p)
      const total = Object.values(p.plan).reduce((a, b) => a + b, 0)
      setAviso(total === 0 ? 'No hay nada que limpiar.' : `Se limpiarían ${total} elementos (${kb(p.bytes)}).`)
    } catch (e) {
      setError((e as Error).message ?? String(e))
    } finally {
      setCargando(false)
    }
  }

  const limpiar = async () => {
    setLimpiando(true)
    setError(null)
    setAviso(null)
    try {
      const p = await purgeSessionsApi({ apply: true, events: incluirEventos, legacySpill: incluirViejos })
      const total = Object.values(p.deleted ?? {}).reduce((a, b) => a + b, 0)
      setAviso(`Limpieza aplicada: ${total} elementos (${kb(p.bytes)}).`)
      if (p.errors?.length) setError(p.errors.join(' · '))
      setPlan(null)
      await loadSessions()
      setAudit(await sessionAuditApi())
    } catch (e) {
      setError((e as Error).message ?? String(e))
    } finally {
      setLimpiando(false)
    }
  }

  const pendientes = (audit?.findings ?? []).filter((f) => f.count > 0 && f.severity !== 'info')
  const informativos = (audit?.findings ?? []).filter((f) => f.count > 0 && f.severity === 'info')
  const hayAlgo = pendientes.length > 0

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[80] bg-black/60 backdrop-blur-sm" />
        <Dialog.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-1/2 z-[90] flex max-h-[86vh] w-[94vw] max-w-[640px] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-[#2C3037] bg-[#1E2127] shadow-2xl focus:outline-none"
        >
          <div className="flex shrink-0 items-center justify-between border-b border-[#2C3037] px-5 py-4">
            <Dialog.Title className="flex items-center gap-2 text-sm font-semibold text-[#EDEEF0]">
              <ShieldAlert className="h-4 w-4 text-[#FFB86B]" /> Auditoría de sesiones
            </Dialog.Title>
            <div className="flex items-center gap-1">
              <button
                onClick={() => void cargar()}
                aria-label="Volver a auditar"
                title="Volver a auditar"
                className="rounded-lg p-1.5 text-[#9BA1AA] hover:bg-[#2C3037] hover:text-[#EDEEF0]"
              >
                <RefreshCw className={`h-4 w-4 ${cargando ? 'animate-spin' : ''}`} />
              </button>
              <Dialog.Close asChild>
                <button aria-label="Cerrar auditoría" className="rounded-lg p-1.5 text-[#9BA1AA] hover:bg-[#2C3037] hover:text-[#EDEEF0]">
                  <X className="h-4 w-4" />
                </button>
              </Dialog.Close>
            </div>
          </div>

          <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-5 py-4">
            {error && (
              <div className="rounded-lg border border-[#FF6B6B]/40 bg-[#FF6B6B]/10 px-3 py-2 text-xs text-[#FFA8A8]">{error}</div>
            )}

            {audit && (
              <>
                <div className="grid grid-cols-4 gap-2">
                  {[
                    { k: 'Sesiones', v: audit.sessions, c: '#EDEEF0' },
                    { k: 'Mensajes', v: audit.messages, c: '#EDEEF0' },
                    { k: 'Huérfanos', v: audit.totals.warnings, c: audit.totals.warnings ? '#FFB86B' : '#3DDC84' },
                    { k: 'Espacio', v: kb(audit.totals.orphan_bytes), c: '#EDEEF0' },
                  ].map((x) => (
                    <div key={x.k} className="rounded-lg border border-[#2C3037] bg-[#15171B] px-3 py-2">
                      <div className="text-[10px] uppercase tracking-wider text-[#6B7280]">{x.k}</div>
                      <div className="font-mono text-sm tabular-nums" style={{ color: x.c }}>{x.v}</div>
                    </div>
                  ))}
                </div>

                {audit.totals.errors > 0 && (
                  <div className="rounded-lg border border-[#FF6B6B]/40 bg-[#FF6B6B]/10 px-3 py-2 text-xs text-[#FFA8A8]">
                    {audit.totals.errors} inconsistencia(s) reales: la base tiene filas que no deberían existir.
                  </div>
                )}

                {!hayAlgo && (
                  <div className="flex items-center gap-2 rounded-lg border border-[#3DDC84]/30 bg-[#3DDC84]/10 px-3 py-2 text-xs text-[#3DDC84]">
                    <CheckCircle2 className="h-3.5 w-3.5" /> Sin huérfanos de borrados. No hay nada que limpiar.
                  </div>
                )}

                {pendientes.length > 0 && (
                  <div className="space-y-1.5">
                    <div className="text-[11px] font-semibold uppercase tracking-wider text-[#9BA1AA]">
                      Quedó atrás de sesiones borradas
                    </div>
                    {pendientes.map((h) => <FilaHallazgo key={h.code} h={h} />)}
                  </div>
                )}

                {informativos.length > 0 && (
                  <div className="space-y-1.5">
                    <div className="text-[11px] font-semibold uppercase tracking-wider text-[#9BA1AA]">Informativo</div>
                    {informativos.map((h) => <FilaHallazgo key={h.code} h={h} />)}
                  </div>
                )}

                <div className="space-y-2 rounded-lg border border-[#2C3037] bg-[#15171B] px-3 py-2.5">
                  <label className="flex items-start gap-2 text-[11px] text-[#9BA1AA]">
                    <input
                      type="checkbox"
                      checked={incluirViejos}
                      onChange={(e) => { setIncluirViejos(e.target.checked); setPlan(null) }}
                      className="mt-0.5 accent-[#4C9DFF]"
                    />
                    <span>Incluir volcados viejos sin dueño (formato anterior, no se pueden atribuir a una sesión).</span>
                  </label>
                  <label className="flex items-start gap-2 text-[11px] text-[#9BA1AA]">
                    <input
                      type="checkbox"
                      checked={incluirEventos}
                      onChange={(e) => { setIncluirEventos(e.target.checked); setPlan(null) }}
                      className="mt-0.5 accent-[#4C9DFF]"
                    />
                    <span>
                      Incluir el log de eventos de sesiones borradas. Es la traza de lo que pasó: borrarlo
                      libera espacio pero pierde la evidencia.
                    </span>
                  </label>
                </div>

                {plan && (
                  <div className="rounded-lg border border-[#4C9DFF]/30 bg-[#4C9DFF]/5 px-3 py-2">
                    <div className="text-[11px] font-semibold text-[#4C9DFF]">Plan</div>
                    <ul className="mt-1 space-y-0.5">
                      {Object.entries(plan.plan).filter(([, n]) => n > 0).map(([k, n]) => (
                        <li key={k} className="flex justify-between text-[11px] text-[#9BA1AA]">
                          <span>{NOMBRES_PLAN[k] ?? k}</span>
                          <span className="font-mono tabular-nums text-[#EDEEF0]">{n}</span>
                        </li>
                      ))}
                      {Object.values(plan.plan).every((n) => !n) && (
                        <li className="text-[11px] text-[#9BA1AA]">Nada que limpiar.</li>
                      )}
                    </ul>
                  </div>
                )}

                {aviso && !plan && (
                  <div className="rounded-lg border border-[#3DDC84]/30 bg-[#3DDC84]/10 px-3 py-2 text-xs text-[#3DDC84]">{aviso}</div>
                )}
              </>
            )}

            {!audit && !error && (
              <div className="py-8 text-center text-xs text-[#9BA1AA]">Auditando la base…</div>
            )}
          </div>

          <div className="flex shrink-0 items-center justify-between gap-2 border-t border-[#2C3037] px-5 py-3">
            <span className="text-[10px] text-[#6B7280]">
              {audit?.checked_at ? `Revisado ${audit.checked_at.replace('T', ' ').slice(0, 19)}` : 'Sólo lectura hasta que apliques'}
            </span>
            <div className="flex items-center gap-2">
              <button
                onClick={() => void revisar()}
                disabled={cargando || limpiando}
                className="rounded-lg border border-[#2C3037] px-3 py-1.5 text-xs text-[#9BA1AA] transition-colors hover:border-[#4C9DFF]/40 hover:text-[#EDEEF0] disabled:opacity-50"
              >
                Revisar
              </button>
              <button
                onClick={() => void limpiar()}
                disabled={limpiando || cargando || !hayAlgo}
                className="flex items-center gap-1.5 rounded-lg bg-[#4C9DFF] px-3 py-1.5 text-xs font-medium text-[#0F1115] transition-opacity hover:opacity-90 disabled:opacity-40"
              >
                <Eraser className="h-3.5 w-3.5" />
                {limpiando ? 'Limpiando…' : 'Limpiar huérfanos'}
              </button>
            </div>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
