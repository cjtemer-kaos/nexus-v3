import { useCallback, useEffect, useState } from 'react'
import { useAppStore } from '@/store/app-store'
import { usageApi, type Usage } from '@/lib/api'
import { ScrollArea } from '@/components/ui/scroll-area'
import { Button } from '@/components/ui/button'
import { Coins, RefreshCw, Loader2 } from 'lucide-react'

export function UsagePanel() {
  const { activeSession } = useAppStore()
  const [scope, setScope] = useState<'session' | 'global'>('session')
  const [u, setU] = useState<Usage | null>(null)
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    setLoading(true); setError(null)
    try { setU(await usageApi(scope === 'session' ? activeSession : '')) }
    catch (e) { setU(null); setError((e as Error).message) } finally { setLoading(false) }
  }, [scope, activeSession])

  useEffect(() => { void load() }, [load])

  const maxTok = Math.max(1, ...(u?.by_model.map((m) => m.tokens) ?? [1]))

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-2 border-b border-[#2C3037] px-4 py-3">
        <Coins className="h-4 w-4 text-[#4C9DFF]" />
        <span className="text-sm font-medium">Uso y costo</span>
        <div className="ml-3 flex gap-1">
          <button onClick={() => setScope('session')} className={`rounded-md px-2 py-0.5 text-xs ${scope === 'session' ? 'bg-[#2C3037] text-[#EDEEF0]' : 'text-[#9BA1AA]'}`}>Sesión</button>
          <button onClick={() => setScope('global')} className={`rounded-md px-2 py-0.5 text-xs ${scope === 'global' ? 'bg-[#2C3037] text-[#EDEEF0]' : 'text-[#9BA1AA]'}`}>Global</button>
        </div>
        <Button variant="ghost" size="icon" aria-label="Recargar" className="ml-auto" disabled={loading} onClick={() => void load()}>
          {loading ? <Loader2 className="h-4 w-4 animate-spin" /> : <RefreshCw className="h-4 w-4" />}
        </Button>
      </div>

      {error && (
        <div className="flex items-center gap-2 border-b border-[#FF6B6B]/30 bg-[#FF6B6B]/10 px-4 py-2 text-xs text-[#FFA8A8]">
          <span className="flex-1">No se pudo leer el uso: {error}</span>
          <button onClick={() => void load()} className="underline hover:text-white">Reintentar</button>
        </div>
      )}

      <ScrollArea className="flex-1">
        <div className="p-4">
          <div className="grid grid-cols-3 gap-3">
            <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3">
              <div className="text-[11px] text-[#9BA1AA]">Mensajes</div>
              <div className="text-xl font-semibold">{u?.messages ?? 0}</div>
            </div>
            <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3">
              <div className="text-[11px] text-[#9BA1AA]">Tokens</div>
              <div className="text-xl font-semibold">{(u?.tokens ?? 0).toLocaleString()}</div>
            </div>
            <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3">
              <div className="text-[11px] text-[#9BA1AA]">Costo estimado</div>
              <div className="text-xl font-semibold text-[#3DDC84]">${(u?.cost?.total_usd ?? 0).toFixed(4)}</div>
            </div>
          </div>

          <div className="mt-4 text-[11px] uppercase tracking-widest text-[#9BA1AA]">Por modelo</div>
          <div className="mt-2 space-y-1.5">
            {(u?.by_model ?? []).length === 0 && !error && !loading && <div className="text-xs text-[#9BA1AA]">Sin datos aún (los tokens se registran por turno del agente).</div>}
            {(u?.by_model ?? []).map((m) => (
              <div key={m.model} className="rounded-lg border border-[#2C3037] bg-[#1E2127] px-3 py-2">
                <div className="flex items-center gap-2 text-xs">
                  <span className="font-mono text-[#8CC1FF]">{m.model}</span>
                  <span className="ml-auto text-[#9BA1AA]">{m.messages} msg · {m.tokens.toLocaleString()} tok · ${(u?.cost?.per_model?.[m.model] ?? 0).toFixed(4)}</span>
                </div>
                <div className="mt-1 h-1.5 rounded-full bg-[#2C3037]">
                  <div className="h-1.5 rounded-full bg-[#4C9DFF]" style={{ width: `${Math.round((m.tokens / maxTok) * 100)}%` }} />
                </div>
              </div>
            ))}
          </div>
          <div className="mt-3 text-[10px] text-[#9BA1AA]">Local (Ollama/llama.cpp) = $0. Cloud = tarifa configurable (`cost.rates`).</div>
        </div>
      </ScrollArea>
    </div>
  )
}
