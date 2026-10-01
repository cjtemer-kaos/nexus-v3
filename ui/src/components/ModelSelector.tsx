import { Cpu, Zap, Cloud, Check, ChevronDown, Circle, RefreshCw, AlertCircle, Sparkles } from 'lucide-react'
import { useAppStore } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { useEffect, useMemo, useState } from 'react'
import * as DropdownMenu from '@radix-ui/react-dropdown-menu'

const providerIcon: Record<string, any> = { llamacpp: Cpu, ollama: Zap, cloud: Cloud }
const providerLabel: Record<string, string> = { llamacpp: 'llama.cpp', ollama: 'ollama', cloud: 'cloud' }

export function ModelSelector() {
  const { models, selectedModel, setModel, providerHealth, isProvidersLoading, providersError, refreshProviders } = useAppStore()
  const [open, setOpen] = useState(false)

  useEffect(() => {
    // Initial health probe — populates models dynamically
    if (models.length === 0 && isProvidersLoading) {
      refreshProviders()
    }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  // Group only online providers that have models — hides offline (e.g. ollama when not installed)
  const grouped = useMemo(() => {
    const g: Record<string, typeof models> = {}
    for (const m of models) {
      // only include if provider is online (health check passed)
      if (providerHealth[m.provider] === 'offline') continue
      if (!g[m.provider]) g[m.provider] = []
      g[m.provider].push(m)
    }
    return g
  }, [models, providerHealth])

  const onlineProviders = Object.keys(grouped)
  const current = models.find(m => m.id === selectedModel) ?? null

  // Fallback: if selection is stale, auto-correct happens in refreshProviders, but handle UI here too
  const isEmpty = !isProvidersLoading && models.length === 0
  const hasOfflineOllama = providerHealth['ollama'] === 'offline' || (providerHealth['ollama'] === undefined && !isProvidersLoading && !onlineProviders.includes('ollama'))

  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger asChild>
      <button
        aria-label="Seleccionar modelo LLM"
        className="flex items-center gap-2.5 rounded-xl border border-[#2C3037] bg-[#22262D] px-3 py-2 text-sm hover:bg-[#22262D] transition-colors min-w-[160px]"
      >
        {isProvidersLoading ? (
          <>
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-[#2C3037] animate-pulse">
              <RefreshCw className="h-4 w-4 animate-spin text-[#9BA1AA]" />
            </div>
            <div className="text-left hidden sm:block">
              <div className="text-xs font-semibold leading-none text-[#9BA1AA]">Detectando…</div>
              <div className="text-[11px] text-[#9BA1AA]">sondeando proveedores</div>
            </div>
            <div className="h-2 w-2 rounded-full bg-amber-400 animate-pulse" />
          </>
        ) : selectedModel === 'auto' ? (
          <>
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-[#0B62D6]/70 text-white">
              <Sparkles className="h-4 w-4" />
            </div>
            <div className="text-left hidden sm:block">
              <div className="text-xs font-semibold leading-none">Auto (router)</div>
              <div className="text-[11px] text-[#9BA1AA]">elige por gema · infer_hint</div>
            </div>
            <div className="h-2 w-2 rounded-full bg-[#4C9DFF]" />
          </>
        ) : current ? (
          <>
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-[#0B62D6] text-white">
              {(() => { const I = providerIcon[current.provider] ?? Cpu; return <I className="h-4 w-4" /> })()}
            </div>
            <div className="text-left hidden sm:block">
              <div className="text-xs font-semibold leading-none">{current.label}</div>
              <div className="text-[11px] text-[#9BA1AA]">{providerLabel[current.provider] ?? current.provider} · {current.ctx.toLocaleString()} ctx</div>
            </div>
            <div className={cn('h-2 w-2 rounded-full', current.status === 'online' ? 'bg-[#3DDC84] shadow-[0_0_8px_rgba(16,185,129,0.6)]' : 'bg-[#3A4049]')} />
          </>
        ) : (
          <>
            <div className="flex h-7 w-7 items-center justify-center rounded-lg bg-[#22262D] text-[#9BA1AA]">
              <AlertCircle className="h-4 w-4" />
            </div>
            <div className="text-left hidden sm:block">
              <div className="text-xs font-semibold leading-none text-[#EDEEF0]">Sin modelos</div>
              <div className="text-[11px] text-[#9BA1AA]">ningún proveedor en línea</div>
            </div>
            <div className="h-2 w-2 rounded-full bg-[#3A4049]" />
          </>
        )}
        <ChevronDown className={cn('h-4 w-4 text-[#9BA1AA] transition-transform', open && 'rotate-180')} />
      </button>
      </DropdownMenu.Trigger>

      <DropdownMenu.Portal>
        <DropdownMenu.Content
          role="listbox"
          aria-label="Lista de modelos"
          align="end"
          sideOffset={8}
          className="z-[100] w-80 rounded-2xl border border-[#2C3037] bg-[#1E2127] shadow-2xl overflow-hidden max-h-[70vh] overflow-y-auto"
        >
            <div className="flex items-center justify-between px-3 py-2">
              <span className="text-xs font-semibold tracking-widest text-[#9BA1AA]">PROVEEDORES DISPONIBLES</span>
              <button
                onClick={() => refreshProviders()}
                aria-label="Actualizar proveedores"
                className="rounded-lg p-1 hover:bg-[#2C3037] text-[#9BA1AA] hover:text-[#EDEEF0]"
                title="Re-sondear salud de proveedores"
              >
                <RefreshCw className={cn('h-3.5 w-3.5', isProvidersLoading && 'animate-spin')} />
              </button>
            </div>

            {isProvidersLoading && models.length === 0 ? (
              <div className="px-4 py-8 text-center text-sm text-[#9BA1AA]">
                <RefreshCw className="h-5 w-5 animate-spin mx-auto mb-2 text-zinc-600" />
                Detectando proveedores…
                <div className="text-xs mt-1">sondeando :8080 y :11434</div>
              </div>
            ) : isEmpty ? (
              <div className="px-4 py-6 text-center">
                <AlertCircle className="h-6 w-6 mx-auto text-amber-500/70 mb-2" />
                <div className="text-sm font-medium text-[#EDEEF0]">Ningún proveedor en línea</div>
                <div className="text-xs text-[#9BA1AA] mt-1 leading-relaxed">
                  Inicia <span className="font-mono text-[#9BA1AA]">llama.cpp :8080</span> u <span className="font-mono text-[#9BA1AA]">Ollama :11434</span>.<br />
                  Re-sondea cuando estén listos.
                </div>
                {providersError && <div className="mt-2 text-xs font-mono text-red-400/70 break-all">{providersError}</div>}
                <button onClick={() => refreshProviders()} className="mt-3 rounded-xl bg-[#0B62D6] hover:bg-[#4C9DFF] px-3 py-1.5 text-xs font-medium text-white">Reintentar detección</button>
              </div>
            ) : (
              <>
                {/* Opción Auto (router): el backend elige modelo por hint/gema */}
                <div className="px-2 pb-1">
                  <DropdownMenu.Item
                    onSelect={() => setModel('auto')}
                    className={cn('flex w-full items-center gap-2 rounded-xl px-3 py-2.5 text-left hover:bg-[#22262D] transition-colors cursor-pointer focus:outline-none', selectedModel === 'auto' && 'bg-[#0B62D6]/20 border border-[#4C9DFF]/30')}
                  >
                    <Sparkles className="h-3.5 w-3.5 text-[#4C9DFF]" />
                    <div className="flex-1 min-w-0">
                      <div className="text-sm font-medium truncate">Auto (router)</div>
                      <div className="text-xs text-[#9BA1AA]">elige el modelo según la gema / hint</div>
                    </div>
                    {selectedModel === 'auto' && <Check className="h-4 w-4 text-[#4C9DFF]" />}
                  </DropdownMenu.Item>
                </div>
                {onlineProviders.length === 0 ? (
                  <div className="px-4 py-4 text-xs text-[#9BA1AA] text-center">Sin proveedores en línea</div>
                ) : (
                  onlineProviders.map(provider => (
                    <div key={provider} className="px-2 pb-2">
                      <div className="flex items-center gap-1.5 px-2 py-1 text-xs font-medium text-[#9BA1AA] uppercase tracking-widest">
                        {(() => { const I = providerIcon[provider] ?? Cpu; return <I className="h-3.5 w-3.5" /> })()} {providerLabel[provider] ?? provider}
                        <span className="ml-auto flex items-center gap-1 text-[11px] normal-case tracking-normal">
                          <Circle className="h-2 w-2 fill-current text-[#3DDC84]" /> en línea
                        </span>
                      </div>
                      {grouped[provider].map(m => (
                        <DropdownMenu.Item
                          key={m.id}
                          onSelect={() => setModel(m.id)}
                          className={cn('flex w-full items-center gap-2 rounded-xl px-3 py-2.5 text-left hover:bg-[#22262D] transition-colors cursor-pointer focus:outline-none', selectedModel === m.id && 'bg-[#0B62D6]/20 border border-[#4C9DFF]/30')}
                        >
                          <div className="h-1.5 w-1.5 rounded-full bg-[#3DDC84]" />
                          <div className="flex-1 min-w-0">
                            <div className="text-sm font-medium truncate">{m.label}</div>
                            <div className="text-xs text-[#9BA1AA]">{m.id} · {m.ctx.toLocaleString()} ctx</div>
                          </div>
                          {selectedModel === m.id && <Check className="h-4 w-4 text-[#4C9DFF]" />}
                        </DropdownMenu.Item>
                      ))}
                    </div>
                  ))
                )}
                {/* Hidden offline info — not rendered as placeholder group */}
                {hasOfflineOllama && models.length > 0 && (
                  <div className="mx-2 mb-2 rounded-lg bg-[#1E2127] border border-[#2C3037] px-3 py-2 text-xs text-[#9BA1AA]">
                    Ollama no detectado en :11434 — modelos Ollama ocultos.
                  </div>
                )}
              </>
            )}

            <div className="border-t border-[#2C3037] bg-[#1E2127] px-3 py-2 text-xs text-[#9BA1AA] flex items-center justify-between">
              <span>{isEmpty ? 'sin modelos' : `${models.length} modelo${models.length !== 1 ? 's' : ''} · ${onlineProviders.length} proveedor${onlineProviders.length !== 1 ? 'es' : ''}`}</span>
              <span className="text-[#4C9DFF] cursor-pointer hover:text-[#8CC1FF]" onClick={(e) => { e.preventDefault(); refreshProviders() }}>auto-detectar</span>
            </div>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  )
}
