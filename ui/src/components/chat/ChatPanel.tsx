import { useState, useRef, useEffect, useCallback } from 'react'
import { Send, Paperclip, Sparkles, Wrench, User, Bot, Copy, Check, ListTodo, Eye, X, Camera, FileText, Database } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { chatApi, chatStreamApi, apiFetch, uploadApi, type UploadResult } from '@/lib/api'
import { VoiceMic } from '@/components/chat/VoiceMic'

type Pending = { key: string; file: File; preview?: string }

const now = () => new Date().toLocaleTimeString().slice(0, 5)
const uid = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`)

export function ChatPanel() {
  const { messages, addMessage, activeSession, loadMessages } = useAppStore()
  const [input, setInput] = useState('')
  const [copied, setCopied] = useState<string | null>(null)
  const [attachToast, setAttachToast] = useState<string | null>(null)
  const [pending, setPending] = useState<Pending[]>([])
  const bottomRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [isStreaming, setIsStreaming] = useState(false)
  const busyRef = useRef(false)  // guard anti doble-send (el state no es sincrónico)
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashQuery, setSlashQuery] = useState('')
  const [lastSteps, setLastSteps] = useState<any[]|null>(null)
  const [lastTodos, setLastTodos] = useState<any|null>(null)
  const [showTraj, setShowTraj] = useState(false)
  const [distillMsg, setDistillMsg] = useState<string | null>(null)

  // Historial: al cambiar de sesión (o al cargar la primera) traer mensajes del server
  useEffect(() => {
    // Resetear estado de streaming al cambiar de sesión (no bloquear la nueva).
    setIsStreaming(false)
    busyRef.current = false
    void loadMessages(activeSession)
  }, [activeSession, loadMessages])

  useEffect(() => { bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, isStreaming, lastSteps, lastTodos])

  const toast = useCallback((msg: string) => {
    setAttachToast(msg)
    setTimeout(() => setAttachToast(null), 3200)
  }, [])

  /* ── adjuntos ─────────────────────────────────────────────── */

  const addFiles = useCallback((files: FileList | File[]) => {
    const list = Array.from(files)
    if (!list.length) return
    const room = 8 - pending.length
    if (room <= 0) { toast('Máximo 8 adjuntos por mensaje'); return }
    const accepted = list.slice(0, room)
    const next: Pending[] = accepted.map((file) => ({ key: `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`, file }))
    for (const p of next) {
      if (p.file.type.startsWith('image/')) {
        const r = new FileReader()
        r.onload = () => setPending((cur) => cur.map((x) => (x.key === p.key ? { ...x, preview: String(r.result) } : x)))
        r.readAsDataURL(p.file)
      }
    }
    setPending((cur) => [...cur, ...next])
  }, [pending.length, toast])

  // Ctrl+V global: pega donde esté el foco dentro del chat
  useEffect(() => {
    const onPaste = (e: ClipboardEvent) => {
      const files = e.clipboardData?.files
      if (files && files.length > 0) { e.preventDefault(); addFiles(files) }
    }
    window.addEventListener('paste', onPaste)
    return () => window.removeEventListener('paste', onPaste)
  }, [addFiles])

  const captureScreen = async () => {
    try {
      const stream = await navigator.mediaDevices.getDisplayMedia({ video: true })
      const track = stream.getVideoTracks()[0]
      const video = document.createElement('video')
      video.srcObject = stream
      video.muted = true
      await video.play()
      await new Promise((r) => setTimeout(r, 250))
      const canvas = document.createElement('canvas')
      canvas.width = video.videoWidth || 1280
      canvas.height = video.videoHeight || 720
      canvas.getContext('2d')?.drawImage(video, 0, 0)
      track.stop()
      stream.getTracks().forEach((t) => t.stop())
      const blob: Blob = await new Promise((resolve) => canvas.toBlob((b) => resolve(b || new Blob()), 'image/png'))
      const f = new File([blob], `captura-${Date.now()}.png`, { type: 'image/png' })
      addFiles([f])
    } catch (e) {
      const msg = (e as Error).name === 'NotAllowedError' ? 'Captura cancelada' : 'Captura no disponible: ' + (e as Error).message
      toast(msg)
    }
  }

  const onFilePicked = (e: React.ChangeEvent<HTMLInputElement>) => {
    if (e.target.files?.length) addFiles(e.target.files)
    e.target.value = ''
  }

  const removePending = (key: string) => setPending((cur) => cur.filter((p) => p.key !== key))

  const onTranscript = (text: string) => {
    setInput((cur) => (cur ? `${cur} ${text}` : text))
    textareaRef.current?.focus()
  }

  /* ── envío ────────────────────────────────────────────────── */

  const send = async () => {
    if ((!input.trim() && pending.length === 0) || isStreaming || busyRef.current) return
    busyRef.current = true
    const text = input.trim()

    // subir adjuntos pendientes
    let uploads: UploadResult[] = []
    if (pending.length) {
      setIsStreaming(true)
      try {
        uploads = await Promise.all(pending.map((p) => uploadApi(p.file)))
      } catch (e) {
        setIsStreaming(false)
        busyRef.current = false
        toast('Fallo al subir adjunto: ' + (e as Error).message.slice(0, 140))
        return
      }
      setPending([])
      setIsStreaming(false)
      const imgs = pending.filter((p) => p.file.type.startsWith('image/'))
      addMessage({
        id: uid(),
        role: 'user',
        content: text || `📎 ${uploads.map((u) => u.name).join(', ')}`,
        timestamp: now(),
        images: imgs.map((p) => p.preview).filter(Boolean) as string[],
      })
    }

    if (text.startsWith('/')) {
      const { activeSession } = useAppStore.getState()
      const stillActive = () => useAppStore.getState().activeSession === activeSession
      if (!uploads.length) addMessage({ id: uid(), role: 'user', content: text, timestamp: now() })
      setInput(''); setSlashOpen(false)
      try {
        const r = await apiFetch('/api/slash', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: text, ctx: { session_id: activeSession } }) })
        const j = await r.json()
        const reply = j.ok ? j.message : (text.slice(1).split(' ')[0] + ': ' + j.message)
        if (stillActive()) addMessage({ id: uid(), role: 'assistant', content: reply, timestamp: now() })
        if (j.data?.action === 'clear_local_only') useAppStore.setState({ messages: [] } as any)
        if (j.data?.action === 'set_model') useAppStore.getState().setModel(j.data.model)
        if (j.data?.action === 'set_permission' || j.data?.action === 'set_plan_mode') {
          try {
            const pr = await apiFetch('/api/permission')
            const pj = await pr.json()
            if (stillActive()) addMessage({ id: uid(), role: 'tool', content: 'permiso ahora: ' + pj.permission, toolName: 'permission', timestamp: now() })
          } catch {}
        }
      } catch (e) { if (stillActive()) addMessage({ id: uid(), role: 'assistant', content: 'slash error: ' + (e as Error).message, timestamp: now() }) }
      busyRef.current = false
      return
    }

    let { activeSession, selectedModel } = useAppStore.getState()
    if (!activeSession) activeSession = await useAppStore.getState().ensureSession()
    const stillActive = () => useAppStore.getState().activeSession === activeSession
    if (!uploads.length) addMessage({ id: uid(), role: 'user', content: text, timestamp: now() })
    setInput(''); setSlashOpen(false)
    setIsStreaming(true)
    try {
      const model = selectedModel && selectedModel !== 'auto' ? selectedModel : undefined
      const { updateMessage } = useAppStore.getState()
      const msgId = uid()
      let acc = ''
      let placed = false
      const pushDelta = (t: string) => {
        if (!stillActive()) return
        acc += t
        if (!placed) { addMessage({ id: msgId, role: 'assistant', content: acc, timestamp: now() }); placed = true }
        else updateMessage(msgId, acc)
      }
      // Streaming solo si no hay adjuntos (el stream no los soporta); si falla, cae al POST.
      let streamed = false
      if (uploads.length === 0) {
        try {
          const sres = await chatStreamApi(text, { sessionId: activeSession || undefined, model, onDelta: pushDelta })
          const finalReply = sres.reply || acc || '[sin respuesta]'
          if (stillActive()) {
            if (placed) updateMessage(msgId, finalReply)
            else addMessage({ id: msgId, role: 'assistant', content: finalReply, timestamp: now() })
          }
          streamed = true
        } catch {
          streamed = false
        }
      }
      if (!streamed) {
        const res: any = await chatApi(text, activeSession || undefined, undefined, model,
          uploads.map(({ id, url, name, mime, is_image }) => ({ id, url, name, mime, is_image })))
        if (res.steps?.length && stillActive()) setLastSteps(res.steps)
        if (res.todos && stillActive()) setLastTodos(res.todos)
        // si el usuario cambió de sesión mientras respondía, el historial de la sesión
        // destino se recarga con loadMessages — no mezclar la respuesta aquí
        if (stillActive()) {
          // si ya se mostraron deltas, ACTUALIZAR (no duplicar con el mismo id)
          if (placed) updateMessage(msgId, res.reply)
          else addMessage({ id: msgId, role: 'assistant', content: res.reply, timestamp: now() })
        }
      }
      void useAppStore.getState().loadSessions()
    } catch (e) {
      const msg = (e as Error).message || String(e)
      let reply: string
      if (msg.includes("timeout") || msg.includes("AbortError")) {
        reply = `Timeout 90s — el modelo local tarda. Reintenta con un mensaje más corto o cambia de modelo. Detalle: ${msg.slice(0,200)}`
      } else {
        reply = `⚠️ Backend no disponible (${msg.slice(0,120)}). Inicia el servidor con \`python -m src.api.server\` (o vía Electron) y reintenta.`
      }
      if (text.toLowerCase().includes('compact')) reply = 'Pipeline **4 capas** activo: **L0 SecurityLingua** filtra inyección, **L1 Recorte** mantiene 4 turnos, **L2 Micro** colapsa herramientas >2k tokens, **L3 Presupuesto** derrama a `./data/spill`, **L4 Resumen** usa modelo barato. Emergencia `prompt_too_long` → Compactación Reactiva.'
        if (text.toLowerCase().includes('alcance') || text.toLowerCase().includes('scope')) reply = 'Guardia de **Alcance** aplica ACL. Con permiso `workspace_write` las raíces son `./workspace` y `./data`; con permiso `full` (Ajustes → Permisos) hay acceso a toda la PC. Patrones denegados siempre: `**/.env`, `**/*.key`. Límite lectura 1.0 MB. Cada herramienta pasa por `scope.check_path`/`check_tool` antes de ejecutar.'
      await new Promise(r => setTimeout(r, 500))
      if (stillActive()) addMessage({ id: uid(), role: 'assistant', content: reply, timestamp: now() })
    } finally {
      busyRef.current = false
      setIsStreaming(false)
    }
  }

  const distillNow = useCallback(async () => {
    const { activeSession } = useAppStore.getState()
    if (!activeSession) { setDistillMsg('sin sesión activa'); setTimeout(() => setDistillMsg(null), 2500); return }
    setDistillMsg('destilando…')
    try {
      const r = await apiFetch(`/api/sessions/${activeSession}/distill`, { method: 'POST' })
      const j: any = await r.json().catch(() => ({}))
      const res = j?.result ?? j ?? {}
      setDistillMsg(`destilado: ${res.distilled ?? 0} piezas · ${res.llm_items ?? 0} items LLM`)
    } catch (e: any) {
      setDistillMsg(`error: ${e?.message || 'falló'}`)
    }
    setTimeout(() => setDistillMsg(null), 4000)
  }, [])

  return (
    <div className="flex h-full flex-col">
      {/* messages */}
      <div className="flex-1 overflow-auto px-6 py-6 space-y-5">
        {messages.length === 0 && !isStreaming && (
          <div className="flex flex-col items-center justify-center gap-4 py-16 text-center">
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl bg-[#0B62D6] text-white">
              <Bot className="h-6 w-6" />
            </div>
            <div>
              <div className="text-sm font-semibold text-[#EDEEF0]">Bienvenido a Nexus v3</div>
              <div className="mx-auto mt-1 max-w-md text-xs leading-relaxed text-[#9BA1AA]">
                Arnés TDAO conectado al backend. Escribe / para comandos · Ctrl+V pega archivos · el micrófono transcribe con el STT local.
              </div>
            </div>
            <div className="flex flex-wrap justify-center gap-2">
              <button onClick={() => setInput('/help')} className="rounded-full border border-[#2C3037] bg-[#22262D] px-3 py-1.5 text-xs text-[#9BA1AA] hover:text-[#EDEEF0]">/help</button>
              <button onClick={() => setInput('explícame la compactación 4 capas')} className="rounded-full border border-[#2C3037] bg-[#22262D] px-3 py-1.5 text-xs text-[#9BA1AA] hover:text-[#EDEEF0]">Compactación 4 capas</button>
              <button onClick={() => setInput('lista el grafo de conocimiento')} className="rounded-full border border-[#2C3037] bg-[#22262D] px-3 py-1.5 text-xs text-[#9BA1AA] hover:text-[#EDEEF0]">Grafo</button>
            </div>
          </div>
        )}
        {messages.map((m) => (
          <div key={m.id} className={cn('flex gap-3', m.role === 'user' ? 'justify-end' : 'justify-start')}>
            {m.role !== 'user' && (
              <div className={cn('mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full', m.role === 'tool' ? 'bg-[#FFB86B]/15 text-[#FFB86B]' : 'bg-[#0B62D6] text-white')}>
                {m.role === 'tool' ? <Wrench className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
              </div>
            )}
            <div className={cn('group relative max-w-[72%] rounded-2xl px-4 py-3 text-sm leading-relaxed', m.role === 'user' ? 'bg-[#0B62D6] text-white rounded-br-sm' : m.role === 'tool' ? 'bg-[#FFB86B]/10 border border-[#FFB86B]/20 text-[#FFE0B0]' : 'bg-[#1E2127] border border-[#2C3037] text-[#EDEEF0]')}>
              {m.toolName && <div className="mb-1 text-xs font-mono text-[#FFB86B]/80">{m.toolName}</div>}
              {m.images && m.images.length > 0 && (
                <div className="mb-2 flex flex-wrap gap-2">
                  {m.images.map((src, i) => (
                    <img key={i} src={src} alt={`adjunto ${i + 1}`} className="h-24 w-24 rounded-lg border border-[#2C3037] object-cover" />
                  ))}
                </div>
              )}
              <div className="whitespace-pre-wrap break-words">{m.content}</div>
              <div className={cn('mt-1.5 flex items-center gap-2 text-[11px]', m.role === 'user' ? 'text-[#B9DBFF] justify-end' : 'text-[#9BA1AA]')}>
                <span>{m.timestamp}</span>
                <button
                  onClick={() => { void navigator.clipboard.writeText(m.content).catch(()=>{}); setCopied(m.id); setTimeout(()=>setCopied(null), 1200) }}
                  aria-label={copied===m.id ? 'Copiado' : 'Copiar mensaje'}
                  className="opacity-0 group-hover:opacity-100 transition-opacity hover:text-[#EDEEF0]"
                >
                  {copied===m.id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                </button>
              </div>
            </div>
            {m.role === 'user' && <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[#22262D] text-[#9BA1AA]"><User className="h-3.5 w-3.5" /></div>}
          </div>
        ))}
        {(lastTodos && lastTodos.todos?.length > 0) && (
          <div className="rounded-xl border border-[#4C9DFF]/20 bg-[#4C9DFF]/10 p-3 text-xs">
            <div className="flex items-center gap-2 font-semibold text-[#8CC1FF]"><ListTodo className="h-3.5 w-3.5" /> Todos: {lastTodos.counts?.completed ?? 0}/{lastTodos.todos.length} completados</div>
            <div className="mt-2 space-y-1">{lastTodos.todos.map((td:any,i:number)=>(<div key={i} className="flex gap-2"><span className={td.status==='completed'?'text-[#3DDC84]':td.status==='in_progress'?'text-[#FFB86B]':'text-[#9BA1AA]'}>{td.status==='completed'?'●':td.status==='in_progress'?'◐':'○'}</span><span className={td.status==='completed'?'line-through text-[#9BA1AA]':''}>{td.content}</span><span className="ml-auto text-[#9BA1AA]">{td.status}</span></div>))}</div>
          </div>
        )}
        {(lastSteps && lastSteps.length>0) && (
          <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-xs">
            <button onClick={()=>setShowTraj(v=>!v)} className="flex items-center gap-2 font-semibold text-[#EDEEF0]"><Eye className="h-3.5 w-3.5" /> Trajectory: {lastSteps.length} pasos {showTraj? '▾':'▸'}</button>
            {showTraj && <div className="mt-2 space-y-2 max-h-64 overflow-auto">{lastSteps.map((s:any,i:number)=>(<div key={i} className="rounded-lg border border-[#2C3037] bg-[#15171B] p-2"><div className="flex gap-2 text-[#9BA1AA]">#{s.iteration} <span className="text-[#4C9DFF]">{s.phase}</span> {s.tool_used && <span className="text-[#FFB86B]">{s.tool_used}</span>}</div><div className="mt-1 whitespace-pre-wrap text-[#EDEEF0]">{String(s.content||'').slice(0,600)}</div></div>))}</div>}
          </div>
        )}
        {isStreaming && (
          <div className="flex gap-3">
            <div className="flex h-7 w-7 items-center justify-center rounded-full bg-[#0B62D6] text-white"><Bot className="h-3.5 w-3.5" /></div>
            <div className="bg-[#1E2127] border border-[#2C3037] rounded-2xl px-4 py-3 flex items-center gap-2">
              <span className="h-2 w-2 animate-bounce rounded-full bg-[#9BA1AA] [animation-delay:0ms]" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-[#9BA1AA] [animation-delay:150ms]" />
              <span className="h-2 w-2 animate-bounce rounded-full bg-[#9BA1AA] [animation-delay:300ms]" />
            </div>
          </div>
        )}
        <div ref={bottomRef} />
      </div>

      {slashOpen && (
        <div className="mx-4 mb-2 rounded-xl border border-[#2C3037] bg-[#22262D] p-2 text-xs shadow-xl">
          <div className="px-2 py-1 text-[#9BA1AA]">Comandos /{slashQuery}</div>
          {['help — show available commands','clear — clear local chat buffer','model <id> — set model','persona [preset] — switch persona','skill <name> — invoke a skill','session [new|list] — session ops','plan [on|off] — toggle plan mode','permission <read_only|workspace_write|full> — cambia permiso','compact — info compactación','goal <objetivo|status|clear> — gestiona goal','export — exporta sesion','feedback [texto] — envia feedback','todo — ver lista'].filter(s=>!slashQuery || s.toLowerCase().includes(slashQuery.toLowerCase())).map(s=>(
            <button key={s} onClick={()=>{const cmd='/'+s.split(' —')[0].split(' ')[0]; setInput(cmd+' '); setSlashOpen(false); textareaRef.current?.focus()}} className="flex w-full gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-[#2C3037]"><span className="text-[#4C9DFF]">/{s.split(' —')[0]}</span><span className="text-[#9BA1AA]">{s.split(' —')[1]||''}</span></button>
          ))}
        </div>
      )}

      {/* composer DECK */}
      <div className="relative border-t border-[#2C3037] bg-[#15171B] p-4">
        {/* previews de adjuntos */}
        {pending.length > 0 && (
          <div className="mb-2 flex flex-wrap gap-2">
            {pending.map((p) => (
              <div key={p.key} className="group relative flex items-center gap-2 rounded-xl border border-[#2C3037] bg-[#1E2127] py-1.5 pl-1.5 pr-8 text-xs">
                {p.preview ? (
                  <img src={p.preview} alt={p.file.name} className="h-9 w-9 rounded-lg object-cover" />
                ) : (
                  <span className="flex h-9 w-9 items-center justify-center rounded-lg bg-[#22262D] text-[#4C9DFF]"><FileText className="h-4 w-4" /></span>
                )}
                <div className="max-w-[140px]">
                  <div className="truncate font-medium text-[#EDEEF0]">{p.file.name}</div>
                  <div className="text-[10px] text-[#9BA1AA]">{(p.file.size/1024).toFixed(1)} KB</div>
                </div>
                <button onClick={() => removePending(p.key)} aria-label={`Quitar ${p.file.name}`} className="absolute right-1.5 top-1/2 -translate-y-1/2 rounded-md p-1 text-[#9BA1AA] hover:bg-[#2C3037] hover:text-[#FF6259]">
                  <X className="h-3.5 w-3.5" />
                </button>
              </div>
            ))}
          </div>
        )}

        <div className="flex items-end gap-2 rounded-2xl border border-[#2C3037] bg-[#1E2127] p-2 focus-within:border-[#4C9DFF]/50 transition-colors">
          <input ref={fileInputRef} type="file" multiple className="hidden" onChange={onFilePicked} accept="*/*" />
          <Button variant="ghost" size="icon" className="shrink-0 rounded-xl text-[#9BA1AA] hover:text-[#EDEEF0]" aria-label="Adjuntar archivo" onClick={() => fileInputRef.current?.click()}><Paperclip className="h-4 w-4" /></Button>
          <Button variant="ghost" size="icon" className="shrink-0 rounded-xl text-[#9BA1AA] hover:text-[#EDEEF0]" aria-label="Capturar pantalla" onClick={captureScreen}><Camera className="h-4 w-4" /></Button>
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e)=>{ const v=e.target.value; setInput(v); if(v.startsWith('/')){ setSlashOpen(true); setSlashQuery(v.slice(1).split(' ')[0]) } else setSlashOpen(false) }}
            onKeyDown={(e)=>{ if(e.key==='Escape' && slashOpen){ e.preventDefault(); setSlashOpen(false); return } if(e.key==='Enter' && !e.shiftKey && !e.nativeEvent.isComposing){ e.preventDefault(); send() }}}
            placeholder="Mensaje a Nexus…  / comandos · Ctrl+V adjunta · Shift+Enter salto"
            rows={1}
            aria-label="Mensaje a Nexus"
            className="max-h-32 min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2.5 text-sm placeholder:text-[#9BA1AA] focus:outline-none text-[#EDEEF0]"
          />
          <div className="relative flex shrink-0 items-center">
            <VoiceMic onTranscript={onTranscript} />
          </div>
          <Button onClick={send} disabled={(!input.trim() && pending.length===0) || isStreaming} aria-label="Enviar mensaje" className="shrink-0 rounded-xl bg-[#0B62D6] hover:bg-[#4C9DFF] text-white">
            <Send className="h-4 w-4" /> <span className="hidden sm:inline">Enviar</span>
          </Button>
        </div>
        <div className="mt-2 flex items-center justify-between text-xs text-[#9BA1AA]">
          <span className="flex items-center gap-1.5"><Sparkles className="h-3 w-3 text-[#4C9DFF]" /> Bucle Agente TDAO · Guardia de Alcance · Compactación 4 capas</span>
          <span className="flex items-center gap-2">
            <span className="hidden sm:inline">⌘K cambio rápido · /herramientas · Ctrl+V adjuntos</span>
            <button onClick={distillNow} title="Destilar esta sesión al grafo de conocimiento" aria-label="Destilar sesión al grafo" className="flex items-center gap-1.5 rounded-lg border border-[#2C3037] bg-[#22262D] px-2 py-1 text-xs text-[#9BA1AA] hover:text-[#EDEEF0]"><Database className="h-3 w-3" /> Destilar</button>
          </span>
        </div>
        {distillMsg && <div className="mt-2 text-xs text-[#4C9DFF]">{distillMsg}</div>}
        {attachToast && <div className="mt-2 text-xs text-[#FFB86B]">{attachToast}</div>}
      </div>
    </div>
  )
}
