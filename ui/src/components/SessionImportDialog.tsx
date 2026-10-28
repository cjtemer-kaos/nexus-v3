import * as Dialog from '@radix-ui/react-dialog'
import { CheckCircle2, FileInput, RefreshCw, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useAppStore } from '@/store/app-store'
import {
  sessionImportApplyApi,
  sessionImportListApi,
  sessionImportPreviewApi,
  type ImportList,
  type ImportPreview,
  type ImportedSession,
} from '@/lib/api'

const DESCARTES: Record<string, string> = {
  tool_use: 'llamadas a herramientas',
  tool_result: 'resultados de herramientas',
  thinking: 'razonamiento',
  redacted_thinking: 'razonamiento',
  function_call: 'llamadas a herramientas',
  function_call_output: 'resultados de herramientas',
  local_shell_call: 'llamadas al shell',
  custom_tool_call: 'llamadas a herramientas',
  custom_tool_call_output: 'resultados de herramientas',
  reasoning: 'razonamiento',
  rol_developer: 'instrucciones del sistema',
  contexto_entorno: 'contexto de entorno',
  sin_texto: 'turnos sin texto',
  subagente: 'subagentes',
  linea_ilegible: 'líneas ilegibles',
}

const FUENTE: Record<string, string> = {
  claude: 'Claude',
  codex: 'Codex',
}

const fecha = (iso: string) => {
  if (!iso) return ''
  const d = new Date(iso)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString('es', { dateStyle: 'short', timeStyle: 'short' })
}

const rolLabel = (role: string) => (role === 'user' ? 'usuario' : role === 'assistant' ? 'asistente' : role)

function Descartado({ skipped }: { skipped: Record<string, number> }) {
  const filas = Object.entries(skipped).filter(([, n]) => n > 0)
  if (!filas.length) return null
  return (
    <div className="mt-2 rounded-lg border border-[#FFB86B]/25 bg-[#FFB86B]/10 px-3 py-2 text-[11px] text-[#FFD2A0]">
      <div className="font-medium">No se importa</div>
      <div className="mt-0.5 leading-snug">
        {filas.map(([k, n]) => `${n} ${DESCARTES[k] ?? k}`).join(' · ')}
      </div>
    </div>
  )
}

