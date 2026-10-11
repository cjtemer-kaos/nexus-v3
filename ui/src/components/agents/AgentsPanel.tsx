import { useEffect, useMemo, useRef, useState } from 'react'
import * as Dialog from '@radix-ui/react-dialog'
import { Bot, Plus, Trash2, Pencil, X, Zap, Cloud, Cpu, Sparkles, AlertCircle } from 'lucide-react'
import { useAppStore, type ModelOption } from '@/store/app-store'
import { agentsApi, agentCreateApi, agentUpdateApi, agentDeleteApi, gemasApi, assignApi, type Agent, type Gema } from '@/lib/api'
import { cn } from '@/lib/utils'

const providerIcon: Record<string, typeof Cpu> = { ollama: Zap, cloud: Cloud, llamacpp: Cpu }

type Draft = { name: string; emoji: string; model: string; system_prompt: string; description: string; gema: string }
const EMPTY: Draft = { name: '', emoji: '🤖', model: '', system_prompt: '', description: '', gema: '' }

function ModelOptions({ models, providerHealth }: { models: ModelOption[]; providerHealth: Record<string, string> }) {
  const grouped = useMemo(() => {
    const g: Record<string, ModelOption[]> = {}
    for (const m of models) {
      if (providerHealth[m.provider] === 'offline') continue
      if (!g[m.provider]) g[m.provider] = []
      g[m.provider].push(m)
    }
    return g
  }, [models, providerHealth])
  return (
    <>
      <option value="">Auto (router) — el arnés elige</option>
      {Object.entries(grouped).map(([provider, list]) => (
        <optgroup key={provider} label={provider}>
          {list.map((m) => <option key={m.id} value={m.id}>{m.label} · {m.ctx.toLocaleString()} ctx</option>)}
        </optgroup>
      ))}
    </>
  )
}

