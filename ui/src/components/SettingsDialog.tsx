import * as Dialog from '@radix-ui/react-dialog'
import { X, Settings, Database, Cpu, Shield, FileCode2, Layers, PlugZap, KeyRound, Mic, RefreshCw } from 'lucide-react'
import { useAppStore } from '@/store/app-store'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { ProvidersPanel } from '@/components/providers/ProvidersPanel'
import { useState, useEffect } from 'react'
import { apiFetch, keysApi, keyPutApi, permissionApi, permissionPutApi, voiceStatusApi, type KeyEntry, type VoiceStatus } from '@/lib/api'

type UpdateState = {
  ok?: boolean
  enabled?: boolean
  is_repo?: boolean
  branch?: string
  head?: string
  remote_head?: string
  running_head?: string
  behind?: number
  ahead?: number
  dirty?: number
  update_available?: boolean
  blocked?: string
  reason?: string
  cached?: boolean
  commits?: string[]
  applied?: boolean
  restart_required?: boolean
  reload_required?: boolean
  ui_stale?: boolean
  stale?: string[]
  built?: Record<string, string>
  changed_count?: number
}

function KeysTab() {
  const [keys, setKeys] = useState<KeyEntry[] | null>(null)
  const [envFile, setEnvFile] = useState('')
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<string | null>(null)
  const [msg, setMsg] = useState<string | null>(null)
  const [msgOk, setMsgOk] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    setLoadError(null)
    keysApi().then((r) => { setKeys(r.keys); setEnvFile(r.env_file) }).catch((e) => setLoadError(String(e).slice(0, 200)))
  }, [reloadKey])

  const save = async (env: string, valueOverride?: string) => {
    setSaving(env); setMsg(null)
    try {
      const val = valueOverride !== undefined ? valueOverride : (drafts[env] ?? '')
      await keyPutApi(env, val)
      setDrafts((d) => ({ ...d, [env]: '' }))
      const r = await keysApi(); setKeys(r.keys)
      setMsgOk(true)
      setMsg(val.trim() ? `${env} guardada en ${envFile} — efectiva al instante` : `${env} eliminada de ${envFile}`)
    } catch (e) { setMsgOk(false); setMsg(String(e).slice(0, 300)) } finally { setSaving(null) }
  }

  if (!keys) return (
    <div className={`text-xs ${loadError ? 'text-[#FFA8A8]' : 'text-[#9BA1AA]'}`}>
      {loadError ? (
        <>No se pudieron leer las keys: {loadError}{' '}
          <button onClick={() => setReloadKey((n) => n + 1)} className="underline hover:text-white">Reintentar</button>
        </>
      ) : 'Cargando keys…'}
    </div>
  )
  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-xs text-[#9BA1AA] leading-relaxed">
        Valores en vivo desde el entorno o <span className="font-mono text-[#EDEEF0]">{envFile || '.env'}</span> (raíz del proyecto). Nunca se muestran completos — solo máscara.
      </div>
      {keys.map((k) => (
        <div key={k.env} className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3">
          <div className="flex items-center gap-2 text-xs">
            <KeyRound className="h-3.5 w-3.5 text-[#4C9DFF]" />
            <span className="font-semibold text-[#EDEEF0]">{k.label}</span>
            <span className="font-mono text-[#9BA1AA]">{k.env}</span>
            <span className={`ml-auto rounded-full px-2 py-0.5 text-[10px] border ${k.configured ? 'border-[#3DDC84]/30 bg-[#3DDC84]/10 text-[#3DDC84]' : 'border-[#2C3037] bg-[#22262D] text-[#9BA1AA]'}`}>
              {k.configured ? `activa · ${k.source}` : 'sin definir'}
            </span>
          </div>
          {k.configured && <div className="mt-1 font-mono text-[11px] text-[#9BA1AA]">{k.masked}</div>}
          <div className="mt-2 flex gap-2">
            <input
              type="password"
              value={drafts[k.env] ?? ''}
              onChange={(e) => setDrafts((d) => ({ ...d, [k.env]: e.target.value }))}
              placeholder={k.configured ? '•••••••• (reescribir)' : 'pegar API key…'}
              className="flex-1 rounded-lg border border-[#2C3037] bg-[#15171B] px-2.5 py-1.5 text-xs text-[#EDEEF0] placeholder:text-[#9BA1AA] focus:outline-none"
            />
            <button
              onClick={() => save(k.env)}
              disabled={saving === k.env || !(drafts[k.env] ?? '').trim()}
              className="rounded-lg bg-[#0B62D6] px-3 py-1.5 text-xs font-medium text-white hover:bg-[#4C9DFF] disabled:opacity-40"
            >Guardar</button>
            {k.configured && (
              <button onClick={() => { setDrafts((d) => ({ ...d, [k.env]: ' ' })); void save(k.env, ' ') }} className="rounded-lg border border-[#2C3037] px-3 py-1.5 text-xs text-[#9BA1AA] hover:text-[#FF6259]">Quitar</button>
            )}
          </div>
          {k.providers.length > 0 && <div className="mt-1.5 flex flex-wrap gap-1">{k.providers.map((p) => <span key={p} className="rounded bg-[#22262D] px-1.5 py-0.5 text-[10px] text-[#9BA1AA]">{p}</span>)}</div>}
        </div>
      ))}
      {msg && <div className={`text-xs ${msgOk ? 'text-[#3DDC84]' : 'text-[#FFA8A8]'}`}>{msg}</div>}
    </div>
  )
}

