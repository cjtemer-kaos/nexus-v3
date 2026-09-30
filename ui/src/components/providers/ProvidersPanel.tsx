import { useEffect, useState } from 'react'
import { Cpu, Zap, Cloud, Check, PlugZap, Eye, EyeOff, Save, RefreshCw, AlertCircle, Sparkles, ExternalLink } from 'lucide-react'
import { getProvidersConfig, saveProvidersConfig, testProvider, type ProvidersConfigResponse } from '@/lib/api'
import { useAppStore } from '@/store/app-store'

type TestState = { loading: boolean; ok?: boolean; models?: string[]; error?: string; latency_ms?: number }

const CLOUD_PRESETS: Record<string, { label: string; base_url: string; hint: string }> = {
  zen: { label: 'Opencode Zen (gratis)', base_url: 'https://opencode.ai/zen', hint: 'muse-spark-1.2' },
  openrouter: { label: 'OpenRouter', base_url: 'https://openrouter.ai/api/v1', hint: 'openai/gpt-4o, anthropic/claude-3.5' },
  openai: { label: 'OpenAI', base_url: 'https://api.openai.com/v1', hint: 'gpt-4o, gpt-4o-mini' },
  anthropic: { label: 'Anthropic (OpenAI-compat)', base_url: 'https://api.anthropic.com/v1', hint: 'claude-3-5-sonnet' },
  custom: { label: 'Custom OpenAI-compatible', base_url: '', hint: 'cualquier endpoint /v1' },
}

const PROVIDER_META: Record<string, { icon: typeof Cpu; label: string; desc: string; color: string }> = {
  llamacpp: { icon: Cpu, label: 'llama.cpp', desc: 'Local :8080 / :8000 · GGUF Q4/Q8', color: 'text-cyan-400' },
  ollama: { icon: Zap, label: 'Ollama', desc: 'Local :11434 · /api/tags', color: 'text-amber-400' },
  cloud: { icon: Cloud, label: 'Nube / OpenAI-compat', desc: 'OpenRouter · OpenAI · Opencode Zen', color: 'text-violet-400' },
}

function Field({ label, hint, children }: { label: string; hint?: string; children: React.ReactNode }) {
  return (
    <label className="block space-y-1.5">
      <span className="text-xs font-medium text-zinc-300">{label} {hint && <span className="font-normal text-zinc-500">· {hint}</span>}</span>
      {children}
    </label>
  )
}