export function AgentsPanel() {
  const { models, providerHealth, refreshProviders } = useAppStore()
  const [agents, setAgents] = useState<Agent[]>([])
  const [gemas, setGemas] = useState<Gema[]>([])
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const [editing, setEditing] = useState<Agent | null>(null)
  const [saving, setSaving] = useState(false)
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [autoModel, setAutoModel] = useState('')
  const assignSeq = useRef(0)

  const reload = async () => {
    setLoading(true); setErr(null)
    try { const r = await agentsApi(); setAgents(r.agents) }
    catch (e) { setErr(String(e).slice(0, 200)) }
    finally { setLoading(false) }
  }

  useEffect(() => { void reload() }, [])
  useEffect(() => { gemasApi().then((r) => setGemas(r.gemas)).catch(() => {}) }, [])
  useEffect(() => { if (models.length === 0) void refreshProviders() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const applyGema = (gemaName: string) => {
    const g = gemas.find((x) => x.name === gemaName)
    setDraft((d) => ({
      ...d,
      gema: gemaName,
      description: d.description || (g ? g.description : ''),
      system_prompt: d.system_prompt || (g ? g.system_prompt : ''),
    }))
    setAutoModel('')
    const seq = ++assignSeq.current
    if (gemaName) assignApi({ gema: gemaName }).then((r) => { if (seq === assignSeq.current) setAutoModel(r.assign.model) }).catch(() => {})
  }

  const saveModel = async (a: Agent, model: string) => {
    const provider = models.find((m) => m.id === model)?.provider ?? ''
    setErr(null)
    try { const r = await agentUpdateApi(a.id, { model, provider }); setAgents((p) => p.map((x) => (x.id === a.id ? r.agent : x))) }
    catch (e) { setErr(String(e).slice(0, 200)) }
  }

  const submit = async () => {
    setSaving(true); setErr(null)
    const provider = models.find((m) => m.id === draft.model)?.provider ?? ''
    try {
      if (editing) {
        const r = await agentUpdateApi(editing.id, { ...draft, provider })
        setAgents((p) => p.map((x) => (x.id === editing.id ? r.agent : x)))
      } else {
        const r = await agentCreateApi({ ...draft, provider })
        setAgents((p) => [...p, r.agent])
      }
      setCreating(false); setEditing(null)
    } catch (e) { setErr(String(e).slice(0, 300)) } finally { setSaving(false) }
  }

  const remove = async (a: Agent) => {
    setErr(null)
    try { await agentDeleteApi(a.id); setAgents((p) => p.filter((x) => x.id !== a.id)) }
    catch (e) { setErr(String(e).slice(0, 200)) }
  }

  const openCreate = () => { setDraft(EMPTY); setCreating(true); assignSeq.current++; setAutoModel('') }
  const openEdit = (a: Agent) => {
    setDraft({ name: a.name, emoji: a.emoji || '🤖', model: a.model, system_prompt: a.system_prompt, description: a.description, gema: a.gema || '' })
    setEditing(a)
    // recomputar el hint de modelo auto para la gema de ESTE agente (no dejar el de otro)
    setAutoModel('')
    const seq = ++assignSeq.current
    if (a.gema && !a.model) assignApi({ gema: a.gema }).then((r) => { if (seq === assignSeq.current) setAutoModel(r.assign.model) }).catch(() => {})
  }
  const dialogOpen = creating || editing !== null

  return (
    <div className="h-full overflow-auto p-4 sm:p-6">
      <div className="mx-auto max-w-5xl">
        <div className="flex items-center gap-3">
          <div className="flex h-10 w-10 items-center justify-center rounded-xl bg-[#0B62D6] text-white"><Bot className="h-5 w-5" /></div>
          <div>
            <h1 className="text-lg font-semibold">Ayudantes</h1>
            <p className="text-xs text-[#9BA1AA]">Cada ayudante usa su propio LLM (modelo + proveedor) y su propio system prompt. Se aplican por turno al enviar en el chat.</p>
          </div>
          <button onClick={openCreate} className="ml-auto flex items-center gap-2 rounded-xl bg-[#0B62D6] px-3 py-2 text-sm font-medium text-white hover:bg-[#4C9DFF]">
            <Plus className="h-4 w-4" /> Nuevo ayudante
          </button>
        </div>

        {err && (
          <div className="mt-4 flex items-start gap-2 rounded-xl border border-[#FF6259]/30 bg-[#FF6259]/10 p-3 text-xs text-[#FF6259]">
            <AlertCircle className="h-4 w-4 shrink-0" /><span className="break-all">{err}</span>
          </div>
        )}

        {loading ? (
          <div className="mt-8 text-center text-sm text-[#9BA1AA]">Cargando ayudantes…</div>
        ) : (
          <div className="mt-5 grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3">
            {agents.map((a) => {
              const Icon = providerIcon[a.provider] ?? Cpu
              const selected = models.find((m) => m.id === a.model)
              return (
                <div key={a.id} className="flex flex-col rounded-2xl border border-[#2C3037] bg-[#1E2127] p-4">
                  <div className="flex items-center gap-3">
                    <div className="flex h-10 w-10 items-center justify-center rounded-xl text-lg" style={{ backgroundColor: (a.color || '#0B62D6') + '22' }}>{a.emoji || '🤖'}</div>
                    <div className="min-w-0">
                      <div className="flex items-center gap-2">
                        <span className="truncate text-sm font-semibold">{a.name}</span>
                        {a.builtin && <span className="rounded-full border border-[#4C9DFF]/30 bg-[#0B62D6]/15 px-1.5 py-0.5 text-[10px] text-[#8CC1FF]">por defecto</span>}
                        {a.gema && <span className="rounded-full border border-[#8B5CF6]/30 bg-[#8B5CF6]/15 px-1.5 py-0.5 text-[10px] text-[#C4B5FD]">{a.gema}</span>}
                      </div>
                      <div className="truncate font-mono text-[11px] text-[#9BA1AA]">{a.id}</div>
                    </div>
                    <div className="ml-auto flex items-center gap-1">
                      <button aria-label={`Editar ${a.name}`} onClick={() => openEdit(a)} className="rounded-lg p-1.5 text-[#9BA1AA] hover:bg-[#2C3037] hover:text-[#EDEEF0]"><Pencil className="h-3.5 w-3.5" /></button>
                      {!a.builtin && (
                        <button aria-label={`Borrar ${a.name}`} onClick={() => remove(a)} className="rounded-lg p-1.5 text-[#9BA1AA] hover:bg-[#2C3037] hover:text-[#FF6259]"><Trash2 className="h-3.5 w-3.5" /></button>
                      )}
                    </div>
                  </div>

                  {a.description && <p className="mt-2 line-clamp-2 text-xs text-[#9BA1AA]">{a.description}</p>}

                  <label className="mt-3 block text-[11px] font-medium tracking-widest text-[#9BA1AA]">LLM DEL AYUDANTE</label>
                  <div className="mt-1 flex items-center gap-2 rounded-xl border border-[#2C3037] bg-[#15171B] px-2.5 py-1.5">
                    {a.model === '' ? <Sparkles className="h-4 w-4 text-[#4C9DFF]" /> : <Icon className="h-4 w-4 text-[#4C9DFF]" />}
                    <select
                      aria-label={`Modelo de ${a.name}`}
                      value={a.model}
                      onChange={(e) => saveModel(a, e.target.value)}
                      className="w-full bg-transparent text-xs text-[#EDEEF0] focus:outline-none"
                    >
                      <ModelOptions models={models} providerHealth={providerHealth} />
                    </select>
                  </div>
                  <div className="mt-1 font-mono text-[10px] text-[#9BA1AA]">
                    {selected ? `${selected.provider} · ${selected.id}` : (a.model ? `${a.provider} · ${a.model}` : 'Auto (router) · sin modelo fijo')}
                  </div>

                  {a.system_prompt && (
                    <div className="mt-3 rounded-lg border border-[#2C3037] bg-[#15171B] p-2">
                      <div className="text-[10px] font-medium tracking-widest text-[#9BA1AA]">SYSTEM PROMPT</div>
                      <div className="mt-0.5 line-clamp-3 whitespace-pre-wrap text-[11px] text-[#EDEEF0]">{a.system_prompt}</div>
                    </div>
                  )}
                </div>
              )
            })}
          </div>
        )}

        {!loading && agents.length === 0 && <div className="mt-8 text-center text-sm text-[#9BA1AA]">Sin ayudantes. Crea el primero.</div>}
      </div>

      <Dialog.Root open={dialogOpen} onOpenChange={(v) => { if (!v) { setCreating(false); setEditing(null) } }}>
        <Dialog.Portal>
          <Dialog.Overlay className="fixed inset-0 z-[80] bg-black/60 backdrop-blur-sm" />
          <Dialog.Content aria-describedby={undefined} className="fixed left-1/2 top-1/2 z-[90] w-[94vw] max-w-[560px] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-[#2C3037] bg-[#1E2127] shadow-2xl focus:outline-none">
            <div className="flex items-center justify-between border-b border-[#2C3037] px-5 py-4">
              <Dialog.Title className="flex items-center gap-2 text-sm font-semibold"><Bot className="h-4 w-4 text-[#4C9DFF]" /> {editing ? `Editar ${editing.name}` : 'Nuevo ayudante'}</Dialog.Title>
              <Dialog.Close asChild>
                <button aria-label="Cerrar" className="rounded-lg p-1.5 text-[#9BA1AA] hover:bg-[#2C3037] hover:text-[#EDEEF0]"><X className="h-4 w-4" /></button>
              </Dialog.Close>
            </div>

            <div className="space-y-3 p-5">
              <div className="flex gap-3">
                <div className="w-16">
                  <label htmlFor="ag-emoji" className="text-[11px] font-medium tracking-widest text-[#9BA1AA]">EMOJI</label>
                  <input id="ag-emoji" value={draft.emoji} onChange={(e) => setDraft((d) => ({ ...d, emoji: e.target.value }))} maxLength={4} className="mt-1 w-full rounded-xl border border-[#2C3037] bg-[#15171B] px-3 py-2 text-center text-lg focus:outline-none focus:border-[#4C9DFF]/50" />
                </div>
                <div className="flex-1">
                  <label htmlFor="ag-name" className="text-[11px] font-medium tracking-widest text-[#9BA1AA]">NOMBRE</label>
                  <input id="ag-name" value={draft.name} disabled={!!editing} onChange={(e) => setDraft((d) => ({ ...d, name: e.target.value }))} placeholder="Coder Local" className="mt-1 w-full rounded-xl border border-[#2C3037] bg-[#15171B] px-3 py-2 text-sm focus:outline-none focus:border-[#4C9DFF]/50 disabled:opacity-60" />
                </div>
              </div>

              <div>
                <label htmlFor="ag-gema" className="text-[11px] font-medium tracking-widest text-[#9BA1AA]">GEMA (ESPECIALIDAD)</label>
                <select id="ag-gema" value={draft.gema} onChange={(e) => applyGema(e.target.value)} className="mt-1 w-full rounded-xl border border-[#2C3037] bg-[#15171B] px-3 py-2 text-sm focus:outline-none focus:border-[#4C9DFF]/50">
                  <option value="">— sin gema (ayudante libre) —</option>
                  {gemas.map((g) => <option key={g.name} value={g.name}>{g.name} · {g.description}</option>)}
                </select>
                <div className="mt-1 text-[10px] text-[#9BA1AA]">La gema define la especialidad: si no escribes un system prompt, se usa el suyo y su hint de modelo.</div>
              </div>

              <div>
                <label htmlFor="ag-model" className="text-[11px] font-medium tracking-widest text-[#9BA1AA]">LLM (modelo)</label>
                <select id="ag-model" value={draft.model} onChange={(e) => setDraft((d) => ({ ...d, model: e.target.value }))} className="mt-1 w-full rounded-xl border border-[#2C3037] bg-[#15171B] px-3 py-2 text-sm focus:outline-none focus:border-[#4C9DFF]/50">
                  <ModelOptions models={models} providerHealth={providerHealth} />
                </select>
                <div className="mt-1 text-[10px] text-[#9BA1AA]">
                  {draft.model
                    ? 'Modelo explícito del ayudante.'
                    : autoModel
                      ? `Auto: el arnés le asignaría ${autoModel} (según lo disponible en la PC).`
                      : 'Auto: el arnés elige el LLM según lo disponible en la PC.'}
                </div>
              </div>

              <div>
                <label htmlFor="ag-desc" className="text-[11px] font-medium tracking-widest text-[#9BA1AA]">DESCRIPCIÓN</label>
                <input id="ag-desc" value={draft.description} onChange={(e) => setDraft((d) => ({ ...d, description: e.target.value }))} placeholder="Qué hace este ayudante" className="mt-1 w-full rounded-xl border border-[#2C3037] bg-[#15171B] px-3 py-2 text-sm focus:outline-none focus:border-[#4C9DFF]/50" />
              </div>

              <div>
                <label htmlFor="ag-sys" className="text-[11px] font-medium tracking-widest text-[#9BA1AA]">SYSTEM PROMPT</label>
                <textarea id="ag-sys" value={draft.system_prompt} onChange={(e) => setDraft((d) => ({ ...d, system_prompt: e.target.value }))} rows={4} placeholder="Eres un ingeniero senior. Responde conciso y con evidencia." className="mt-1 w-full resize-y rounded-xl border border-[#2C3037] bg-[#15171B] px-3 py-2 text-sm focus:outline-none focus:border-[#4C9DFF]/50" />
              </div>
            </div>

            <div className="flex justify-end gap-2 border-t border-[#2C3037] px-5 py-3">
              <Dialog.Close asChild><button className="rounded-xl border border-[#2C3037] px-4 py-2 text-sm hover:bg-[#22262D]">Cancelar</button></Dialog.Close>
              <button onClick={submit} disabled={saving || !draft.name.trim()} className={cn('rounded-xl bg-[#0B62D6] px-4 py-2 text-sm font-medium text-white hover:bg-[#4C9DFF]', (saving || !draft.name.trim()) && 'opacity-40')}>{saving ? 'Guardando…' : (editing ? 'Guardar' : 'Crear')}</button>
            </div>
          </Dialog.Content>
        </Dialog.Portal>
      </Dialog.Root>
    </div>
  )
}
