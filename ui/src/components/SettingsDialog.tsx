import * as Dialog from '@radix-ui/react-dialog'
import { X, Settings, Database, Cpu, Shield, FileCode2, Layers, PlugZap } from 'lucide-react'
import { useAppStore } from '@/store/app-store'
import { Tabs, TabsList, TabsTrigger, TabsContent } from '@/components/ui/tabs'
import { ProvidersPanel } from '@/components/providers/ProvidersPanel'
import { useState } from 'react'

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
          className="fixed left-1/2 top-1/2 z-[90] w-[94vw] max-w-[720px] -translate-x-1/2 -translate-y-1/2 rounded-2xl border border-white/10 bg-zinc-900 shadow-2xl max-h-[88vh] overflow-hidden flex flex-col focus:outline-none"
        >
          <div className="flex items-center justify-between border-b border-white/10 bg-zinc-900 px-5 py-4 shrink-0">
            <Dialog.Title className="flex items-center gap-2 text-sm font-semibold">
              <Settings className="h-4 w-4 text-violet-400" /> Ajustes
            </Dialog.Title>
            <div className="flex items-center gap-2">
              <span className="hidden sm:inline text-xs text-zinc-500 font-mono">config.yaml · Alcance</span>
              <Dialog.Close asChild>
                <button aria-label="Cerrar ajustes" className="rounded-lg p-1.5 hover:bg-white/10 text-zinc-400 hover:text-zinc-100">
                  <X className="h-4 w-4" />
                </button>
              </Dialog.Close>
            </div>
          </div>

          <Tabs value={tab} onValueChange={setTab} className="flex-1 overflow-hidden flex flex-col min-h-0">
            <div className="px-5 pt-3 border-b border-white/10 bg-zinc-900 shrink-0">
              <TabsList>
                <TabsTrigger value="resumen"><Layers className="h-3.5 w-3.5 inline mr-1" />Resumen</TabsTrigger>
                <TabsTrigger value="proveedores"><PlugZap className="h-3.5 w-3.5 inline mr-1" />Proveedores LLM</TabsTrigger>
                <TabsTrigger value="avanzado"><FileCode2 className="h-3.5 w-3.5 inline mr-1" />Avanzado</TabsTrigger>
              </TabsList>
            </div>

            <div className="flex-1 overflow-auto p-5 space-y-5">
              <TabsContent value="resumen" className="space-y-5">
                <div className="rounded-xl bg-gradient-to-br from-violet-600/15 to-cyan-500/15 border border-violet-500/20 p-4">
                  <div className="text-xs font-semibold flex items-center gap-2"><Layers className="h-3.5 w-3.5 text-violet-400" /> Proyecto Nexus v3</div>
                  <div className="mt-1 text-xs text-zinc-400 leading-relaxed">
                    Configuración gestionada en <span className="font-mono text-zinc-200">config/config.yaml</span> — sin valores hardcodeados.
                    Edita proveedores en la pestaña <span className="text-violet-300">Proveedores LLM</span> sin tocar código.
                  </div>
                  <div className="mt-3 grid grid-cols-2 gap-2 text-xs">
                    <div className="rounded-lg bg-white/5 border border-white/10 p-2.5">
                      <div className="text-zinc-500 text-[11px] tracking-widest">MODELO ACTIVO</div>
                      {current ? (
                        <>
                          <div className="font-medium text-white">{current.label}</div>
                          <div className="text-zinc-500 font-mono text-[11px]">{current.id} · {current.ctx.toLocaleString()} ctx</div>
                        </>
                      ) : (
                        <div className="text-xs text-zinc-500 mt-1">{isProvidersLoading ? 'Detectando…' : 'Ninguno — sin proveedores en línea'}</div>
                      )}
                    </div>
                    <div className="rounded-lg bg-white/5 border border-white/10 p-2.5">
                      <div className="flex items-center justify-between">
                        <div className="text-zinc-500 text-[11px] tracking-widest">PROVEEDORES</div>
                        <button onClick={() => refreshProviders()} className="text-[11px] text-violet-400 hover:text-violet-300">{isProvidersLoading ? 'sondeando…' : 're-sondear'}</button>
                      </div>
                      <div className="flex flex-wrap gap-1.5 mt-1">
                        {Object.keys(providerHealth).length === 0 && !isProvidersLoading ? (
                          <span className="text-xs text-zinc-500">sin datos — re-sondea</span>
                        ) : Object.entries(providerHealth).map(([k, v]) => (
                          <span key={k} className={`rounded-full px-2 py-0.5 text-xs border ${v === 'online' ? 'bg-emerald-500/15 border-emerald-500/20 text-emerald-400' : v === 'offline' ? 'bg-white/5 border-white/10 text-zinc-500' : 'bg-amber-500/15 border-amber-500/20 text-amber-400'}`}>{k}:{v}</span>
                        ))}
                        {isProvidersLoading && Object.keys(providerHealth).length === 0 && <span className="text-xs text-zinc-500">detectando…</span>}
                      </div>
                      {models.length > 0 && <div className="mt-2 text-[11px] text-zinc-500">{models.length} modelo{models.length !== 1 ? 's' : ''} detectado{models.length !== 1 ? 's' : ''} (solo en línea)</div>}
                      {models.length === 0 && !isProvidersLoading && <div className="mt-2 text-[11px] text-amber-400/80">Ningún modelo — configura en Proveedores LLM</div>}
                    </div>
                  </div>
                </div>

                <div className="grid grid-cols-3 gap-2">
                  <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3 text-center">
                    <Database className="h-4 w-4 mx-auto text-violet-400" />
                    <div className="text-xs font-medium mt-1">WAL+FTS</div>
                    <div className="text-[11px] text-zinc-500">Sesión persistente</div>
                  </div>
                  <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3 text-center">
                    <Cpu className="h-4 w-4 mx-auto text-cyan-400" />
                    <div className="text-xs font-medium mt-1">RAG 768d</div>
                    <div className="text-[11px] text-zinc-500">nomic-embed</div>
                  </div>
                  <div className="rounded-xl border border-white/10 bg-white/[0.03] p-3 text-center">
                    <Shield className="h-4 w-4 mx-auto text-emerald-400" />
                    <div className="text-xs font-medium mt-1">Alcance</div>
                    <div className="text-[11px] text-zinc-500">Jaula FS activa</div>
                  </div>
                </div>
              </TabsContent>

              <TabsContent value="proveedores">
                <ProvidersPanel />
              </TabsContent>

              <TabsContent value="avanzado" className="space-y-4">
                <div className="space-y-2">
                  <div className="text-xs font-semibold tracking-widest text-zinc-500 flex items-center gap-1.5"><FileCode2 className="h-3.5 w-3.5" /> CONFIG.YAML (referencia)</div>
                  <pre className="rounded-xl border border-white/10 bg-zinc-950 p-3 text-xs font-mono leading-relaxed text-zinc-300 overflow-auto">
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
                  <div className="text-[11px] text-zinc-500">La pestaña <span className="text-violet-300">Proveedores LLM</span> escribe aquí automáticamente. También puedes editar <span className="font-mono text-zinc-300">config/config.yaml</span> en el Editor (pasa por Guardia Alcance).</div>
                </div>
                <div className="rounded-xl border border-white/10 bg-white/[0.02] p-3 text-xs text-zinc-400 leading-relaxed">
                  <span className="font-semibold text-zinc-200">Compatibilidad:</span> Hermes usa <code className="font-mono text-zinc-300">provider: custom + base_url</code> y OpenCode usa <code className="font-mono text-zinc-300">@ai-sdk/openai-compatible + baseURL</code>. Nexus v3 unifica ambos: cualquier endpoint OpenAI-compat funciona con solo <code className="font-mono">base_url</code> + <code className="font-mono">api_key</code> + <code className="font-mono">model</code>.
                </div>
              </TabsContent>
            </div>
          </Tabs>

          <div className="flex justify-end gap-2 px-5 py-3 border-t border-white/10 bg-zinc-900 shrink-0">
            <Dialog.Close asChild>
              <button className="rounded-xl border border-white/10 px-4 py-2 text-sm hover:bg-white/5">Cerrar</button>
            </Dialog.Close>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  )
}