export function ProvidersPanel() {
  const { refreshProviders } = useAppStore()
  const [cfg, setCfg] = useState<ProvidersConfigResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)
  const [saveMsg, setSaveMsg] = useState<string | null>(null)
  const [tests, setTests] = useState<Record<string, TestState>>({})
  const [showKey, setShowKey] = useState<Record<string, boolean>>({})
  const [cloudPreset, setCloudPreset] = useState<string>('zen')

  const load = async () => {
    setLoading(true); setErr(null)
    try {
      const data = await getProvidersConfig()
      setCfg(data)
      // detect preset from cloud base_url
      const cu = data.providers.cloud?.base_url || ''
      if (cu.includes('openrouter')) setCloudPreset('openrouter')
      else if (cu.includes('opencode')) setCloudPreset('zen')
      else if (cu.includes('api.openai')) setCloudPreset('openai')
      else if (cu.includes('anthropic')) setCloudPreset('anthropic')
      else if (cu) setCloudPreset('custom')
    } catch (e) { setErr((e as Error).message) }
    finally { setLoading(false) }
  }
  useEffect(() => { load() }, [])

  const update = (name: string, patch: Record<string, unknown>) => {
    if (!cfg) return
    setCfg({ ...cfg, providers: { ...cfg.providers, [name]: { ...cfg.providers[name], ...patch } as any } })
    setSaveMsg(null)
  }

  const handleTest = async (name: string) => {
    if (!cfg) return
    const p = cfg.providers[name] || {}
    const base_url = (p.base_url as string) || ''
    if (!base_url) { setTests(t => ({ ...t, [name]: { loading: false, ok: false, error: 'base_url vacío' } })); return }
    setTests(t => ({ ...t, [name]: { loading: true } }))
    try {
      const res = await testProvider({
        provider: name,
        base_url,
        api_key: (p.api_key as string) || '',
        auth_style: (p.auth_style as string) || 'bearer',
        fallback_url: (p.fallback_url as string) || undefined,
      })
      setTests(t => ({ ...t, [name]: { loading: false, ok: res.ok, models: res.models, error: res.error || undefined, latency_ms: res.latency_ms } }))
    } catch (e) {
      setTests(t => ({ ...t, [name]: { loading: false, ok: false, error: (e as Error).message } }))
    }
  }

  const handleSave = async () => {
    if (!cfg) return
    setSaving(true); setSaveMsg(null)
    try {
      // Normalize models: string comma -> array handled server side, but send arrays
      const providers: Record<string, unknown> = {}
      for (const [k, v] of Object.entries(cfg.providers)) {
        const copy: Record<string, unknown> = { ...v }
        // models as array
        if (typeof copy.models === 'string') {
          copy.models = (copy.models as string).split(',').map(s => s.trim()).filter(Boolean)
        }
        // trim base_url
        if (typeof copy.base_url === 'string') copy.base_url = (copy.base_url as string).trim()
        if (typeof copy.fallback_url === 'string') copy.fallback_url = (copy.fallback_url as string).trim() || undefined
        // don't send masked
        delete copy.api_key_masked
        delete copy._has_env_key
        // keep api_key only if non-empty and not placeholder bullet
        if (typeof copy.api_key === 'string' && copy.api_key.includes('•')) delete copy.api_key
        providers[k] = copy
      }
      await saveProvidersConfig({ providers, router: cfg.router })
      setSaveMsg('Guardado · config.yaml actualizado')
      await refreshProviders()
      setTimeout(() => setSaveMsg(null), 3000)
    } catch (e) {
      setSaveMsg('Error: ' + (e as Error).message.slice(0,400))
    } finally { setSaving(false) }
  }

  if (loading) return <div className="flex items-center gap-2 text-sm text-zinc-500 py-8 justify-center"><RefreshCw className="h-4 w-4 animate-spin" /> Cargando proveedores…</div>
  if (err) return <div className="rounded-xl border border-red-500/20 bg-red-500/10 p-4 text-sm text-red-300"><AlertCircle className="h-4 w-4 inline mr-1" /> {err} <button onClick={load} className="ml-2 underline">Reintentar</button></div>
  if (!cfg) return null

  const order = ['llamacpp', 'ollama', 'cloud'] as const
  // ensure all present
  for (const k of order) if (!cfg.providers[k]) cfg.providers[k] = {} as any

  return (
    <div className="space-y-4">
      <div className="rounded-xl border border-violet-500/20 bg-gradient-to-br from-violet-600/10 to-cyan-500/10 p-3.5 flex items-start gap-3">
        <div className="h-8 w-8 rounded-lg bg-violet-600 flex items-center justify-center shrink-0"><PlugZap className="h-4 w-4 text-white" /></div>
        <div className="min-w-0">
          <div className="text-sm font-semibold">Proveedores LLM · sin código</div>
          <div className="text-xs text-zinc-400 leading-relaxed mt-0.5">Configura Ollama, llama.cpp y nube (OpenRouter / Opencode Zen / OpenAI) sin tocar <span className="font-mono text-zinc-300">config.yaml</span>. Usa <em className="text-zinc-300">Probar conexión</em> y luego <em className="text-zinc-300">Guardar</em>.</div>
          <div className="mt-2 flex flex-wrap gap-1.5 text-[11px]"><span className="rounded-full bg-white/10 px-2 py-0.5">base_url</span><span className="rounded-full bg-white/10 px-2 py-0.5">api_key</span><span className="rounded-full bg-white/10 px-2 py-0.5">model</span><span className="rounded-full bg-white/10 px-2 py-0.5">Probar</span></div>
        </div>
      </div>

      {order.map((key) => {
        const meta = PROVIDER_META[key]
        const I = meta.icon
        const p = cfg.providers[key] || {}
        const t = tests[key]
        const enabled = (p.enabled as boolean | undefined) ?? true // default true for ollama/llamacpp, cloud false but UI shows toggle
        const isCloud = key === 'cloud'
        const showModels = Array.isArray(p.models) ? (p.models as string[]).join(', ') : (p.models as unknown as string) || ''
        return (
          <div key={key} className="rounded-2xl border border-white/10 bg-white/[0.02] overflow-hidden">
            <div className="flex items-center gap-3 px-4 py-3 border-b border-white/10 bg-white/[0.03]">
              <div className={`h-8 w-8 rounded-lg bg-white/10 flex items-center justify-center ${meta.color}`}><I className="h-4 w-4" /></div>
              <div className="min-w-0 flex-1">
                <div className="text-sm font-semibold flex items-center gap-2">{meta.label}
                  <span className="text-xs font-normal text-zinc-500 hidden sm:inline">· {meta.desc}</span>
                </div>
                <div className="text-xs text-zinc-500 sm:hidden">{meta.desc}</div>
              </div>
              <label className="flex items-center gap-2 text-xs cursor-pointer select-none">
                <span className={enabled ? 'text-emerald-400' : 'text-zinc-500'}>{enabled ? 'Activado' : 'Desactivado'}</span>
                <button
                  role="switch" aria-checked={enabled} aria-label={`Activar ${meta.label}`}
                  onClick={() => update(key, { enabled: !enabled })}
                  className={`relative h-6 w-11 rounded-full transition-colors ${enabled ? 'bg-violet-600' : 'bg-white/15'}`}
                >
                  <span className={`absolute top-0.5 left-0.5 h-5 w-5 rounded-full bg-white shadow transition-transform ${enabled ? 'translate-x-5' : ''}`} />
                </button>
              </label>
            </div>

            <div className="p-4 space-y-3.5">
              {isCloud && (
                <Field label="Preset rápido" hint="elige y se rellena base_url">
                  <div className="flex gap-2">
                    <select value={cloudPreset} onChange={e => {
                      const v = e.target.value; setCloudPreset(v)
                      const preset = CLOUD_PRESETS[v]
                      if (preset) {
                        if (v !== 'custom') update(key, { base_url: preset.base_url })
                        // enable cloud when user picks a preset other than zen? keep enabled true
                        update(key, { enabled: true })
                      }
                    }} className="flex-1 rounded-xl border border-white/10 bg-zinc-900 px-3 py-2 text-sm focus:outline-none focus:border-violet-500/50">
                      {Object.entries(CLOUD_PRESETS).map(([k, v]) => <option key={k} value={k}>{v.label} — {v.base_url || 'custom'}</option>)}
                    </select>
                    {p.base_url ? <a href={String(p.base_url)} target="_blank" rel="noreferrer" className="rounded-xl border border-white/10 px-3 py-2 text-zinc-500 hover:text-zinc-300" title="Abrir"><ExternalLink className="h-4 w-4" /></a> : null}
                  </div>
                  <div className="text-[11px] text-zinc-500">Ejemplo modelos: {CLOUD_PRESETS[cloudPreset]?.hint}</div>
                </Field>
              )}

              <div className="grid sm:grid-cols-2 gap-3">
                <Field label="Base URL" hint={key==='ollama' ? 'http://localhost:11434' : key==='llamacpp' ? 'http://localhost:8080' : 'https://…/v1'}>
                  <input value={(p.base_url as string) || ''} onChange={e=>update(key, { base_url: e.target.value })}
                    placeholder={key==='ollama' ? 'http://localhost:11434' : key==='llamacpp' ? 'http://localhost:8080' : 'https://openrouter.ai/api/v1'}
                    className="w-full rounded-xl border border-white/10 bg-zinc-950 px-3 py-2 text-sm font-mono placeholder:text-zinc-600 focus:outline-none focus:border-violet-500/50" />
                </Field>
                {key==='llamacpp' ? (
                  <Field label="Fallback URL" hint="opcional · :8000">
                    <input value={(p.fallback_url as string) || ''} onChange={e=>update(key,{ fallback_url: e.target.value })}
                      placeholder="http://localhost:8000"
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-3 py-2 text-sm font-mono placeholder:text-zinc-600 focus:outline-none focus:border-violet-500/50" />
                  </Field>
                ) : isCloud ? (
                  <Field label="API Key" hint={p._has_env_key ? 'usando ${OPENCODE_API_KEY}' : 'se guarda en config.yaml'}>
                    <div className="relative">
                      <input type={showKey[key] ? 'text' : 'password'} value={(p.api_key as string) || ''} onChange={e=>update(key,{ api_key: e.target.value })}
                        placeholder={p._has_env_key ? '•••• (env var) · deja vacío para mantener' : 'sk-… / or-…'}
                        className="w-full rounded-xl border border-white/10 bg-zinc-950 px-3 py-2 pr-9 text-sm font-mono placeholder:text-zinc-600 focus:outline-none focus:border-violet-500/50" />
                      <button type="button" onClick={()=>setShowKey(s=>({ ...s, [key]: !s[key] }))} className="absolute right-1 top-1/2 -translate-y-1/2 rounded-lg p-1.5 text-zinc-500 hover:text-zinc-300">
                        {showKey[key] ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                      </button>
                    </div>
                    {p.api_key_masked && !p.api_key ? <div className="text-[11px] text-zinc-500">actual: <span className="font-mono text-zinc-400">{p.api_key_masked as string}</span></div> : null}
                  </Field>
                ) : null}
              </div>

              {!isCloud && key!=='llamacpp' ? null : null}

              {/* Ollama doesn't need api_key; show models for all */}
              <Field label="Modelos" hint="separados por coma · se detectan al Probar">
                <input value={showModels} onChange={e=>update(key,{ models: e.target.value })}
                  placeholder={key==='ollama' ? 'qwen2.5-coder:7b, deepseek-r1:8b' : key==='llamacpp' ? 'nexus-director-v6, qwen3.5-9b' : 'muse-spark-1.2, gpt-4o'}
                  className="w-full rounded-xl border border-white/10 bg-zinc-950 px-3 py-2 text-sm font-mono placeholder:text-zinc-600 focus:outline-none focus:border-violet-500/50" />
                <div className="text-[11px] text-zinc-500">Tip: deja vacío y usa <em>Probar conexión</em> para autodetectar.</div>
              </Field>

              {isCloud && (
                <div className="grid grid-cols-2 gap-3">
                  <Field label="Auth" hint="bearer / x-api-key">
                    <select value={(p.auth_style as string) || 'bearer'} onChange={e=>update(key,{ auth_style: e.target.value })}
                      className="w-full rounded-xl border border-white/10 bg-zinc-900 px-3 py-2 text-sm focus:outline-none focus:border-violet-500/50">
                      <option value="bearer">Bearer</option>
                      <option value="x-api-key">x-api-key</option>
                      <option value="none">none</option>
                    </select>
                  </Field>
                  <Field label="Timeout (s)">
                    <input type="number" min={5} max={300} value={(p.timeout_s as number) ?? (key==='cloud'?60:300)} onChange={e=>update(key,{ timeout_s: Number(e.target.value) })}
                      className="w-full rounded-xl border border-white/10 bg-zinc-950 px-3 py-2 text-sm focus:outline-none focus:border-violet-500/50" />
                  </Field>
                </div>
              )}
              {key==='llamacpp' && (
                <Field label="Contexto (tokens)" hint="ctx · ej 40960">
                  <input type="number" min={1024} max={200000} step={1024} value={(p.ctx as number) ?? 40960} onChange={e=>update(key,{ ctx: Number(e.target.value) })}
                    className="w-full rounded-xl border border-white/10 bg-zinc-950 px-3 py-2 text-sm focus:outline-none focus:border-violet-500/50" />
                </Field>
              )}

              <div className="flex flex-wrap items-center gap-2">
                <button onClick={()=>handleTest(key)} disabled={t?.loading}
                  className="inline-flex items-center gap-2 rounded-xl bg-white text-zinc-900 px-4 py-2 text-sm font-medium hover:bg-zinc-100 disabled:opacity-50">
                  {t?.loading ? <RefreshCw className="h-4 w-4 animate-spin" /> : <PlugZap className="h-4 w-4" />}
                  Probar conexión
                </button>
                {t && !t.loading && (
                  <span className={`inline-flex items-center gap-1.5 rounded-full px-3 py-1 text-xs border ${t.ok ? 'bg-emerald-500/15 border-emerald-500/20 text-emerald-400' : 'bg-red-500/15 border-red-500/20 text-red-300'}`}>
                    {t.ok ? <Check className="h-3.5 w-3.5" /> : <AlertCircle className="h-3.5 w-3.5" />}
                    {t.ok ? `En línea` : 'Fallo'} {t.latency_ms ? `· ${t.latency_ms}ms` : ''}
                  </span>
                )}
                {t?.ok && t.models && t.models.length > 0 && (
                  <span className="text-xs text-zinc-400">{t.models.length} modelos: <span className="font-mono text-zinc-300">{t.models.slice(0,6).join(', ')}</span>{t.models.length>6 ? ' …' : ''}</span>
                )}
                {t?.ok && t.models && t.models.length>0 && (
                  <button onClick={()=>{
                    const cur = Array.isArray(p.models) ? (p.models as string[]) : []
                    const merged = Array.from(new Set([...cur, ...t.models!]))
                    update(key,{ models: merged })
                  }} className="text-xs text-violet-400 hover:text-violet-300">Importar modelos detectados</button>
                )}
                {t?.error && <span className="text-xs text-red-300 max-w-[260px] truncate" title={t.error}>{t.error}</span>}
              </div>

              {!t?.ok && t?.error && <div className="rounded-lg bg-red-500/10 border border-red-500/20 px-3 py-2 text-xs text-red-300">{t.error}</div>}
            </div>
          </div>
        )
      })}

      <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3 flex flex-wrap items-center gap-2">
        <button onClick={handleSave} disabled={saving}
          className="inline-flex items-center gap-2 rounded-xl bg-violet-600 hover:bg-violet-500 disabled:opacity-50 px-5 py-2.5 text-sm font-medium text-white">
          {saving ? <RefreshCw className="h-4 w-4 animate-spin" /> : <Save className="h-4 w-4" />} {saving ? 'Guardando…' : 'Guardar proveedores'}
        </button>
        <span className="text-xs text-zinc-500">Escribe en <span className="font-mono text-zinc-300">config/config.yaml</span> y re-sondea.</span>
        {saveMsg && <span className={`ml-auto text-xs px-2.5 py-1 rounded-full border ${saveMsg.startsWith('Error') ? 'bg-red-500/15 border-red-500/20 text-red-300' : 'bg-emerald-500/15 border-emerald-500/20 text-emerald-400'}`}>{saveMsg}</span>}
      </div>

      <div className="rounded-xl border border-white/10 bg-zinc-900 p-3 text-xs text-zinc-500 leading-relaxed flex gap-2">
        <Sparkles className="h-4 w-4 text-violet-400 shrink-0 mt-0.5" />
        <div>
          <span className="text-zinc-300">Cómo funciona:</span> Ollama no necesita API key — solo base_url. llama.cpp usa OpenAI-compat (<code className="font-mono text-zinc-400">/v1/chat/completions</code>). Nube soporta OpenRouter/Opencode/OpenAI: elige preset, pega tu <code className="font-mono text-zinc-400">api_key</code> y modelo. <br />
          Como Hermes (<code className="font-mono">provider: custom / base_url</code>) y OpenCode (<code className="font-mono">provider.npm=@ai-sdk/openai-compatible</code>), un solo campo <code className="font-mono text-zinc-300">base_url</code> alcanza para cualquier endpoint compatible.
        </div>
      </div>
    </div>
  )
}
