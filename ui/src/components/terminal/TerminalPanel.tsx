import { useEffect, useRef, useState } from 'react'
import { Terminal as TermIcon, Trash2, Copy, Maximize2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/store/app-store'
import { execCmd } from '@/lib/api'

export function TerminalPanel() {
  const { terminalLines, appendTerminal, clearTerminal } = useAppStore()
  const [input, setInput] = useState('')
  const [history, setHistory] = useState<string[]>([])
  const [histIdx, setHistIdx] = useState<number>(-1)
  const [busy, setBusy] = useState(false)
  const [cwd] = useState('./workspace')
  const scrollRef = useRef<HTMLDivElement>(null)

  useEffect(()=>{ scrollRef.current?.scrollTo(0, scrollRef.current.scrollHeight) }, [terminalLines])

  const run = async () => {
    if(!input.trim() || busy) return
    const cmd = input.trim()
    setHistory(h=>[...h, cmd])
    setHistIdx(-1)
    if(cmd==='clear' || cmd==='limpiar' || cmd==='cls') {
      clearTerminal()
      setInput('')
      return
    }
    appendTerminal(`$ ${cmd}`)
    setInput('')
    setBusy(true)
    try {
      const r = await execCmd(cmd, cwd, 15)
      const out = r.output?.trimEnd()
      if(out) appendTerminal(out)
      else appendTerminal(r.ok ? '(sin salida)' : `(exit ${r.exit_code})`)
      if(!r.ok && r.exit_code !== 0) appendTerminal(`[exit ${r.exit_code}]`)
    } catch(e){
      const msg = (e as Error).message || String(e)
      // fallback local para comandos sin backend
      if(msg.includes('404') || msg.includes('Failed to fetch')){
        appendTerminal(`(backend no disponible: ${msg.slice(0,120)} — revisa que el servidor esté activo)`)
      } else {
        appendTerminal(`error: ${msg.slice(0,400)}`)
      }
    } finally { setBusy(false) }
  }

  return (
    <div className="flex h-full flex-col bg-[#0a0a0f]">
      <div className="flex items-center gap-2 border-b border-[#2C3037] px-3 py-2 bg-[#1E2127]/70">
        <TermIcon className="h-4 w-4 text-[#9BA1AA]" />
        <span className="text-xs font-mono text-[#EDEEF0]">nexus · {cwd} — Alcance jail</span>
        {busy && <span className="ml-2 text-xs text-amber-400 animate-pulse">ejecutando…</span>}
        <div className="ml-auto flex gap-1">
          <Button variant="ghost" size="xs" aria-label="Copiar salida" onClick={()=>{ navigator.clipboard.writeText(terminalLines.join('\n')).catch(()=>{}) }}><Copy className="h-3.5 w-3.5" /></Button>
          <Button variant="ghost" size="xs" aria-label="Limpiar terminal" onClick={()=> clearTerminal()}><Trash2 className="h-3.5 w-3.5" /></Button>
          <Button variant="ghost" size="xs"><Maximize2 className="h-3.5 w-3.5" /></Button>
        </div>
      </div>

      <div ref={scrollRef} className="flex-1 overflow-auto p-4 font-mono text-xs leading-5">
        {terminalLines.map((line,i)=>(
          <div key={i} className={line.startsWith('$') ? 'text-[#8CC1FF]' : line.startsWith('✓') ? 'text-[#3DDC84]' : line.startsWith('[TDAO]') ? 'text-[#8CC1FF]' : line.startsWith('error') || line.startsWith('(backend') ? 'text-red-400' : 'text-[#EDEEF0]'}>
            {line}
          </div>
        ))}
        <div className="mt-2 flex items-center gap-2">
          <span className="text-[#3DDC84]">❯</span>
          <input
            value={input}
            onChange={e=>setInput(e.target.value)}
            onKeyDown={e=>{
              if(e.key==='Enter') run()
              if(e.key==='ArrowUp'){ e.preventDefault(); if(history.length){ const idx = histIdx===-1 ? history.length-1 : Math.max(0, histIdx-1); setHistIdx(idx); setInput(history[idx]) } }
              if(e.key==='ArrowDown'){ e.preventDefault(); if(histIdx>=0){ const idx = histIdx+1; if(idx>=history.length){ setHistIdx(-1); setInput('')} else { setHistIdx(idx); setInput(history[idx]) } } }
              if(e.key==='l' && e.ctrlKey){ e.preventDefault(); clearTerminal()} }}
            placeholder={busy ? 'ejecutando…' : 'comando (ls, git status, python --version)…'}
            aria-label="Entrada de terminal"
            className="flex-1 bg-transparent text-[#EDEEF0] placeholder:text-zinc-600 focus:outline-none disabled:opacity-50"
            autoFocus
            disabled={busy}
          />
        </div>
        <div className="mt-3 text-[11px] text-[#9BA1AA] border-t border-[#2C3037] pt-2">
          Shell — cada comando corre en <span className="text-[#EDEEF0] font-mono">{cwd}</span> vía <span className="text-amber-400">POST /api/exec</span>. En modo jail se bloquea encadenar/redirigir (<span className="font-mono">; | &amp; &gt; $()</span>) y rutas fuera de <span className="font-mono">./workspace, ./data</span>. Para shell completo: Ajustes → Permisos → <span className="text-[#EDEEF0]">full</span>.
        </div>
      </div>
    </div>
  )
}