function PermisoTab() {
  const [perm, setPerm] = useState<string | null>(null)
  const [options, setOptions] = useState<string[]>([])
  const [msg, setMsg] = useState<string | null>(null)
  const [msgOk, setMsgOk] = useState(true)
  const [failed, setFailed] = useState(false)
  const [reloadKey, setReloadKey] = useState(0)

  useEffect(() => {
    setFailed(false)
    permissionApi().then((r) => { setPerm(r.permission); setOptions(r.options) })
      .catch((e) => { setMsg(String(e).slice(0, 200)); setFailed(true) })
  }, [reloadKey])

  const set = async (p: string) => {
    const prev = perm; setPerm(p); setMsg(null)
    try { await permissionPutApi(p); setMsgOk(true); setMsg(`permiso aplicado: ${p}`) } catch (e) { setPerm(prev); setMsgOk(false); setMsg(String(e).slice(0, 200)) }
  }

  const desc: Record<string, string> = {
    read_only: 'Solo lectura — el agente no escribe ni ejecuta.',
    workspace_write: 'Escribe en ./workspace y ./data. Alcance FS activo.',
    full: 'Control total de la PC (raíces detectadas + red). Usar con cuidado.',
  }

  if (!perm) return (
    <div className={`text-xs ${failed ? 'text-[#FFA8A8]' : 'text-[#9BA1AA]'}`}>
      {failed ? (
        <>No se pudieron cargar los permisos: {msg}{' '}
          <button onClick={() => { setMsg(null); setReloadKey((n) => n + 1) }} className="underline hover:text-white">Reintentar</button>
        </>
      ) : 'Cargando permisos…'}
    </div>
  )
  return (
    <div className="space-y-3">
      <div className="text-xs text-[#9BA1AA]">Guardia de Alcance — ACL por defecto denegado. El permiso se guarda en config.yaml y aplica al próximo tool-call.</div>
      {options.map((o) => (
        <label key={o} className={`flex cursor-pointer items-start gap-3 rounded-xl border p-3 transition-colors ${perm === o ? 'border-[#4C9DFF]/60 bg-[#0B62D6]/10' : 'border-[#2C3037] bg-[#1E2127] hover:border-[#4C9DFF]/30'}`}>
          <input type="radio" name="perm" checked={perm === o} onChange={() => set(o)} className="mt-0.5 accent-[#4C9DFF]" />
          <div>
            <div className="text-xs font-semibold text-[#EDEEF0] font-mono">{o}</div>
            <div className="mt-0.5 text-[11px] text-[#9BA1AA]">{desc[o] || ''}</div>
          </div>
          {o === 'full' && <span className="ml-auto rounded-full border border-[#FF6259]/40 bg-[#FF6259]/10 px-2 py-0.5 text-[10px] text-[#FF6259]">riesgo total</span>}
        </label>
      ))}
      {msg && <div className={`text-xs ${msgOk ? 'text-[#3DDC84]' : 'text-[#FFA8A8]'}`}>{msg}</div>}
    </div>
  )
}

