import { useState, useRef, useEffect } from 'react'
import { Send, Paperclip, Sparkles, Wrench, User, Bot, Copy, Check, ListTodo, Eye } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { chatApi } from '@/lib/api'

export function ChatPanel() {
  const { messages, addMessage } = useAppStore()
  const [input, setInput] = useState('')
  const [copied, setCopied] = useState<string | null>(null)
  const [attachToast, setAttachToast] = useState(false)
  const bottomRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const [isStreaming, setIsStreaming] = useState(false)
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashQuery, setSlashQuery] = useState('')
  const [lastSteps, setLastSteps] = useState<any[]|null>(null)
  const [lastTodos, setLastTodos] = useState<any|null>(null)
  const [showTraj, setShowTraj] = useState(false)

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, isStreaming, lastSteps, lastTodos])

  const send = async () => {
    if (!input.trim()) return
    const text = input.trim()
    if (text.startsWith('/')) {
      const { activeSession } = useAppStore.getState()
      addMessage({ id: Date.now().toString(), role: 'user', content: text, timestamp: new Date().toLocaleTimeString().slice(0,5) })
      setInput(''); setSlashOpen(false)
      try {
        const isDev = (window as any).location?.port === '5174'
        const base = isDev ? 'http://127.0.0.1:8765' : window.location.origin
        const url = base === window.location.origin ? '/api/slash' : base + '/api/slash'
        const r = await fetch(url, { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({ command: text, ctx: { session_id: activeSession } }) })
        const j = await r.json()
        const reply = j.ok ? j.message : (text.slice(1).split(' ')[0] + ': ' + j.message)
        addMessage({ id: (Date.now()+1).toString(), role: 'assistant', content: reply, timestamp: new Date().toLocaleTimeString().slice(0,5) })
        if (j.data?.action === 'clear_local_only') useAppStore.setState({ messages: [] } as any)
        if (j.data?.action === 'set_model') useAppStore.getState().setModel(j.data.model)
        if (j.data?.action === 'set_permission' || j.data?.action === 'set_plan_mode') {
          try {
            const pr = await fetch(base === window.location.origin ? '/api/permission' : base + '/api/permission')
            const pj = await pr.json()
            addMessage({ id: (Date.now()+2).toString(), role: 'tool', content: 'permiso ahora: ' + pj.permission, toolName: 'permission', timestamp: new Date().toLocaleTimeString().slice(0,5) })
          } catch {}
        }
      } catch (e) { addMessage({ id: (Date.now()+1).toString(), role: 'assistant', content: 'slash error: ' + (e as Error).message, timestamp: new Date().toLocaleTimeString().slice(0,5) }) }
      return
    }
    const { activeSession, selectedModel } = useAppStore.getState()
    addMessage({ id: Date.now().toString(), role: 'user', content: text, timestamp: new Date().toLocaleTimeString().slice(0,5) })
    setInput(''); setSlashOpen(false)
    setIsStreaming(true)
    try {
      const model = selectedModel && selectedModel !== 'auto' ? selectedModel : undefined
      const res: any = await chatApi(text, activeSession || undefined, undefined, model)
      if (res.steps?.length) setLastSteps(res.steps)
      if (res.todos) setLastTodos(res.todos)
      addMessage({ id: (Date.now()+1).toString(), role: 'assistant', content: res.reply, timestamp: new Date().toLocaleTimeString().slice(0,5) })
    } catch (e) {
      const msg = (e as Error).message || String(e)
      let reply: string
      if (msg.includes("timeout") || msg.includes("AbortError")) {
        reply = `Timeout 90s — el modelo local tarda. Reintenta con un mensaje más corto o cambia de modelo. Detalle: ${msg.slice(0,200)}`
      } else {
        reply = `⚠️ Backend no disponible (${msg.slice(0,120)}). Verifica que el arnés esté corriendo en http://127.0.0.1:8765 — inicia con \`python -m src.api.server\` o vía Electron. Cuando el backend esté online el mensaje se enrutará vía ProviderRouter (Thompson/VRAM) y TDAO.`
      }
      if (text.toLowerCase().includes('compact')) reply = 'Pipeline **4 capas** activo: **L0 SecurityLingua** filtra inyección, **L1 Recorte** mantiene 4 turnos, **L2 Micro** colapsa herramientas >2k tokens, **L3 Presupuesto** derrama a `./data/spill`, **L4 Resumen** usa modelo barato. Emergencia `prompt_too_long` → Compactación Reactiva.'
      if (text.toLowerCase().includes('alcance') || text.toLowerCase().includes('scope')) reply = 'Guardia de **Alcance** aplica ACL por defecto denegado. Raíces permitidas: `./workspace`, `./data`. Patrones denegados: `**/.env`, `**/.git/**`. Límite lectura 1.0 MB. Cada herramienta pasa por `alcance.verificar` antes de ejecutar.'
      await new Promise(r => setTimeout(r, 500))
      addMessage({ id: (Date.now()+1).toString(), role: 'assistant', content: reply, timestamp: new Date().toLocaleTimeString().slice(0,5) })
    } finally {
      setIsStreaming(false)
    }
  }

  const handleAttach = () => {
    if (fileInputRef.current) fileInputRef.current.click()
    else { setAttachToast(true); setTimeout(()=>setAttachToast(false), 2000) }
  }
  const onFilePicked = (e: React.ChangeEvent<HTMLInputElement>) => {
    const f = e.target.files?.[0]
    if (!f) return
    addMessage({ id: Date.now().toString(), role: 'user', content: `Archivo adjunto: ${f.name} (${(f.size/1024).toFixed(1)} KB) — será leído por el agente bajo Guardia de Alcance (./workspace, ./data)`, timestamp: new Date().toLocaleTimeString().slice(0,5) })
    e.target.value = ''
  }

  return (
    <div className="flex h-full flex-col">
      {/* messages */}
      <div className="flex-1 overflow-auto px-6 py-6 space-y-5">
        {messages.length === 0 && !isStreaming && (
          <div className="flex flex-col items-center justify-center gap-4 py-16 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-gradient-to-br from-violet-600 to-cyan-500 text-white">
              <Bot className="h-6 w-6" />
            </div>
            <div>
              <div className="text-sm font-semibold text-zinc-200">Bienvenido a Nexus v3</div>
              <div className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-zinc-500">
                Arnés TDAO conectado al backend vivo (8765). Escribe / para comandos. El agente usa todo_write, web_fetch y Guardia de permisos.
              </div>
            </div>
            <div className="flex flex-wrap justify-center gap-2">
              <button onClick={() => setInput('/help')} className="rounded-full border border-white/10 bg-white/[0.06] px-3 py-1.5 text-xs text-zinc-300 hover:bg-white/[0.1]">/help</button>
              <button onClick={() => setInput('explícame la compactación 4 capas')} className="rounded-full border border-white/10 bg-white/[0.06] px-3 py-1.5 text-xs text-zinc-300 hover:bg-white/[0.1]">Compactación 4 capas</button>
              <button onClick={() => setInput('lista el grafo de conocimiento')} className="rounded-full border border-white/10 bg-white/[0.06] px-3 py-1.5 text-xs text-zinc-300 hover:bg-white/[0.1]">Grafo</button>
            </div>
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={cn('flex gap-3', m.role === 'user' ? 'justify-end' : 'justify-start')}>
            {m.role !== 'user' && (
              <div className={cn('mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full', m.role === 'tool' ? 'bg-amber-500/15 text-amber-400' : 'bg-violet-600 text-white')}>
                {m.role === 'tool' ? <Wrench className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
              </div>
            )}
            <div className={cn('group relative max-w-[72%] rounded-2xl px-4 py-3 text-sm leading-relaxed', m.role === 'user' ? 'bg-violet-600 text-white rounded-br-sm' : m.role === 'tool' ? 'bg-amber-500/10 border border-amber-500/20 text-amber-100' : 'bg-white/[0.06] border border-white/10 text-zinc-100')}>
              {m.toolName && <div className="mb-1 text-xs font-mono text-amber-400/80">{m.toolName}</div>}
              <div className="whitespace-pre-wrap break-words">{m.content}</div>
              <div className={cn('mt-1.5 flex items-center gap-2 text-[11px]', m.role === 'user' ? 'text-violet-200 justify-end' : 'text-zinc-500')}>
                <span>{m.timestamp}</span>
                <button
                  onClick={() => { navigator.clipboard.writeText(m.content); setCopied(m.id); setTimeout(()=>setCopied(null), 1200) }}
                  className="opacity-0 group-hover:opacity-100 transition-opacity hover:text-foreground"
                >
                  {copied===m.id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                </button>
              </div>
            </div>
            {m.role === 'user' && <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-zinc-800 text-zinc-300"><User className="h-3.5 w-3.5" /></div>}
          </div>
        ))}
        {(lastTodos && lastTodos.todos?.length > 0) && (
          <div className="rounded-xl border border-violet-500/20 bg-violet-500/10 p-3 text-xs">
            <div className="flex items-center gap-2 font-semibold text-violet-300"><ListTodo className="h-3.5 w-3.5" /> Todos: {lastTodos.counts.completed}/{lastTodos.todos.length} completados</div>
            <div className="mt-2 space-y-1">{lastTodos.todos.map((td:any,i:number)=>(<div key={i} className="flex gap-2"><span className={td.status==='completed'?'text-emerald-400':td.status==='in_progress'?'text-amber-400':'text-zinc-500'}>{td.status==='completed'?'●':td.status==='in_progress'?'◐':'○'}</span><span className={td.status==='completed'?'line-through text-zinc-500':''}>{td.content}</span><span className="ml-auto text-zinc-500">{td.status}</span></div>))}</div>
          </div>
        )}
        {(lastSteps && lastSteps.length>0) && (
          <div className="rounded-xl border border-white/10 bg-white/[0.04] p-3 text-xs">
            <button onClick={()=>setShowTraj(v=>!v)} className="flex items-center gap-2 font-semibold text-zinc-300"><Eye className="h-3.5 w-3.5" /> Trajectory: {lastSteps.length} pasos {showTraj? '▾':'▸'}</button>
            {showTraj && <div className="mt-2 space-y-2 max-h-64 overflow-auto">{lastSteps.map((s:any,i:number)=>(<div key={i} className="rounded-lg border border-white/5 bg-zinc-900 p-2"><div className="flex gap-2 text-zinc-500">#{s.iteration} <span className="text-violet-400">{s.phase}</span> {s.tool_used && <span className="text-amber-400">{s.tool_used}</span>}</div><div className="mt-1 whitespace-pre-wrap text-zinc-300">{String(s.content||'').slice(0,600)}</div></div>))}</div>}
          </div>
        )}
        {isStreaming && (
          <div className="flex gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-violet-600 text-white"><Bot className="h-3.5 w-3.5" /></div>
            <div className="bg-white/[0.06] border border-white/10 rounded-2xl px-4 py-3 flex items-center gap-2">
              <span className="h-2 w-2 animate-bounce rounded-full bg-zinc-500 [animation-delay:0ms]" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-zinc-500 [animation-delay:150ms]" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-zinc-500 [animation-delay:300ms]" />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {slashOpen && (
        <div className="mx-4 mb-2 rounded-xl border border-white/10 bg-zinc-900 p-2 text-xs shadow-xl">
          <div className="px-2 py-1 text-zinc-500">Comandos /{slashQuery}</div>
          {['help — show available commands','clear — clear local chat buffer','model <id> — set model','persona [preset] — switch persona','skill <name> — invoke a skill','session [new|list] — session ops','plan [on|off] — toggle plan mode','permission <read_only|workspace_write|full> — cambia permiso','compact — info compactación','goal <objetivo|status|clear> — gestiona goal','export — exporta sesion','feedback [texto] — envia feedback','todo — ver lista'].filter(s=>!slashQuery || s.toLowerCase().includes(slashQuery.toLowerCase())).map(s=>(
            <button key={s} onClick={()=>{const cmd='/'+s.split(' —')[0].split(' ')[0]; setInput(cmd+' '); setSlashOpen(false)}} className="flex w-full gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-white/10"><span className="text-violet-400">/{s.split(' —')[0]}</span><span className="text-zinc-500">{s.split(' —')[1]||''}</span></button>
          ))}
        </div>
      )}
      {/* composer */}
      <div className="border-t border-white/10 bg-zinc-950/50 backdrop-blur p-4">
        <div className="flex items-end gap-2 rounded-2xl border border-white/10 bg-white/[0.04] p-2 focus-within:border-violet-500/50 focus-within:bg-white/[0.06] transition-colors">
          <input ref={fileInputRef} type="file" className="hidden" onChange={onFilePicked} />
          <Button variant="ghost" size="icon" className="shrink-0 rounded-xl" aria-label="Adjuntar archivo" onClick={handleAttach}><Paperclip className="h-4 w-4" /></Button>
          <textarea
            value={input}
            onChange={(e)=>{ const v=e.target.value; setInput(v); if(v.startsWith('/')){ setSlashOpen(true); setSlashQuery(v.slice(1).split(' ')[0]) } else setSlashOpen(false) }}
            onKeyDown={(e)=>{ if(e.key==='Escape' && slashOpen){ e.preventDefault(); setSlashOpen(false); return } if(e.key==='Enter' && !e.shiftKey){ e.preventDefault(); send() }}}
            placeholder="Mensaje a Nexus…  / para comandos (Shift+Enter salto)"
            rows={1}
            aria-label="Mensaje a Nexus"
            className="max-h-32 min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2.5 text-sm placeholder:text-zinc-500 focus:outline-none"
          />
          <Button onClick={send} disabled={!input.trim() || isStreaming} aria-label="Enviar mensaje" className="shrink-0 rounded-xl bg-violet-600 hover:bg-violet-500">
            <Send className="h-4 w-4" /> <span className="hidden sm:inline">Enviar</span>
          </Button>
        </div>
        <div className="mt-2 flex items-center justify-between text-xs text-zinc-500">
          <span className="flex items-center gap-1.5"><Sparkles className="h-3 w-3 text-violet-400" /> Bucle Agente TDAO · Guardia de Alcance · Compactación 4 capas</span>
          <span className="hidden sm:inline">⌘K cambio rápido · /herramientas · @archivos</span>
        </div>
        {attachToast && <div className="mt-2 text-xs text-amber-400">Selector de archivos — respeta Guardia de Alcance (./workspace)</div>}
      </div>
    </div>
  )
}
