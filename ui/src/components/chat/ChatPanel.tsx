import { useState, useRef, useEffect, useCallback } from 'react'
import { Send, Paperclip, Sparkles, Wrench, User, Bot, Copy, Check, ListTodo, Eye, X, Camera, FileText, Database, ChevronRight, Volume2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { chatApi, chatStreamApi, steerSessionApi, sessionModeApi, apiFetch, uploadApi, voiceTtsApi, type UploadResult } from '@/lib/api'
import { VoiceMic } from '@/components/chat/VoiceMic'

type Pending = { key: string; file: File; preview?: string }

const now = () => new Date().toLocaleTimeString().slice(0, 5)
const uid = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`)

// Resumen de una línea para el bloque colapsable de una tool (estilo opencode).
const summarize = (s: string) => {
  const first = (s || '').split('\n').map(x => x.trim()).filter(Boolean)[0] || '(sin salida)'
  return first.length > 90 ? first.slice(0, 88) + '…' : first
}

type SlashCommand = { name: string; description?: string; args?: string }

const SLASH_FALLBACK: SlashCommand[] = [
  { name: 'help', description: 'muestra los comandos disponibles' },
  { name: 'clear', description: 'limpia el chat en pantalla' },
  { name: 'model', args: '<id>', description: 'elige el modelo' },
  { name: 'persona', args: '[preset]', description: 'cambia el preset de personalidad' },
  { name: 'skill', args: '<name>', description: 'invoca una skill por nombre' },
  { name: 'session', args: '[new|list]', description: 'operaciones de sesión' },
  { name: 'plan', args: '[on|off]', description: 'activa/desactiva modo plan' },
  { name: 'permission', args: '<read_only|workspace_write|full>', description: 'cambia permiso' },
  { name: 'compact', description: 'info compactación 4 capas' },
  { name: 'goal', args: '<objetivo|status|clear>', description: 'gestiona objetivo largo' },
  { name: 'export', description: 'exporta log de sesión ZIP' },
  { name: 'feedback', args: '[texto]', description: 'envía feedback' },
  { name: 'todo', description: 'ver lista todo actual' },
  { name: 'agent', args: '<name>', description: 'cambia de ayudante' },
  { name: 'alcance', description: 'info de alcance/permisos' },
  { name: 'herramientas', description: 'lista las herramientas disponibles' },
  { name: 'doctor', description: 'abre el diagnóstico del sistema' },
]

export function ChatPanel() {
  const { messages, messagesError, addMessage, activeSession, loadMessages, liveStatus, loadStatus } = useAppStore()
  const [input, setInput] = useState('')
  const [copied, setCopied] = useState<string | null>(null)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const toggleExpanded = useCallback((id: string) => setExpanded(prev => {
    const n = new Set(prev); if (n.has(id)) n.delete(id); else n.add(id); return n
  }), [])
  const [attachToast, setAttachToast] = useState<string | null>(null)
  const [pending, setPending] = useState<Pending[]>([])
  const bottomRef = useRef<HTMLDivElement>(null)
  const fileInputRef = useRef<HTMLInputElement>(null)
  const textareaRef = useRef<HTMLTextAreaElement>(null)
  const [isStreaming, setIsStreaming] = useState(false)
  const [reasoning, setReasoning] = useState('')
  const [showReasoning, setShowReasoning] = useState(false)
  const busyRef = useRef(false)  // guard anti doble-send (el state no es sincrónico)
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashQuery, setSlashQuery] = useState('')
  const [lastSteps, setLastSteps] = useState<any[]|null>(null)
  const [lastTodos, setLastTodos] = useState<any|null>(null)
  const [showTraj, setShowTraj] = useState(false)
  const [distillMsg, setDistillMsg] = useState<string | null>(null)
  const [distilling, setDistilling] = useState(false)
  const [speaking, setSpeaking] = useState(false)
  const [slashCommands, setSlashCommands] = useState<SlashCommand[]>(SLASH_FALLBACK)

  // Los comandos los define el server (/api/slash); la lista local es solo fallback.
  useEffect(() => {
    apiFetch('/api/slash')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`slash ${r.status}`))))
      .then((d) => { if (Array.isArray(d?.commands) && d.commands.length) setSlashCommands(d.commands) })
      .catch(() => { /* backend caído: seguimos con el fallback */ })
  }, [])

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
    // Si hay un run en curso, el mensaje se inyecta como STEER (el loop lo lee
    // entre iteraciones). Antes el input quedaba bloqueado y /steer era inalcanzable.
    if (isStreaming && !busyRef.current && input.trim()) {
      const text = input.trim()
      const sidNow = useAppStore.getState().activeSession
      if (!sidNow) { toast('No hay sesión activa para dirigir el run'); return }
      setInput(''); setSlashOpen(false)
      try {
        const r = await steerSessionApi(sidNow, text)
        addMessage({ id: uid(), role: 'user', content: text, timestamp: now() })
        addMessage({
          id: uid(),
          role: 'tool',
          content: r.queued
            ? 'steer encolado: el run lo aplicará en la próxima iteración'
            : 'steer no aplicado: no hay run en curso para esta sesión',
          toolName: 'steer',
          timestamp: now(),
        })
      } catch (e) {
        toast('No se pudo dirigir el run: ' + (e as Error).message.slice(0, 140))
      }
      return
    }
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
        if (j.data?.action === 'set_agent' && j.data.agent) useAppStore.getState().setActiveAgent(j.data.agent)
        if (j.data?.action === 'session_new') useAppStore.getState().createSession()
        if (j.data?.action === 'export_session') {
          if (!activeSession) {
            if (stillActive()) addMessage({ id: uid(), role: 'tool', content: 'export: no hay sesión activa', toolName: 'export', timestamp: now() })
          } else {
            try {
              const er = await apiFetch(`/api/sessions/${encodeURIComponent(activeSession)}/export`)
              if (!er.ok) throw new Error(`HTTP ${er.status}`)
              const url = URL.createObjectURL(await er.blob())
              const a = document.createElement('a')
              a.href = url; a.download = `${activeSession}.zip`; a.click()
              window.setTimeout(() => URL.revokeObjectURL(url), 4000)
              if (stillActive()) addMessage({ id: uid(), role: 'tool', content: `export: descargado ${activeSession}.zip`, toolName: 'export', timestamp: now() })
            } catch (e) {
              if (stillActive()) addMessage({ id: uid(), role: 'tool', content: `export falló: ${(e as Error).message.slice(0, 140)}`, toolName: 'export', timestamp: now() })
            }
          }
        }
        if (j.data?.action === 'todo_list') {
          if (!activeSession) {
            if (stillActive()) addMessage({ id: uid(), role: 'tool', content: 'todos: no hay sesión activa', toolName: 'todo', timestamp: now() })
          } else {
            try {
              const tr = await apiFetch(`/api/todos?session_id=${encodeURIComponent(activeSession)}`)
              if (!tr.ok) throw new Error(`HTTP ${tr.status}`)
              const tj: any = await tr.json()
              const list: any[] = Array.isArray(tj) ? tj : (tj.todos || [])
              const txt = list.length
                ? list.map((t) => `· [${t.status || 'pending'}] ${t.content || ''}`).join('\n')
                : '(sin tareas en esta sesión)'
              if (stillActive()) addMessage({ id: uid(), role: 'tool', content: `todos (${list.length}):\n${txt}`, toolName: 'todo', timestamp: now() })
            } catch (e) {
              if (stillActive()) addMessage({ id: uid(), role: 'tool', content: `todos falló: ${(e as Error).message.slice(0, 140)}`, toolName: 'todo', timestamp: now() })
            }
          }
        }
        if (j.data?.action === 'set_permission' || j.data?.action === 'set_plan_mode') {
          try {
            const pr = await apiFetch('/api/permission')
            const pj = await pr.json()
            if (stillActive()) addMessage({ id: uid(), role: 'tool', content: 'permiso ahora: ' + pj.permission, toolName: 'permission', timestamp: now() })
          } catch (e) {
            if (stillActive()) addMessage({ id: uid(), role: 'tool', content: `permiso: no se pudo releer (/api/permission): ${(e as Error).message.slice(0, 140)}`, toolName: 'permission', timestamp: now() })
          }
        }
      } catch (e) { if (stillActive()) addMessage({ id: uid(), role: 'assistant', content: 'slash error: ' + (e as Error).message, timestamp: now() }) }
      busyRef.current = false
      return
    }

    const state = useAppStore.getState()
    let activeSession = state.activeSession
    const { selectedModel, activeAgent } = state
    if (!activeSession) activeSession = await useAppStore.getState().ensureSession()
    const stillActive = () => useAppStore.getState().activeSession === activeSession
    if (!uploads.length) addMessage({ id: uid(), role: 'user', content: text, timestamp: now() })
    setInput(''); setSlashOpen(false)
    setReasoning('')
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
          const sres = await chatStreamApi(text, { sessionId: activeSession || undefined, model, agentId: (activeAgent && activeAgent !== 'default') ? activeAgent : undefined,
            onDelta: pushDelta,
            onReasoning: (t) => { if (stillActive()) setReasoning(prev => prev + t) } })
          const finalReply = (sres.success === false ? '⚠️ ' : '') + (sres.reply || acc || '[sin respuesta]')
          if (stillActive()) {
            if (placed) updateMessage(msgId, finalReply)
            else addMessage({ id: msgId, role: 'assistant', content: finalReply, timestamp: now() })
          }
          streamed = true
        } catch (e) {
          streamed = false
          if (placed && stillActive()) {
            acc += `\n\n⚠️ Respuesta interrumpida: ${(e as Error).message.slice(0, 160)}. Reintentá.`
            updateMessage(msgId, acc)
          }
        }
      }
      if (!streamed && !placed) {
        const res: any = await chatApi(text, activeSession || undefined, undefined, model,
          uploads.map(({ id, url, name, mime, is_image }) => ({ id, url, name, mime, is_image })),
          (activeAgent && activeAgent !== 'default') ? activeAgent : undefined)
        if (res.steps?.length && stillActive()) setLastSteps(res.steps)
        if (res.todos && stillActive()) setLastTodos(res.todos)
        // Fallback honesto: el server avisa cuando no hubo respuesta real del modelo.
        const failed = res.success === false
        const echo = res.mode === 'echo_fallback'
        const body = res.reply || '[sin respuesta]'
        const head = (failed || echo)
          ? `⚠️ ${echo ? 'Respuesta de emergencia (echo_fallback): el modelo no respondió.' : 'El modelo no produjo una respuesta válida.'}${res.error ? ` Detalle: ${String(res.error).slice(0, 200)}` : ''}\n\n`
          : ''
        // si el usuario cambió de sesión mientras respondía, el historial de la sesión
        // destino se recarga con loadMessages — no mezclar la respuesta aquí
        if (stillActive()) {
          // si ya se mostraron deltas, ACTUALIZAR (no duplicar con el mismo id)
          if (placed) updateMessage(msgId, head + body)
          else addMessage({ id: msgId, role: 'assistant', content: head + body, timestamp: now() })
        }
      }
      void useAppStore.getState().loadSessions()
    } catch (e) {
      const msg = (e as Error).message || String(e)
      const reply = msg.includes("timeout") || msg.includes("AbortError")
        ? `⚠️ Timeout del modelo. Reintenta con un mensaje más corto o cambia de modelo. Detalle: ${msg.slice(0, 200)}`
        : `⚠️ Backend no disponible (${msg.slice(0, 120)}). Inicia el servidor con \`python -m src.api.server\` (o vía Electron) y reintenta.`
      await new Promise(r => setTimeout(r, 500))
      if (stillActive()) addMessage({ id: uid(), role: 'assistant', content: reply, timestamp: now() })
    } finally {
      busyRef.current = false
      setIsStreaming(false)
    }
  }

  // Modo plan/act: el endpoint existía y statusApi.modes llegaba, pero no había control.
  const sessionMode = ((liveStatus?.modes ?? {})[activeSession] as 'act' | 'plan' | undefined) ?? 'act'
  const [modeBusy, setModeBusy] = useState(false)
  const toggleSessionMode = useCallback(async () => {
    if (!activeSession || modeBusy) return
    const next = sessionMode === 'plan' ? 'act' : 'plan'
    setModeBusy(true)
    try {
      const r = await sessionModeApi(activeSession, next)
      await loadStatus()
      addMessage({
        id: uid(), role: 'tool', toolName: 'plan_mode', timestamp: now(),
        content: r.mode === 'plan'
          ? 'modo plan: solo lectura — las tools mutantes quedan bloqueadas hasta volver a act'
          : 'modo act: escritura y ejecución permitidas',
      })
    } catch (e) {
      addMessage({ id: uid(), role: 'tool', toolName: 'plan_mode', timestamp: now(),
        content: `no se pudo cambiar el modo: ${(e as Error).message.slice(0, 160)}` })
    } finally {
      setModeBusy(false)
    }
  }, [activeSession, sessionMode, modeBusy, loadStatus, addMessage])

  const distillNow = useCallback(async () => {
    const { activeSession } = useAppStore.getState()
    if (!activeSession) { setDistillMsg('sin sesión activa'); setTimeout(() => setDistillMsg(null), 2500); return }
    if (distilling) return
    setDistilling(true)
    setDistillMsg('destilando…')
    try {
      const r = await apiFetch(`/api/sessions/${activeSession}/distill`, { method: 'POST' })
      const j: any = await r.json().catch(() => ({}))
      if (!r.ok || j?.ok === false) throw new Error(j?.error || j?.detail || `HTTP ${r.status}`)
      const res = j?.result ?? j ?? {}
      setDistillMsg(`destilado: ${res.distilled ?? 0} piezas · ${res.llm_items ?? 0} items LLM`)
    } catch (e: any) {
      setDistillMsg(`error: ${e?.message || 'falló'}`)
    } finally {
      setDistilling(false)
    }
    setTimeout(() => setDistillMsg(null), 4000)
  }, [distilling])

  const speakLast = useCallback(async () => {
    const { messages: msgs } = useAppStore.getState()
    const last = [...msgs].reverse().find((m) => m.role === 'assistant')
    if (!last) { setAttachToast('no hay respuesta para leer'); setTimeout(() => setAttachToast(null), 2500); return }
    if (speaking) return
    setSpeaking(true); setAttachToast(null)
    try {
      const blob = await voiceTtsApi(last.content)
      const url = URL.createObjectURL(blob)
      const audio = new Audio(url)
      const done = () => { URL.revokeObjectURL(url); setSpeaking(false) }
      audio.onended = done
      audio.onerror = () => { done(); setAttachToast('no se pudo reproducir el audio') }
      await audio.play()
    } catch (e) {
      setSpeaking(false)
      setAttachToast('TTS: ' + (e as Error).message.slice(0, 120))
      setTimeout(() => setAttachToast(null), 4000)
    }
  }, [speaking])

  return (
    <div className="flex h-full flex-col">
      {/* messages */}
      <div className="flex-1 overflow-auto px-6 py-6 space-y-5">
        {messagesError && (
          <div className="flex items-center gap-2 rounded-lg border border-[#5A2A2A] bg-[#3A1F1F]/60 px-3 py-2 text-xs text-[#FFA8A8]">
            <span className="flex-1">{messagesError}</span>
            <button
              onClick={() => { void loadMessages(activeSession) }}
              className="rounded border border-[#5A2A2A] px-2 py-0.5 text-[11px] text-[#FFC9C9] hover:bg-[#4A2525]"
            >
              Reintentar
            </button>
          </div>
        )}
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
          m.role === 'tool' ? (
            <div key={m.id} className="max-w-[85%] overflow-hidden rounded-xl border border-[#2C3037] bg-[#1E2127]">
              <button onClick={() => toggleExpanded(m.id)} aria-expanded={expanded.has(m.id)}
                className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-[#22262D]">
                <Wrench className="h-3.5 w-3.5 shrink-0 text-[#FFB86B]" />
                <span className="shrink-0 font-mono text-[#FFB86B]">{m.toolName || 'tool'}</span>
                <span className="min-w-0 flex-1 truncate text-[#9BA1AA]">{summarize(m.content)}</span>
                <span className="shrink-0 text-[#59616D]">{m.timestamp}</span>
                <ChevronRight className={cn('h-3.5 w-3.5 shrink-0 text-[#9BA1AA] transition-transform', expanded.has(m.id) && 'rotate-90')} />
              </button>
              {expanded.has(m.id) && (
                <div className="max-h-80 overflow-auto border-t border-[#2C3037] px-3 py-2 text-xs leading-relaxed text-[#C9D2E0] whitespace-pre-wrap break-words">
                  {m.content}
                  <div className="mt-1.5">
                    <button onClick={() => { void navigator.clipboard.writeText(m.content).catch(()=>{}); setCopied(m.id); setTimeout(()=>setCopied(null), 1200) }}
                      className="text-[10px] text-[#8CC1FF] hover:underline">
                      {copied===m.id ? 'copiado' : 'copiar salida'}
                    </button>
                  </div>
                </div>
              )}
            </div>
          ) : (
          <div key={m.id} className={cn('flex gap-3', m.role === 'user' ? 'justify-end' : 'justify-start')}>
            {m.role !== 'user' && (
              <div className="mt-0.5 flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-[#0B62D6] text-white">
                <Bot className="h-3.5 w-3.5" />
              </div>
            )}
            <div className={cn('group relative max-w-[72%] rounded-2xl px-4 py-3 text-sm leading-relaxed', m.role === 'user' ? 'bg-[#0B62D6] text-white rounded-br-sm' : 'bg-[#1E2127] border border-[#2C3037] text-[#EDEEF0]')}>
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
          )
        ))}
        {(lastTodos && lastTodos.todos?.length > 0) && (
          <div className="rounded-xl border border-[#4C9DFF]/20 bg-[#4C9DFF]/10 p-3 text-xs">
            <div className="flex items-center gap-2 font-semibold text-[#8CC1FF]"><ListTodo className="h-3.5 w-3.5" /> Todos: {lastTodos.counts?.completed ?? 0}/{lastTodos.todos.length} completados</div>
            <div className="mt-2 space-y-1">{lastTodos.todos.map((td:any,i:number)=>(<div key={i} className="flex gap-2"><span className={td.status==='completed'?'text-[#3DDC84]':td.status==='in_progress'?'text-[#FFB86B]':'text-[#9BA1AA]'}>{td.status==='completed'?'●':td.status==='in_progress'?'◐':'○'}</span><span className={td.status==='completed'?'line-through text-[#9BA1AA]':''}>{td.content}</span><span className="ml-auto text-[#9BA1AA]">{td.status==='completed'?'hecho':td.status==='in_progress'?'en curso':'pendiente'}</span></div>))}</div>
          </div>
        )}
        {(lastSteps && lastSteps.length>0) && (
          <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-xs">
            <button onClick={()=>setShowTraj(v=>!v)} className="flex items-center gap-2 font-semibold text-[#EDEEF0]"><Eye className="h-3.5 w-3.5" /> Trayectoria: {lastSteps.length} pasos {showTraj? '▾':'▸'}</button>
            {showTraj && <div className="mt-2 space-y-2 max-h-64 overflow-auto">{lastSteps.map((s:any,i:number)=>(<div key={i} className="rounded-lg border border-[#2C3037] bg-[#15171B] p-2"><div className="flex gap-2 text-[#9BA1AA]">#{s.iteration} <span className="text-[#4C9DFF]">{s.phase}</span> {s.tool_used && <span className="text-[#FFB86B]">{s.tool_used}</span>}</div><div className="mt-1 whitespace-pre-wrap text-[#EDEEF0]">{String(s.content||'').slice(0,600)}</div></div>))}</div>}
          </div>
        )}
        {reasoning && (
          <div className="max-w-[85%] overflow-hidden rounded-xl border border-[#2C3037] bg-[#1E2127]">
            <button onClick={() => setShowReasoning(v => !v)} aria-expanded={showReasoning}
              className="flex w-full items-center gap-2 px-3 py-1.5 text-left text-xs hover:bg-[#22262D]">
              <Sparkles className="h-3.5 w-3.5 shrink-0 text-[#C5A3FF]" />
              <span className="shrink-0 text-[#C5A3FF]">Pensando…</span>
              <span className="min-w-0 flex-1 truncate text-[#9BA1AA]">{summarize(reasoning)}</span>
              <ChevronRight className={cn('h-3.5 w-3.5 shrink-0 text-[#9BA1AA] transition-transform', showReasoning && 'rotate-90')} />
            </button>
            {showReasoning && (
              <div className="max-h-60 overflow-auto border-t border-[#2C3037] px-3 py-2 text-xs leading-relaxed text-[#C9D2E0] whitespace-pre-wrap break-words">{reasoning}</div>
            )}
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
          {slashCommands.filter(c => !slashQuery || `${c.name} ${c.args ?? ''} ${c.description ?? ''}`.toLowerCase().includes(slashQuery.toLowerCase())).map(c => (
            <button key={c.name} onClick={()=>{ setInput('/'+c.name+(c.args ? ' ' : '')); setSlashOpen(false); textareaRef.current?.focus() }} className="flex w-full gap-2 rounded-lg px-2 py-1.5 text-left hover:bg-[#2C3037]"><span className="shrink-0 text-[#4C9DFF]">/{c.name}{c.args ? ` ${c.args}` : ''}</span><span className="text-[#9BA1AA]">{c.description || ''}</span></button>
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
            placeholder={isStreaming ? "Dirigir el run en curso… (Enter encola la instrucción)" : "Mensaje a Nexus…  / comandos · Ctrl+V adjunta · Shift+Enter salto"}
            rows={1}
            aria-label="Mensaje a Nexus"
            className="max-h-32 min-h-[40px] flex-1 resize-none bg-transparent px-2 py-2.5 text-sm placeholder:text-[#9BA1AA] focus:outline-none text-[#EDEEF0]"
          />
          <div className="relative flex shrink-0 items-center">
            <VoiceMic onTranscript={onTranscript} />
          </div>
          <Button onClick={send} disabled={!input.trim() && pending.length===0} aria-label={isStreaming ? "Dirigir el run en curso" : "Enviar mensaje"} className="shrink-0 rounded-xl bg-[#0B62D6] hover:bg-[#4C9DFF] text-white">
            <Send className="h-4 w-4" /> <span className="hidden sm:inline">{isStreaming ? 'Dirigir' : 'Enviar'}</span>
          </Button>
        </div>
        <div className="mt-2 flex items-center justify-between text-xs text-[#9BA1AA]">
          <span className="flex items-center gap-1.5"><Sparkles className="h-3 w-3 text-[#4C9DFF]" /> Bucle Agente TDAO · Guardia de Alcance · Compactación 4 capas</span>
          <span className="flex items-center gap-2">
            <span className="hidden sm:inline">⌘K cambio rápido · /herramientas · Ctrl+V adjuntos</span>
            <button onClick={toggleSessionMode} disabled={modeBusy || !activeSession} title={sessionMode === 'plan' ? 'Modo plan activo: solo lectura. Clic para volver a act' : 'Activar modo plan (solo lectura)'} aria-pressed={sessionMode === 'plan'} className={`flex items-center gap-1.5 rounded-lg border px-2 py-1 text-xs disabled:opacity-50 ${sessionMode === 'plan' ? 'border-[#FFB86B]/50 bg-[#FFB86B]/10 text-[#FFB86B]' : 'border-[#2C3037] bg-[#22262D] text-[#9BA1AA] hover:text-[#EDEEF0]'}`}><Eye className="h-3 w-3" /> {modeBusy ? '…' : sessionMode === 'plan' ? 'Plan' : 'Act'}</button>
            <button onClick={speakLast} disabled={speaking} title="Leer la última respuesta en voz alta" aria-label="Leer última respuesta" className="flex items-center gap-1.5 rounded-lg border border-[#2C3037] bg-[#22262D] px-2 py-1 text-xs text-[#9BA1AA] hover:text-[#EDEEF0] disabled:opacity-50"><Volume2 className="h-3 w-3" /> {speaking ? 'Leyendo…' : 'Escuchar'}</button>
            <button onClick={distillNow} disabled={distilling} title="Destilar esta sesión al grafo de conocimiento" aria-label="Destilar sesión al grafo" className="flex items-center gap-1.5 rounded-lg border border-[#2C3037] bg-[#22262D] px-2 py-1 text-xs text-[#9BA1AA] hover:text-[#EDEEF0] disabled:opacity-50"><Database className="h-3 w-3" /> {distilling ? 'Destilando…' : 'Destilar'}</button>
          </span>
        </div>
        {distillMsg && <div className="mt-2 text-xs text-[#4C9DFF]">{distillMsg}</div>}
        {attachToast && <div className="mt-2 text-xs text-[#FFB86B]">{attachToast}</div>}
      </div>
    </div>
  )
}