function VozTab() {
  const [status, setStatus] = useState<VoiceStatus | null>(null)
  const [err, setErr] = useState<string | null>(null)
  useEffect(() => {
    voiceStatusApi().then(setStatus).catch((e) => setErr(String(e).slice(0, 200)))
  }, [])
  if (err) return <div className="text-xs text-[#FF6259]">{err}</div>
  if (!status) return <div className="text-xs text-[#9BA1AA]">Consultando motor de voz… (1ª llamada puede tardar)</div>
  const chip = (on: boolean) => on ? 'border-[#3DDC84]/30 bg-[#3DDC84]/10 text-[#3DDC84]' : 'border-[#2C3037] bg-[#22262D] text-[#9BA1AA]'
  return (
    <div className="space-y-3">
      <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-xs text-[#9BA1AA] leading-relaxed">
        STT (faster-whisper o endpoint HTTP) para el micrófono del chat · TTS (Piper si está instalado; si no, voz nativa de Windows vía SAPI). Sin descargas obligatorias.
      </div>
      <div className="grid grid-cols-2 gap-2 text-xs">
        <div className={`rounded-xl border p-3 ${chip(status.stt_available)}`}>
          <div className="font-semibold">STT (dictado)</div>
          <div className="mt-0.5 font-mono text-[11px]">{status.stt_available ? `listo · ${status.stt_model || 'whisper'}` : 'modelos ausentes'}</div>
        </div>
        <div className={`rounded-xl border p-3 ${chip(status.tts_available)}`}>
          <div className="font-semibold">TTS (voz)</div>
          <div className="mt-0.5 font-mono text-[11px]">{status.tts_available ? `listo · ${status.tts_backend || status.tts_model || 'tts'}` : 'no disponible'}</div>
        </div>
      </div>
      <div className="text-[11px] text-[#9BA1AA]">Fallback del navegador: si el STT local no está, el micrófono usa SpeechRecognition (Chrome/Edge).</div>
    </div>
  )
}

function UpdatesCard() {
  const [st, setSt] = useState<UpdateState | null>(null)
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<string | null>(null)
  const [ok, setOk] = useState(true)

  const load = async (refresh = false) => {
    try {
      const r = await apiFetch(`/api/updates/auto${refresh ? '?refresh=1' : ''}`, { timeoutMs: 120000 })
      setSt(await r.json())
    } catch (e) {
      setOk(false)
      setMsg(String(e).slice(0, 160))
    }
  }
  useEffect(() => { void load(false) }, [])

  const applyNow = async () => {
    setBusy(true); setMsg(null)
    try {
      const r = await apiFetch('/api/updates/auto/apply', { method: 'POST', timeoutMs: 300000 })
      const j = await r.json()
      setSt(j)
      if (j.applied) {
        const w = window as unknown as { nexus?: { restartBackend?: () => Promise<unknown> }; location: Location }
        if (j.ui_stale) {
          setOk(false)
          setMsg(`Se trajo el código nuevo, pero la UI no se pudo recompilar (${j.built?.ui || 'sin detalle'}). La ventana sigue mostrando el bundle anterior.`)
        } else if (j.restart_required && w.nexus?.restartBackend) {
          setMsg('Actualizado. Reiniciando el backend para tomar el código nuevo…')
          await w.nexus.restartBackend()
          setTimeout(() => w.location.reload(), 1800)
        } else {
          setMsg(j.reload_required ? 'Actualizado. Recargando la ventana con la UI nueva…' : 'Actualizado. Recargá la ventana para ver la UI nueva.')
          setTimeout(() => w.location.reload(), 1500)
        }
      } else {
        setOk(false)
        setMsg(j.reason || j.blocked || 'No se aplicó nada.')
      }
    } catch (e) {
      setOk(false)
      setMsg(String(e).slice(0, 160))
    }
    setBusy(false)
  }

  const nuevo = !!st?.update_available
  const desfasado = !!st?.running_head && !!st?.head && st.running_head !== st.head
  const estado = !st
    ? 'consultando…'
    : !st.is_repo
      ? 'sin git en esta instalación'
      : nuevo
        ? `${st.behind} commit${st.behind === 1 ? '' : 's'} nuevo${st.behind === 1 ? '' : 's'}`
        : st.ui_stale
          ? 'código nuevo, UI sin recompilar'
          : desfasado
            ? 'código nuevo en disco — falta reiniciar'
            : 'al día'

  const restartNow = async () => {
    const w = window as unknown as { nexus?: { restartBackend?: () => Promise<unknown> }; location: Location }
    if (!w.nexus?.restartBackend) {
      setOk(false)
      setMsg('Esta ventana no puede reiniciar el backend; cerrá y abrí Nexus para tomar el código nuevo.')
      return
    }
    setBusy(true)
    setMsg('Reiniciando el backend para tomar el código nuevo…')
    try {
      await w.nexus.restartBackend()
      setOk(true)
      setTimeout(() => w.location.reload(), 2000)
    } catch (e) {
      setOk(false)
      setMsg(String(e).slice(0, 160))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-4">
      <div className="flex items-center justify-between">
        <div className="text-xs font-semibold flex items-center gap-2">
          <RefreshCw className={`h-3.5 w-3.5 ${nuevo ? 'text-[#FFB86B]' : 'text-[#3DDC84]'}`} /> Actualizaciones
        </div>
        <div className="flex items-center gap-2">
          <button onClick={() => load(true)} disabled={busy} className="text-[11px] text-[#4C9DFF] hover:text-[#8CC1FF] disabled:opacity-40">
            buscar ahora
          </button>
          {desfasado && (
            <button
              onClick={restartNow}
              disabled={busy}
              className="rounded-lg border border-[#FFB86B]/30 bg-[#FFB86B]/10 px-2 py-1 text-[11px] text-[#FFB86B] hover:bg-[#FFB86B]/20 disabled:opacity-40"
            >
              reiniciar backend
            </button>
          )}
          <button
            onClick={applyNow}
            disabled={busy || !nuevo || !!st?.blocked}
            className="rounded-lg border border-[#4C9DFF]/30 bg-[#4C9DFF]/10 px-2 py-1 text-[11px] text-[#8CC1FF] hover:bg-[#4C9DFF]/20 disabled:opacity-40"
          >
            {busy ? 'actualizando…' : 'actualizar ahora'}
          </button>
        </div>
      </div>
      <div className="mt-2 text-xs text-[#9BA1AA] leading-relaxed">
        Se actualiza sola al abrir la app (<span className="font-mono text-[#EDEEF0]">git pull --ff-only</span>) y recompila
        lo que haga falta. Si hay cambios locales sin commitear o commits propios sin publicar, no toca nada.
      </div>
      <div className="mt-2 flex flex-wrap items-center gap-1.5 text-[11px]">
        <span className="rounded-full border border-[#2C3037] bg-[#22262D] px-2 py-0.5 font-mono text-[#9BA1AA]">
          {st?.branch || '—'}
        </span>
        <span className="rounded-full border border-[#2C3037] bg-[#22262D] px-2 py-0.5 font-mono text-[#9BA1AA]">
          {st?.head || '—'}
        </span>
        {desfasado && (
          <span className="rounded-full border border-[#FFB86B]/30 bg-[#FFB86B]/10 px-2 py-0.5 font-mono text-[#FFB86B]">
            corriendo {st?.running_head}
          </span>
        )}
        <span className={`rounded-full px-2 py-0.5 border ${nuevo || desfasado || st?.ui_stale ? 'border-[#FFB86B]/30 bg-[#FFB86B]/10 text-[#FFB86B]' : 'border-[#3DDC84]/30 bg-[#3DDC84]/10 text-[#3DDC84]'}`}>
          {estado}
        </span>
        {!!st?.dirty && (
          <span className="rounded-full border border-[#FFB86B]/30 bg-[#FFB86B]/10 px-2 py-0.5 text-[#FFB86B]">
            {st.dirty} cambio{st.dirty === 1 ? '' : 's'} local{st.dirty === 1 ? '' : 'es'}
          </span>
        )}
      </div>
      {!!st?.commits?.length && (
        <div className="mt-2 rounded-lg border border-[#2C3037] bg-[#15171B] p-2 font-mono text-[11px] text-[#9BA1AA] space-y-0.5">
          {st.commits.slice(0, 4).map((c) => <div key={c} className="truncate">· {c}</div>)}
        </div>
      )}
      {st?.blocked && <div className="mt-2 text-[11px] text-[#FFB86B]">{st.blocked}</div>}
      {!!st?.stale?.length && (
        <div className="mt-2 rounded-lg border border-[#FFB86B]/30 bg-[#FFB86B]/10 p-2 text-[11px] text-[#FFB86B]">
          El código nuevo está en disco, pero no se pudo recompilar: {st.stale.join(', ')}.
          Esta máquina necesita <span className="font-mono">npm</span> y <span className="font-mono">node_modules</span> para
          que el cambio de UI se vea; sin eso, la ventana sigue con el bundle anterior.
        </div>
      )}
      {!st?.ok && !!st?.reason && <div className="mt-1 text-[11px] text-[#9BA1AA]">{st.reason}</div>}
      {msg && <div className={`mt-2 text-[11px] ${ok ? 'text-[#3DDC84]' : 'text-[#FF6259]'}`}>{msg}</div>}
    </div>
  )
}

export function SettingsDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (v: boolean) => void }) {
  const { providerHealth, selectedModel, models, isProvidersLoading, refreshProviders } = useAppStore()
  const current = models.find(m => m.id === selectedModel)
  const [tab, setTab] = useState('resumen')

  return (
    <Dialog.Root open={open} onOpenChange={onOpenChange}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-[80] bg-black/60 backdrop-blur-sm data-[state=open]:animate-in data-[state=closed]:animate-out" />
        <Dialog.Content
          aria-describedby={undefined}
          className="fixed left-1/2 top-1/2 z-[90] w-[94vw] max-w-[720px] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-[#2C3037] bg-[#1E2127] shadow-2xl max-h-[88vh] overflow-hidden flex flex-col focus:outline-none"
        >
          <div className="flex items-center justify-between border-b border-[#2C3037] bg-[#1E2127] px-5 py-4 shrink-0">
            <Dialog.Title className="flex items-center gap-2 text-sm font-semibold">
              <Settings className="h-4 w-4 text-[#4C9DFF]" /> Ajustes
            </Dialog.Title>
            <div className="flex items-center gap-2">
              <span className="hidden sm:inline text-xs text-[#9BA1AA] font-mono">config.yaml · Alcance</span>
              <Dialog.Close asChild>
                <button aria-label="Cerrar ajustes" className="rounded-lg p-1.5 hover:bg-[#2C3037] text-[#9BA1AA] hover:text-[#EDEEF0]">
                  <X className="h-4 w-4" />
                </button>
              </Dialog.Close>
            </div>
          </div>

          <Tabs value={tab} onValueChange={setTab} className="flex-1 overflow-hidden flex flex-col min-h-0">
            <div className="px-5 pt-3 border-b border-[#2C3037] bg-[#1E2127] shrink-0">
              <TabsList>
                <TabsTrigger value="resumen"><Layers className="h-3.5 w-3.5 inline mr-1" />Resumen</TabsTrigger>
                <TabsTrigger value="proveedores"><PlugZap className="h-3.5 w-3.5 inline mr-1" />Proveedores LLM</TabsTrigger>
                <TabsTrigger value="keys"><KeyRound className="h-3.5 w-3.5 inline mr-1" />API Keys</TabsTrigger>
                <TabsTrigger value="permisos"><Shield className="h-3.5 w-3.5 inline mr-1" />Permisos</TabsTrigger>
                <TabsTrigger value="voz"><Mic className="h-3.5 w-3.5 inline mr-1" />Voz</TabsTrigger>
                <TabsTrigger value="avanzado"><FileCode2 className="h-3.5 w-3.5 inline mr-1" />Avanzado</TabsTrigger>
              </TabsList>
            </div>

            <div className="flex-1 overflow-auto p-5 space-y-5">
              <TabsContent value="resumen" className="space-y-5">
                <div className="rounded-xl bg-[#1E2127] border border-[#4C9DFF]/20 p-4">
                  <div className="text-xs font-semibold flex items-center gap-2"><Layers className="h-3.5 w-3.5 text-[#4C9DFF]" /> Proyecto Nexus v3</div>
                  <div className="mt-1 text-xs text-[#9BA1AA] leading-relaxed">
                    Configuración gestionada en <span className="font-mono text-[#EDEEF0]">config/config.yaml</span> — sin valores hardcodeados.
                    Edita proveedores en la pestaña <span className="text-[#8CC1FF]">Proveedores LLM</span> sin tocar código.
                  </div>
                  <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                    <div className="rounded-lg bg-[#22262D] border border-[#2C3037] p-2.5">
                      <div className="text-[#9BA1AA] text-[11px] tracking-widest">MODELO ACTIVO</div>
                      {current ? (
                        <>
                          <div className="font-medium text-white">{current.label}</div>
                          <div className="text-[#9BA1AA] font-mono text-[11px]">{current.id} · {current.ctx.toLocaleString()} ctx</div>
                        </>
                      ) : selectedModel === 'auto' ? (
                        <>
                          <div className="font-medium text-white">Auto (router)</div>
                          <div className="text-[#9BA1AA] font-mono text-[11px]">el harness elige según la tarea</div>
                        </>
                      ) : (
                        <div className="text-xs text-[#9BA1AA] mt-1">{isProvidersLoading ? 'Detectando…' : 'Ninguno — elegí uno en Proveedores LLM'}</div>
                      )}
                    </div>
                    <div className="rounded-lg bg-[#22262D] border border-[#2C3037] p-2.5">
                      <div className="flex items-center justify-between">
                        <div className="text-[#9BA1AA] text-[11px] tracking-widest">PROVEEDORES</div>
                        <button onClick={() => refreshProviders()} className="text-[11px] text-[#4C9DFF] hover:text-[#8CC1FF]">{isProvidersLoading ? 'sondeando…' : 're-sondear'}</button>
                      </div>
                      <div className="flex flex-wrap gap-1.5 mt-1">
                        {Object.keys(providerHealth).length === 0 && !isProvidersLoading ? (
                          <span className="text-xs text-[#9BA1AA]">sin datos — re-sondea</span>
                        ) : Object.entries(providerHealth).map(([k, v]) => (
                          <span key={k} className={`rounded-full px-2 py-0.5 text-xs border ${v === 'online' ? 'bg-[#3DDC84]/15 border-[#3DDC84]/20 text-[#3DDC84]' : v === 'offline' ? 'bg-[#22262D] border-[#2C3037] text-[#9BA1AA]' : 'bg-amber-500/15 border-amber-500/20 text-amber-400'}`}>{k}:{v}</span>
                        ))}
                        {isProvidersLoading && Object.keys(providerHealth).length === 0 && <span className="text-xs text-[#9BA1AA]">detectando…</span>}
                      </div>
                      {models.length > 0 && <div className="mt-2 text-[11px] text-[#9BA1AA]">{models.length} modelo{models.length !== 1 ? 's' : ''} detectado{models.length !== 1 ? 's' : ''} (solo en línea)</div>}
                      {models.length === 0 && !isProvidersLoading && <div className="mt-2 text-[11px] text-amber-400/80">Ningún modelo — configura en Proveedores LLM</div>}
                    </div>
                  </div>
                </div>

                <div className="grid grid-cols-3 gap-2">
                  <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-center">
                    <Database className="h-4 w-4 mx-auto text-[#4C9DFF]" />
                    <div className="text-xs font-medium mt-1">WAL+FTS</div>
                    <div className="text-[11px] text-[#9BA1AA]">Sesión persistente</div>
                  </div>
                  <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-center">
                    <Cpu className="h-4 w-4 mx-auto text-[#8CC1FF]" />
                    <div className="text-xs font-medium mt-1">RAG 768d</div>
                    <div className="text-[11px] text-[#9BA1AA]">nomic-embed</div>
                  </div>
                  <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-center">
                    <Shield className="h-4 w-4 mx-auto text-[#3DDC84]" />
                    <div className="text-xs font-medium mt-1">Alcance</div>
                    <div className="text-[11px] text-[#9BA1AA]">Jaula FS activa</div>
                  </div>
                </div>

                <UpdatesCard />
              </TabsContent>

              <TabsContent value="proveedores">
                <ProvidersPanel />
              </TabsContent>

              <TabsContent value="keys">
                <KeysTab />
              </TabsContent>

              <TabsContent value="permisos">
                <PermisoTab />
              </TabsContent>

              <TabsContent value="voz">
                <VozTab />
              </TabsContent>

              <TabsContent value="avanzado" className="space-y-4">
                <div className="space-y-2">
                  <div className="text-xs font-semibold tracking-widest text-[#9BA1AA] flex items-center gap-1.5"><FileCode2 className="h-3.5 w-3.5" /> CONFIG.YAML (referencia)</div>
                  <pre className="rounded-xl border border-[#2C3037] bg-[#15171B] p-3 text-xs font-mono leading-relaxed text-[#EDEEF0] overflow-auto">
{`project: nexus-v3
brain:
  path: \${NEXUS_BRAIN:-./brain}
session:
  wal: true
  fts: true
providers:
  ollama:   { base_url: http://localhost:11434, models: [qwen2.5-coder:7b] }
  llamacpp: { base_url: http://localhost:8080, fallback_url: http://localhost:8000, ctx: 40960 }
  cloud:    { base_url: https://openrouter.ai/api/v1, api_key: \${OPENROUTER_API_KEY}, enabled: false }
scope:
  allowed_roots: ["./workspace", "./data"]
  denied_patterns: ["**/.env", "**/.git/**"]
  max_read_bytes: 1048576`}
                  </pre>
                  <div className="text-[11px] text-[#9BA1AA]">La pestaña <span className="text-[#8CC1FF]">Proveedores LLM</span> escribe aquí automáticamente. También puedes editar <span className="font-mono text-[#EDEEF0]">config/config.yaml</span> en el Editor (pasa por Guardia Alcance).</div>
                </div>
                <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-xs text-[#9BA1AA] leading-relaxed">
                  <span className="font-semibold text-[#EDEEF0]">Compatibilidad:</span> cualquier servicio con API compatible con OpenAI se conecta con solo <code className="font-mono text-[#EDEEF0]">base_url</code> + <code className="font-mono text-[#EDEEF0]">api_key</code> + <code className="font-mono text-[#EDEEF0]">model</code>. El campo <code className="font-mono text-[#EDEEF0]">base_url</code> apunta a la raíz del endpoint (por ejemplo <code className="font-mono text-[#EDEEF0]">https://host/v1</code>); Nexus v3 le agrega <code className="font-mono text-[#EDEEF0]">/chat/completions</code>.
                </div>
              </TabsContent>
            </div>
          </Tabs>

          <div className="flex justify-end gap-2 px-5 py-3 border-t border-[#2C3037] bg-[#1E2127] shrink-0">
            <Dialog.Close asChild>
              <button className="rounded-xl border border-[#2C3037] px-4 py-2 text-sm hover:bg-[#22262D]">Cerrar</button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
