import { useCallback, useEffect, useState } from 'react'
import { Download, RefreshCw, RotateCw, X } from 'lucide-react'
import { apiFetch } from '@/lib/api'

type Estado = {
  ok?: boolean
  enabled?: boolean
  is_repo?: boolean
  branch?: string
  head?: string
  running_head?: string
  behind?: number
  update_available?: boolean
  blocked?: string
  reason?: string
  applied?: boolean
  ui_stale?: boolean
  stale?: string[]
  built?: Record<string, string>
  reload_required?: boolean
  restart_required?: boolean
  commits?: string[]
}

type NexusWindow = {
  nexus?: { restartBackend?: () => Promise<unknown> }
  location: Location
}

const POLL_MS = 10 * 60 * 1000

export function UpdateBanner() {
  const [st, setSt] = useState<Estado | null>(null)
  const [descartado, setDescartado] = useState('')
  const [busy, setBusy] = useState(false)
  const [aviso, setAviso] = useState<string | null>(null)

  const load = useCallback(async () => {
    try {
      const r = await apiFetch('/api/updates/auto', { timeoutMs: 30000 })
      setSt(await r.json())
    } catch {
      // Sin backend o sin repo: no hay nada que avisar.
      setSt(null)
    }
  }, [])

  useEffect(() => {
    void load()
    const iv = setInterval(() => { void load() }, POLL_MS)
    return () => clearInterval(iv)
  }, [load])

  const desfasado = !!st?.running_head && !!st?.head && st.running_head !== st.head
  const nuevo = !!st?.update_available
  const sinCompilar = !!st?.ui_stale
  const visible = !!st?.is_repo && (nuevo || desfasado || sinCompilar)
  const clave = `${st?.head || ''}:${st?.running_head || ''}:${nuevo ? st?.behind : ''}:${sinCompilar ? 1 : 0}`
  if (!visible || clave === descartado) return null

  const w = window as unknown as NexusWindow

  const aplicar = async () => {
    setBusy(true)
    setAviso('Trayendo el código nuevo y recompilando…')
    try {
      const r = await apiFetch('/api/updates/auto/apply', { method: 'POST', timeoutMs: 300000 })
      const j: Estado = await r.json()
      setSt(j)
      if (!j.applied) {
        setAviso(j.blocked || j.reason || 'No se pudo aplicar el update.')
        return
      }
      if (j.ui_stale) {
        setAviso(`Se trajo el código, pero la UI no se pudo recompilar (${j.built?.ui || 'sin detalle'}).`)
        return
      }
      if (j.restart_required && w.nexus?.restartBackend) {
        setAviso('Actualizado. Reiniciando el backend…')
        await w.nexus.restartBackend()
        setTimeout(() => w.location.reload(), 2000)
        return
      }
      setAviso('Actualizado. Recargando la ventana…')
      setTimeout(() => w.location.reload(), 1500)
    } catch (e) {
      setAviso(String(e).slice(0, 160))
    } finally {
      setBusy(false)
    }
  }

  const reiniciar = async () => {
    if (!w.nexus?.restartBackend) {
      setAviso('Cerrá y abrí Nexus para tomar el código nuevo.')
      return
    }
    setBusy(true)
    setAviso('Reiniciando el backend…')
    try {
      await w.nexus.restartBackend()
      setTimeout(() => w.location.reload(), 2000)
    } catch (e) {
      setAviso(String(e).slice(0, 160))
      setBusy(false)
    }
  }

  const titulo = nuevo
    ? `${st?.behind} commit${st?.behind === 1 ? '' : 's'} nuevo${st?.behind === 1 ? '' : 's'} en ${st?.branch || 'el remoto'}`
    : sinCompilar
      ? 'Código nuevo en disco, pero la UI no se recompiló'
      : 'Código nuevo en disco — falta reiniciar el backend'

  return (
    <div className="flex shrink-0 items-center gap-2 border-b border-[#FFB86B]/25 bg-[#FFB86B]/10 px-3 py-1.5 text-[11px] text-[#FFB86B]">
      <RefreshCw className={`h-3.5 w-3.5 shrink-0 ${busy ? 'animate-spin' : ''}`} />
      <span className="font-medium">{titulo}</span>
      {nuevo && !!st?.commits?.length && (
        <span className="hidden truncate font-mono text-[#FFB86B]/70 md:inline">{st.commits[0]}</span>
      )}
      {sinCompilar && (
        <span className="hidden truncate text-[#FFB86B]/80 lg:inline">
          falta npm y node_modules en esta máquina para ver el cambio de UI
        </span>
      )}
      {desfasado && !sinCompilar && (
        <span className="hidden font-mono text-[#FFB86B]/70 lg:inline">corriendo {st?.running_head}</span>
      )}
      {!!st?.blocked && <span className="hidden truncate text-[#FFB86B]/80 xl:inline">· {st.blocked}</span>}
      <div className="ml-auto flex items-center gap-1.5">
        {aviso && <span className="hidden max-w-[320px] truncate text-[#EDEEF0] md:inline">{aviso}</span>}
        {nuevo && !sinCompilar && (
          <button
            onClick={aplicar}
            disabled={busy || !!st?.blocked}
            className="flex items-center gap-1 rounded-md border border-[#FFB86B]/40 bg-[#FFB86B]/15 px-2 py-0.5 hover:bg-[#FFB86B]/25 disabled:opacity-40"
          >
            <Download className="h-3 w-3" /> actualizar
          </button>
        )}
        {desfasado && !nuevo && (
          <button
            onClick={reiniciar}
            disabled={busy}
            className="flex items-center gap-1 rounded-md border border-[#FFB86B]/40 bg-[#FFB86B]/15 px-2 py-0.5 hover:bg-[#FFB86B]/25 disabled:opacity-40"
          >
            <RotateCw className="h-3 w-3" /> reiniciar backend
          </button>
        )}
        <button
          onClick={() => setDescartado(clave)}
          title="Ocultar hasta el próximo cambio"
          aria-label="Ocultar aviso de actualización"
          className="rounded-md p-0.5 text-[#FFB86B]/70 hover:bg-[#FFB86B]/15 hover:text-[#FFB86B]"
        >
          <X className="h-3 w-3" />
        </button>
      </div>
    </div>
  )
}