export function SessionImportDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const loadSessions = useAppStore((s) => s.loadSessions)
  const setActiveSession = useAppStore((s) => s.setActiveSession)

  const [lista, setLista] = useState<ImportList | null>(null)
  const [fuente, setFuente] = useState('')
  const [elegida, setElegida] = useState<ImportedSession | null>(null)
  const [preview, setPreview] = useState<ImportPreview | null>(null)
  const [proyecto, setProyecto] = useState('')
  const [cargando, setCargando] = useState(false)
  const [importando, setImportando] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [listo, setListo] = useState<string | null>(null)

  const cargarLista = useCallback(async (src = '') => {
    setCargando(true)
    setError(null)
    try {
      const r = await sessionImportListApi(src, 60)
      setLista(r)
      setElegida(null)
      setPreview(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setCargando(false)
    }
  }, [])

  useEffect(() => {
    if (!open) return
    setListo(null)
    setProyecto('')
    void cargarLista(fuente)
  }, [open, fuente, cargarLista])

  const elegir = async (s: ImportedSession) => {
    setElegida(s)
    setPreview(null)
    setError(null)
    setProyecto(`importado-${s.source}`)
    setCargando(true)
    try {
      setPreview(await sessionImportPreviewApi(s.handle, 40))
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setCargando(false)
    }
  }

  const importar = async () => {
    if (!elegida || importando) return
    setImportando(true)
    setError(null)
    try {
      const r = await sessionImportApplyApi({ handle: elegida.handle, project: proyecto.trim() })
      await loadSessions()
      setActiveSession(r.session_id)
      setListo(`${r.imported} turnos importados en «${r.title}»`)
      setTimeout(() => onOpenChange(false), 1400)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setImportando(false)
    }
  }

  const disponibles = useMemo(
    () => Object.entries(lista?.available ?? {}).filter(([, ok]) => ok).map(([k]) => k),
    [lista],
  )

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[80] bg-black/60 backdrop-blur-sm" />
        <Dialog.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-1/2 z-[90] flex max-h-[86vh] w-[96vw] max-w-[880px] -translate-x-1/2 -translate-y-1/2 flex-col overflow-hidden rounded-2xl border border-[#2C3037] bg-[#1E2127] shadow-2xl focus:outline-none"
        >
          <div className="flex shrink-0 items-center justify-between border-b border-[#2C3037] px-5 py-4">
            <Dialog.Title className="flex items-center gap-2 text-sm font-semibold text-[#EDEEF0]">
              <FileInput className="h-4 w-4 text-[#4C9DFF]" /> Importar una sesión
            </Dialog.Title>
            <div className="flex items-center gap-1">
              <button
                onClick={() => void cargarLista(fuente)}
                aria-label="Volver a buscar"
                title="Volver a buscar"
                className="rounded-lg p-1.5 text-[#9BA1AA] hover:bg-[#2C3037] hover:text-[#EDEEF0]"
              >
                <RefreshCw className={cargando ? 'h-4 w-4 animate-spin' : 'h-4 w-4'} />
              </button>
              <Dialog.Close asChild>
                <button aria-label="Cerrar" className="rounded-lg p-1.5 text-[#9BA1AA] hover:bg-[#2C3037] hover:text-[#EDEEF0]">
                  <X className="h-4 w-4" />
                </button>
              </Dialog.Close>
            </div>
          </div>

          <div className="flex min-h-0 flex-1 flex-col gap-3 overflow-hidden p-4 sm:flex-row">
            <div className="flex min-h-0 w-full flex-col rounded-xl border border-[#2C3037] bg-[#15171B] sm:w-[46%]">
              <div className="flex items-center gap-1 border-b border-[#2C3037] px-2 py-1.5">
                <button
                  onClick={() => setFuente('')}
                  className={`rounded-md px-2 py-1 text-[11px] ${fuente === '' ? 'bg-[#4C9DFF]/15 text-[#8CC1FF]' : 'text-[#9BA1AA] hover:text-[#EDEEF0]'}`}
                >
                  Todas
                </button>
                {['claude', 'codex'].map((f) => (
                  <button
                    key={f}
                    onClick={() => setFuente(f)}
                    className={`rounded-md px-2 py-1 text-[11px] ${fuente === f ? 'bg-[#4C9DFF]/15 text-[#8CC1FF]' : 'text-[#9BA1AA] hover:text-[#EDEEF0]'}`}
                  >
                    {FUENTE[f] ?? f}
                  </button>
                ))}
                <span className="ml-auto pr-1 text-[10px] text-[#59616D]">
                  {lista?.sessions.length ?? 0} sesiones
                </span>
              </div>
              <div className="min-h-0 flex-1 overflow-y-auto p-1">
                {!cargando && lista && lista.sessions.length === 0 && (
                  <div className="px-2 py-6 text-center text-[11px] leading-relaxed text-[#59616D]">
                    No hay sesiones para importar.
                    {Object.entries(lista.available).some(([, ok]) => ok) ? null : (
                      <div className="mt-1">
                        No se encontraron los directorios:
                        <div className="mt-1 font-mono text-[10px] text-[#6B7280]">
                          {Object.values(lista.roots).flat().join('\n')}
                        </div>
                      </div>
                    )}
                  </div>
                )}
                {(lista?.sessions ?? []).map((s) => (
                  <button
                    key={s.handle}
                    onClick={() => void elegir(s)}
                    className={`mb-1 w-full rounded-lg border px-2.5 py-2 text-left transition-colors ${
                      elegida?.handle === s.handle
                        ? 'border-[#4C9DFF]/50 bg-[#4C9DFF]/10'
                        : 'border-transparent hover:bg-[#22262D]'
                    }`}
                  >
                    <div className="flex items-center gap-2">
                      <span className="rounded border border-[#2C3037] bg-[#22262D] px-1.5 py-0.5 text-[10px] uppercase tracking-wide text-[#8CC1FF]">
                        {FUENTE[s.source] ?? s.source}
                      </span>
                      <span className="min-w-0 flex-1 truncate text-xs text-[#EDEEF0]" title={s.title}>{s.title}</span>
                    </div>
                    <div className="mt-0.5 flex items-center gap-2 text-[10px] text-[#59616D]">
                      <span className="min-w-0 flex-1 truncate" title={s.cwd}>{s.cwd || s.name}</span>
                      <span className="shrink-0 tabular-nums">{s.turns_aprox} turnos</span>
                      <span className="shrink-0">{fecha(s.mtime)}</span>
                    </div>
                  </button>
                ))}
              </div>
            </div>

            <div className="flex min-h-0 w-full flex-col rounded-xl border border-[#2C3037] bg-[#15171B] sm:flex-1">
              {!elegida ? (
                <div className="flex flex-1 items-center justify-center px-6 text-center text-[11px] leading-relaxed text-[#59616D]">
                  Elegí una sesión de la lista para ver qué turnos entran y qué se descarta antes de escribir nada.
                </div>
              ) : (
                <>
                  <div className="border-b border-[#2C3037] px-3 py-2">
                    <div className="truncate text-xs font-medium text-[#EDEEF0]" title={elegida.title}>{elegida.title}</div>
                    <div className="mt-0.5 flex flex-wrap gap-x-3 text-[10px] text-[#9BA1AA]">
                      {preview?.model && <span>modelo: {preview.model}</span>}
                      {preview?.cwd && <span className="min-w-0 truncate" title={preview.cwd}>{preview.cwd}</span>}
                      {preview && <span>{preview.turns_total} turnos de texto</span>}
                      {preview?.truncated && <span className="text-[#FFB86B]">truncado</span>}
                    </div>
                  </div>
                  <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2">
                    <Descartado skipped={preview?.skipped ?? {}} />
                    <div className="mt-2 space-y-2">
                      {(preview?.turns ?? []).map((t, i) => (
                        <div key={i} className="rounded-lg border border-[#22262D] bg-[#1A1D23] px-2.5 py-1.5">
                          <div className="flex items-center gap-2 text-[10px] text-[#59616D]">
                            <span className={t.role === 'user' ? 'text-[#8CC1FF]' : 'text-[#3DDC84]'}>{rolLabel(t.role)}</span>
                            <span className="ml-auto tabular-nums">{t.chars} caracteres</span>
                            {t.timestamp && <span>{fecha(t.timestamp)}</span>}
                          </div>
                          <div className="mt-1 whitespace-pre-wrap break-words text-[11px] leading-relaxed text-[#C9D2E0]">
                            {t.content || '(vacío)'}
                          </div>
                        </div>
                      ))}
                    </div>
                    {preview && preview.turns_total > preview.turns.length && (
                      <div className="mt-2 text-center text-[10px] text-[#59616D]">
                        muestra de {preview.turns.length} de {preview.turns_total} turnos
                      </div>
                    )}
                  </div>
                  <div className="border-t border-[#2C3037] px-3 py-2">
                    <label className="text-[10px] text-[#9BA1AA]" htmlFor="import-project">Proyecto</label>
                    <div className="mt-1 flex gap-2">
                      <input
                        id="import-project"
                        value={proyecto}
                        onChange={(e) => setProyecto(e.target.value)}
                        placeholder="importado"
                        className="min-w-0 flex-1 rounded-lg border border-[#2C3037] bg-[#1E2127] px-2 py-1.5 text-xs text-[#EDEEF0] placeholder:text-[#59616D] focus:border-[#4C9DFF]/50 focus:outline-none"
                      />
                      <button
                        onClick={() => void importar()}
                        disabled={importando || !preview || preview.turns_total === 0}
                        className="shrink-0 rounded-lg bg-[#4C9DFF] px-3 py-1.5 text-xs font-medium text-[#0B1220] transition-colors hover:bg-[#6FB0FF] disabled:bg-[#2C3037] disabled:text-[#59616D]"
                      >
                        {importando ? 'Importando…' : `Importar ${preview?.turns_total ?? 0} turnos`}
                      </button>
                    </div>
                  </div>
                </>
              )}
            </div>
          </div>

          {(error || listo) && (
            <div className={`shrink-0 border-t px-4 py-2 text-[11px] ${error ? 'border-[#5A2A2A] bg-[#3A1F1F]/60 text-[#FFA8A8]' : 'border-[#2A5A3A] bg-[#1F3A2A]/60 text-[#8FE3B0]'}`}>
              <span className="flex items-center gap-1.5">
                {listo && <CheckCircle2 className="h-3.5 w-3.5" />}
                {error ?? listo}
              </span>
            </div>
          )}

          {disponibles.length > 0 && (
            <div className="shrink-0 border-t border-[#2C3037] px-4 py-2 text-[10px] text-[#59616D]">
              Se leen los JSONL locales de {disponibles.map((d) => FUENTE[d] ?? d).join(' y ')}. No se escribe nada hasta importar.
            </div>
          )}
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
