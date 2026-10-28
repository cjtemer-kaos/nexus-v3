import { useState, useRef, useEffect, useCallback, useMemo } from 'react'
import {
  Send, Paperclip, Sparkles, Wrench, User, Bot, Copy, Check, ListTodo, Eye, X, Camera,
  FileText, Database, ChevronRight, Volume2, MoreHorizontal, ArrowUp, GitBranch, CheckCircle2,
  Undo2, Square, Download, ClipboardCopy, ArrowDown,
} from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAppStore, type ChatMessage } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { chatApi, chatStreamApi, steerSessionApi, sessionModeApi, apiFetch, uploadApi, voiceTtsApi, usageApi, rewindSessionApi, sessionExportApi, type UploadResult, type Usage } from '@/lib/api'
import { VoiceMic } from '@/components/chat/VoiceMic'
import { ApplyPatchResult } from '@/components/chat/ApplyPatchResult'
import { Markdown } from '@/components/chat/Markdown'
import { InlineAsks } from '@/components/chat/InlineAsks'
import { ToolOutput } from '@/components/chat/ToolOutput'
import { AgentSelector } from '@/components/AgentSelector'
import { ModelSelector } from '@/components/ModelSelector'
import { ContextMeter } from '@/components/chat/ContextMeter'

type Pending = { key: string; file: File; preview?: string }
type Grupo =
  | { tipo: 'msg'; m: ChatMessage }
  | { tipo: 'tools'; nombre: string; items: ChatMessage[] }

// Las tools consecutivas se agrupan en una línea ("Se usaron 9 Shell").
const ETIQUETA_TOOL: Record<string, string> = {
  plan_mode: 'cambio de modo',
  steer: 'indicación',
  question: 'pregunta',
  permission: 'permiso',
}
const etiquetaTool = (n: string) => ETIQUETA_TOOL[n] || n

const agrupar = (msgs: ChatMessage[]): Grupo[] => {
  const out: Grupo[] = []
  for (const m of msgs) {
    const ultimo = out[out.length - 1]
    if (m.role === 'tool') {
      if (ultimo && ultimo.tipo === 'tools' && ultimo.nombre === (m.toolName || 'tool')) {
        ultimo.items.push(m)
      } else {
        out.push({ tipo: 'tools', nombre: m.toolName || 'tool', items: [m] })
      }
    } else {
      out.push({ tipo: 'msg', m })
    }
  }
  return out
}

const now = () => new Date().toLocaleTimeString().slice(0, 5)
const uid = () => (globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2, 8)}`)

// El modelo a veces escribe el bloque de tool_call como texto: no debe verse en el chat.
const sanitizeAssistantText = (s: string) => {
  let out = s || ''
  out = out.replace(/<tool_call>[\s\S]*?<\/tool_call>/g, '').replace(/<tool_call>[\s\S]*$/g, '')
  out = out.replace(/<\/?tool_call>/g, '')
  return out.replace(/\n{3,}/g, '\n\n').trim()
}

// Resumen de una línea para el bloque colapsable de una tool.
const summarize = (s: string) => {
  const first = (s || '').split('\n').map(x => x.trim()).filter(Boolean)[0] || '(sin salida)'
  return first.length > 90 ? first.slice(0, 88) + '…' : first
}

const summarizeTool = (name: string | undefined, content: string) => {
  if (name === 'apply_patch') {
    try {
      const j = JSON.parse(content)
      const n = Array.isArray(j.files) ? j.files.length : 0
      const fuzz = Number(j.fuzz || 0)
      return `${j.dry_run ? 'dry-run' : 'aplicado'} · ${n} archivo${n === 1 ? '' : 's'}${fuzz > 0 ? ` · fuzz ${fuzz}` : ''}`
    } catch {
      return summarize(content)
    }
  }
  return summarize(content)
}

// El prefijo lo agrega el backend; en la tarjeta el titulo ya lo dice.
const summaryBody = (s: string) => (s || '').replace(/^\[(?:resumen de [^\]]*|handoff)\]\s*/i, '').trim()

type SlashCommand = { name: string; description?: string; args?: string }

const SLASH_FALLBACK: SlashCommand[] = [
  { name: 'help', description: 'muestra los comandos disponibles' },
  { name: 'clear', description: 'limpia el chat en pantalla' },
  { name: 'model', args: '<id>', description: 'elige el modelo' },
  { name: 'persona', args: '[preset]', description: 'cambia el preset de personalidad' },
  { name: 'skill', args: '<name>', description: 'invoca una skill por nombre' },
  { name: 'session', args: '[new|list]', description: 'operaciones de sesión' },
  { name: 'plan', args: '[on|off]', description: 'activa/desactiva modo plan' },
  { name: 'permission', args: '<#0..#4 | read_only|workspace_write|full>', description: 'peldaño de permisos de la sesión' },
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

type ChatPanelProps = { sessionId?: string; paneId?: 0 | 1 }

export function ChatPanel({ sessionId, paneId = 0 }: ChatPanelProps = {}) {
  const {
    messagesError, liveStatus, loadStatus, sessions, forkSession,
    messagesBySession, paneSessions, activeSession: activaGlobal, split,
    messages: mensajesActivos,
    addMessage: addMessageStore, updateMessage: updateMessageStore,
    setMessages: setMessagesStore, loadMessages: loadMessagesStore,
  } = useAppStore()
  // Con dos paneles abiertos cada uno muestra SU sesión; `activeSession` global
  // queda para el panel que tiene el foco (atajos, envíos externos).
  const activeSession = sessionId || paneSessions[paneId] || activaGlobal
  const messages = messagesBySession[activeSession]
    ?? (activeSession === activaGlobal ? mensajesActivos : [])

  // La sesión se resuelve al momento de escribir, no al renderizar: un panel
  // sin sesión puede crearla recién al enviar el primer mensaje.
  const resolveSid = useCallback(
    () => sessionId || useAppStore.getState().paneSessions[paneId] || useAppStore.getState().activeSession,
    [sessionId, paneId],
  )
  const addMessage = useCallback((m: ChatMessage) => addMessageStore(m, resolveSid() || undefined), [addMessageStore, resolveSid])
  const updateMessage = useCallback((id: string, content: string) => updateMessageStore(id, content, resolveSid() || undefined), [updateMessageStore, resolveSid])
  const setMessages = useCallback((m: ChatMessage[]) => setMessagesStore(m, resolveSid() || undefined), [setMessagesStore, resolveSid])
  const loadMessages = useCallback((sid: string) => loadMessagesStore(sid), [loadMessagesStore])
  const [usage, setUsage] = useState<Usage | null>(null)
  const [menuOpen, setMenuOpen] = useState(false)
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
  const abortRef = useRef<AbortController | null>(null)
  const [slashOpen, setSlashOpen] = useState(false)
  const [slashQuery, setSlashQuery] = useState('')
  const [lastSteps, setLastSteps] = useState<any[]|null>(null)
  const [lastTodos, setLastTodos] = useState<any|null>(null)
  const [showTraj, setShowTraj] = useState(false)
  const [distillMsg, setDistillMsg] = useState<string | null>(null)
  const [distilling, setDistilling] = useState(false)
  const [speaking, setSpeaking] = useState(false)
  const [exporting, setExporting] = useState(false)
  const [copiadoTodo, setCopiadoTodo] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const dragDepth = useRef(0)
  // Si el usuario subió a leer, el stream no puede arrastrarlo al final.
  const cercaDelFinal = useRef(true)
  const [hayQueBajar, setHayQueBajar] = useState(false)
  const scrollRef = useRef<HTMLDivElement>(null)
  const onScrollMensajes = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    const resto = el.scrollHeight - el.scrollTop - el.clientHeight
    const abajo = resto < 120
    cercaDelFinal.current = abajo
    // El botón aparece exactamente cuando el auto-scroll se apagó: un solo umbral
    // evita el caso raro de "no me sigue pero tampoco hay botón".
    setHayQueBajar(!abajo)
  }, [])
  const irAlFinal = useCallback(() => {
    cercaDelFinal.current = true
    setHayQueBajar(false)
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [])
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

  // Tokens/costo de la sesión en el header.
  useEffect(() => {
    if (!activeSession) { setUsage(null); return }
    let vivo = true
    usageApi(activeSession)
      .then((u) => { if (vivo) setUsage(u) })
      .catch(() => { if (vivo) setUsage(null) })
    return () => { vivo = false }
  }, [activeSession, messages.length, isStreaming])

  useEffect(() => { if (cercaDelFinal.current) bottomRef.current?.scrollIntoView({ behavior: 'smooth' }) }, [messages, isStreaming, lastSteps, lastTodos])

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

  const send = async (override?: string) => {
    const draft = override ?? input
    // Si hay un run en curso, el mensaje se inyecta como STEER (el loop lo lee
    // entre iteraciones). Antes el input quedaba bloqueado y /steer era inalcanzable.
    if (isStreaming && !busyRef.current && draft.trim()) {
      const text = draft.trim()
      const sidNow = resolveSid()
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
    if ((!draft.trim() && pending.length === 0) || isStreaming || busyRef.current) return
    busyRef.current = true
    const text = draft.trim()

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
      const sid = activeSession
      const stillActive = () => resolveSid() === sid
      if (!uploads.length) addMessage({ id: uid(), role: 'user', content: text, timestamp: now() })
      setInput(''); setSlashOpen(false)
      try {
        const r = await apiFetch('/api/slash', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: text, ctx: { session_id: sid } }) })
        const j = await r.json()
        const reply = j.ok ? j.message : (text.slice(1).split(' ')[0] + ': ' + j.message)
        const sendsPrompt = j.data?.action === 'send_prompt'
        if (stillActive() && !sendsPrompt) addMessage({ id: uid(), role: 'assistant', content: reply, timestamp: now() })
        if (j.data?.action === 'clear_local_only') setMessages([])
        if (j.data?.action === 'set_model') useAppStore.getState().setModel(j.data.model)
        if (j.data?.action === 'set_agent' && j.data.agent) useAppStore.getState().setActiveAgent(j.data.agent)
        if (j.data?.action === 'session_new') useAppStore.getState().createSession()
        if (j.data?.action === 'export_session') {
          if (!sid) {
            if (stillActive()) addMessage({ id: uid(), role: 'tool', content: 'export: no hay sesión activa', toolName: 'export', timestamp: now() })
          } else {
            try {
              const er = await apiFetch(`/api/sessions/${encodeURIComponent(sid)}/export`)
              if (!er.ok) throw new Error(`HTTP ${er.status}`)
              const url = URL.createObjectURL(await er.blob())
              const a = document.createElement('a')
              a.href = url; a.download = `${sid}.zip`; a.click()
              window.setTimeout(() => URL.revokeObjectURL(url), 4000)
              if (stillActive()) addMessage({ id: uid(), role: 'tool', content: `export: descargado ${sid}.zip`, toolName: 'export', timestamp: now() })
            } catch (e) {
              if (stillActive()) addMessage({ id: uid(), role: 'tool', content: `export falló: ${(e as Error).message.slice(0, 140)}`, toolName: 'export', timestamp: now() })
            }
          }
        }
        if (j.data?.action === 'todo_list') {
          if (!sid) {
            if (stillActive()) addMessage({ id: uid(), role: 'tool', content: 'todos: no hay sesión activa', toolName: 'todo', timestamp: now() })
          } else {
            try {
              const tr = await apiFetch(`/api/todos?session_id=${encodeURIComponent(sid)}`)
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
        // Comando personalizado: el servidor devuelve el prompt ya expandido y
        // acá se envía como turno normal (mismo jail, mismos permisos).
        if (sendsPrompt && typeof j.data?.prompt === 'string') {
          const warns: string[] = Array.isArray(j.data.warnings) ? j.data.warnings : []
          if (warns.length && stillActive()) {
            addMessage({ id: uid(), role: 'tool', content: `/${j.data.command ?? ''}: ${warns.join(' · ')}`, toolName: 'comando', timestamp: now() })
          }
          if (j.data.model) useAppStore.getState().setModel(String(j.data.model))
          if (j.data.agent) useAppStore.getState().setActiveAgent(String(j.data.agent))
          busyRef.current = false
          await send(String(j.data.prompt))
          return
        }
      } catch (e) { if (stillActive()) addMessage({ id: uid(), role: 'assistant', content: 'slash error: ' + (e as Error).message, timestamp: now() }) }
      busyRef.current = false
      return
    }

    const state = useAppStore.getState()
    const { selectedModel, activeAgent } = state
    let sid = activeSession
    if (!sid) {
      sid = await useAppStore.getState().ensureSession()
      useAppStore.getState().setPaneSession(paneId, sid)
    }
    const stillActive = () => resolveSid() === sid
    if (!uploads.length) addMessage({ id: uid(), role: 'user', content: text, timestamp: now() })
    setInput(''); setSlashOpen(false)
    setReasoning('')
    setIsStreaming(true)
    try {
      const model = selectedModel && selectedModel !== 'auto' ? selectedModel : undefined
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
          if (stillActive()) setLastSteps([])
          const ctrl = new AbortController()
          abortRef.current = ctrl
          const sres = await chatStreamApi(text, { sessionId: sid || undefined, model, agentId: (activeAgent && activeAgent !== 'default') ? activeAgent : undefined,
            signal: ctrl.signal,
            onDelta: pushDelta,
            onStep: (s) => { if (stillActive()) setLastSteps(prev => [...(prev || []), s as any]) },
            onReasoning: (t) => { if (stillActive()) setReasoning(prev => prev + t) } })
          abortRef.current = null
          const finalReply = (sres.success === false ? '⚠️ ' : '') + (sres.reply || acc || '[sin respuesta]')
          if (stillActive()) {
            if (sres.steps?.length) setLastSteps(sres.steps as any)
            if (placed) updateMessage(msgId, finalReply)
            else addMessage({ id: msgId, role: 'assistant', content: finalReply, timestamp: now() })
          }
          streamed = true
        } catch (e) {
          abortRef.current = null
          streamed = false
          const cortado = (e as Error).name === 'AbortError'
          if (cortado) {
            if (stillActive()) {
              if (placed) updateMessage(msgId, `${acc}\n\n■ Detenido por el usuario.`)
              else addMessage({ id: msgId, role: 'assistant', content: '■ Detenido por el usuario.', timestamp: now() })
            }
          } else if (placed && stillActive()) {
            acc += `\n\n⚠️ Respuesta interrumpida: ${(e as Error).message.slice(0, 160)}. Reintentá.`
            updateMessage(msgId, acc)
          }
          // Detenido a propósito: lo que llegó ya está en el hilo y no se reintenta
          // por POST (sería duplicar el turno que el usuario acaba de cortar).
          if (cortado) streamed = true
        }
      }
      if (!streamed && !placed) {
        const res: any = await chatApi(text, sid || undefined, undefined, model,
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
      if (streamed && stillActive() && sid) {
        // El server ya persistió el turno: releer trae las tools como mensajes
        // (agrupadas en una línea), igual que al abrir la sesión de nuevo.
        void useAppStore.getState().loadMessages(sid)
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

  const detener = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setIsStreaming(false)
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && isStreaming) { e.preventDefault(); detener() }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [isStreaming, detener])

  // Volver acá: borra en el server todo lo posterior  // al mensaje ANTERIOR al elegido y devuelve el texto del usuario al composer.
  const volverAca = useCallback(async (m: ChatMessage) => {
    if (!activeSession || isStreaming) return
    const ok = confirm('¿Volver a este mensaje?\n\nSe descarta la respuesta y todo lo que vino después en esta sesión, y el texto vuelve al cuadro de escritura para editarlo.')
    if (!ok) return
    const idx = messages.findIndex((x) => x.id === m.id)
    const prev = idx > 0 ? messages[idx - 1] : null
    const prevId = prev ? Number(prev.id.slice(activeSession.length + 1)) : 0
    try {
      await rewindSessionApi(activeSession, Number.isFinite(prevId) ? prevId : 0)
      await loadMessages(activeSession)
      setInput(m.content)
      requestAnimationFrame(() => textareaRef.current?.focus())
    } catch (e) {
      setAttachToast(`no se pudo volver: ${(e as Error).message.slice(0, 140)}`)
      setTimeout(() => setAttachToast(null), 5000)
    }
  }, [activeSession, isStreaming, messages, loadMessages])

  const distillNow = useCallback(async () => {
    const sid = resolveSid()
    if (!sid) { setDistillMsg('sin sesión activa'); setTimeout(() => setDistillMsg(null), 2500); return }
    if (distilling) return
    setDistilling(true)
    setDistillMsg('destilando…')
    try {
      const r = await apiFetch(`/api/sessions/${sid}/distill`, { method: 'POST' })
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
  }, [distilling, resolveSid])

  const speakLast = useCallback(async () => {
    const msgs = messages
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
  }, [speaking, messages])

  /* ── exportar / compartir ─────────────────────────────────── */

  const nombreArchivo = useCallback(() => {
    const s = useAppStore.getState().sessions.find((x) => x.id === resolveSid())
    const base = (s?.title || s?.id || 'sesion').replace(/[^\p{L}\p{N}\s._-]+/gu, '').trim().slice(0, 60)
    return base || 'sesion'
  }, [resolveSid])

  const descargarSesion = useCallback(async () => {
    if (!activeSession) return
    setExporting(true)
    try {
      const blob = await sessionExportApi(activeSession)
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = `${nombreArchivo()}.zip`
      document.body.appendChild(a)
      a.click()
      a.remove()
      setTimeout(() => URL.revokeObjectURL(url), 5000)
      toast('Sesión exportada (.zip)')
    } catch (e) {
      toast('exportar: ' + (e as Error).message.slice(0, 120))
    } finally {
      setExporting(false)
    }
  }, [activeSession, nombreArchivo, toast])

  const copiarTranscripcion = useCallback(async () => {
    const msgs = messages
    const utiles = msgs.filter((m) => m.role !== 'tool' && (m.content || '').trim())
    if (!utiles.length) { toast('la sesión no tiene texto para copiar'); return }
    const cuerpo = utiles
      .map((m) => `**${m.role === 'user' ? 'Usuario' : 'Nexus'}**\n\n${m.content.trim()}`)
      .join('\n\n---\n\n')
    try {
      await navigator.clipboard.writeText(`# ${nombreArchivo()}\n\n${cuerpo}\n`)
      setCopiadoTodo(true)
      setTimeout(() => setCopiadoTodo(false), 2000)
    } catch (e) {
      toast('no se pudo copiar: ' + (e as Error).message.slice(0, 100))
    }
  }, [nombreArchivo, toast, messages])

  /* ── arrastrar archivos al chat ───────────────────────────── */

  const onDragEnter = useCallback((e: React.DragEvent) => {
    if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return
    e.preventDefault()
    dragDepth.current += 1
    setDragOver(true)
  }, [])

  const onDragLeave = useCallback((e: React.DragEvent) => {
    if (!Array.from(e.dataTransfer?.types || []).includes('Files')) return
    dragDepth.current = Math.max(0, dragDepth.current - 1)
    if (dragDepth.current === 0) setDragOver(false)
  }, [])

  const onDrop = useCallback((e: React.DragEvent) => {
    if (!e.dataTransfer?.files?.length) return
    e.preventDefault()
    dragDepth.current = 0
    setDragOver(false)
    addFiles(e.dataTransfer.files)
  }, [addFiles])

  const grupos = useMemo(() => agrupar(messages), [messages])
  const sesion = sessions.find((s) => s.id === activeSession)
  const titulo = sesion?.title || 'Sesión nueva'
  const proyecto = sesion?.project || ''

  return (
    <div
      className="relative flex h-full min-h-0 flex-col bg-[#15171B]"
      data-pane={split ? paneId : undefined}
      onMouseDown={split ? () => useAppStore.getState().setActivePane(paneId) : undefined}
      onDragEnter={onDragEnter}
      onDragOver={(e) => { if (Array.from(e.dataTransfer?.types || []).includes('Files')) e.preventDefault() }}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      {dragOver && (
        <div className="pointer-events-none absolute inset-0 z-[80] flex items-center justify-center bg-[#0B0D10]/70 backdrop-blur-[1px]">
          <div className="rounded-2xl border-2 border-dashed border-[#4C9DFF]/70 bg-[#1E2127]/95 px-8 py-6 text-center shadow-2xl">
            <Paperclip className="mx-auto mb-2 h-6 w-6 text-[#4C9DFF]" />
            <div className="text-sm font-semibold text-[#EDEEF0]">Soltá los archivos para adjuntarlos</div>
            <div className="mt-1 text-[11px] text-[#9BA1AA]">Imágenes, PDF, texto o código · hasta 8 por mensaje</div>
          </div>
        </div>
      )}
      {/* header de sesión (titulo + consumo + acciones) */}
      <div className="flex h-12 shrink-0 items-center gap-3 border-b border-[#22262D] px-5">
        <div className="min-w-0">
          <div className="truncate text-sm font-semibold leading-tight text-[#EDEEF0]" title={titulo}>{titulo}</div>
          <div className="truncate text-[11px] leading-tight text-[#9BA1AA]">
            {proyecto ? `${proyecto} · ` : ''}{activeSession ? activeSession : 'sin sesión'}
          </div>
        </div>
        <div className="ml-auto flex items-center gap-2 text-[11px] text-[#9BA1AA]">
          <ContextMeter sessionId={activeSession} refreshKey={`${messages.length}-${isStreaming}`} onToast={toast} />
          {usage && usage.tokens > 0 && (
            <span title={`${usage.messages} mensajes · ${usage.tokens} tokens`} className="hidden whitespace-nowrap tabular-nums sm:inline">
              {usage.tokens >= 1000 ? `${(usage.tokens / 1000).toFixed(1)} mil` : usage.tokens} tokens
              {usage.cost.total_usd > 0 && ` · ${usage.cost.total_usd.toFixed(2)} US$`}
            </span>
          )}
          <button
            onClick={toggleSessionMode}
            disabled={modeBusy || !activeSession}
            title={sessionMode === 'plan' ? 'Modo plan activo: solo lectura. Clic para volver a act' : 'Activar modo plan (solo lectura)'}
            aria-pressed={sessionMode === 'plan'}
            className={cn(
              'rounded-md border px-2 py-1 text-[11px] disabled:opacity-50',
              sessionMode === 'plan'
                ? 'border-[#FFB86B]/50 bg-[#FFB86B]/10 text-[#FFB86B]'
                : 'border-[#2C3037] bg-[#1E2127] text-[#9BA1AA] hover:text-[#EDEEF0]',
            )}
          >
            {modeBusy ? '…' : sessionMode === 'plan' ? 'Plan' : 'Act'}
          </button>
          <div className="relative">
            <button
              onClick={() => setMenuOpen((v) => !v)}
              aria-label="Más acciones de la sesión"
              aria-expanded={menuOpen}
              className="rounded-md p-1 text-[#9BA1AA] hover:bg-[#262A31] hover:text-[#EDEEF0]"
            >
              <MoreHorizontal className="h-4 w-4" />
            </button>
            {menuOpen && (
              <>
                <div className="fixed inset-0 z-[60]" onClick={() => setMenuOpen(false)} />
                <div className="absolute right-0 top-full z-[70] mt-1 w-56 rounded-xl border border-[#2C3037] bg-[#1E2127] p-1 shadow-2xl">
                  <button onClick={() => { setMenuOpen(false); void copiarTranscripcion() }} disabled={!activeSession || messages.length === 0}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-[#C9CDD3] hover:bg-[#262A31] disabled:opacity-50">
                    {copiadoTodo ? <Check className="h-3.5 w-3.5 text-[#3DDC84]" /> : <ClipboardCopy className="h-3.5 w-3.5" />}
                    {copiadoTodo ? 'Transcripción copiada' : 'Copiar transcripción'}
                  </button>
                  <button onClick={() => { setMenuOpen(false); void descargarSesion() }} disabled={!activeSession || exporting}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-[#C9CDD3] hover:bg-[#262A31] disabled:opacity-50">
                    <Download className="h-3.5 w-3.5" /> {exporting ? 'Exportando…' : 'Exportar sesión (.zip)'}
                  </button>
                  <div className="my-1 h-px bg-[#2C3037]" />
                  <button onClick={() => { setMenuOpen(false); void speakLast() }} disabled={speaking}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-[#C9CDD3] hover:bg-[#262A31] disabled:opacity-50">
                    <Volume2 className="h-3.5 w-3.5" /> {speaking ? 'Leyendo…' : 'Escuchar respuesta'}
                  </button>
                  <button onClick={() => { setMenuOpen(false); void distillNow() }} disabled={distilling || !activeSession}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-[#C9CDD3] hover:bg-[#262A31] disabled:opacity-50">
                    <Database className="h-3.5 w-3.5" /> {distilling ? 'Destilando…' : 'Destilar al grafo'}
                  </button>
                  <button onClick={() => { setMenuOpen(false); if (activeSession) forkSession(activeSession) }} disabled={!activeSession}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-[#C9CDD3] hover:bg-[#262A31] disabled:opacity-50">
                    <GitBranch className="h-3.5 w-3.5" /> Bifurcar sesión
                  </button>
                  <button onClick={() => { setMenuOpen(false); toggleSessionMode() }} disabled={!activeSession}
                    className="flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-xs text-[#C9CDD3] hover:bg-[#262A31] disabled:opacity-50">
                    <Eye className="h-3.5 w-3.5" /> {sessionMode === 'plan' ? 'Volver a Act' : 'Modo Plan (solo lectura)'}
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      </div>

      {/* messages */}
      <div ref={scrollRef} onScroll={onScrollMensajes} className="min-h-0 flex-1 overflow-y-auto px-4 py-5">
        <div className="mx-auto flex w-full max-w-3xl flex-col gap-4">
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
                Nexus v3 conectado al backend. Escribe / para comandos · Ctrl+V pega archivos · el micrófono transcribe con el STT local.
              </div>
            </div>
            <div className="flex flex-wrap justify-center gap-2">
              <button onClick={() => setInput('/help')} className="rounded-full border border-[#2C3037] bg-[#22262D] px-3 py-1.5 text-xs text-[#9BA1AA] hover:text-[#EDEEF0]">/help</button>
              <button onClick={() => setInput('explícame la compactación 4 capas')} className="rounded-full border border-[#2C3037] bg-[#22262D] px-3 py-1.5 text-xs text-[#9BA1AA] hover:text-[#EDEEF0]">Compactación 4 capas</button>
              <button onClick={() => setInput('lista el grafo de conocimiento')} className="rounded-full border border-[#2C3037] bg-[#22262D] px-3 py-1.5 text-xs text-[#9BA1AA] hover:text-[#EDEEF0]">Grafo</button>
            </div>
          </div>
        )}
        {grupos.map((g) => {
          if (g.tipo === 'tools') {
            const items = g.items
            const abierto = items.some((x) => expanded.has(x.id))
            return (
              <div key={`tools-${items[0].id}`} className="self-start">
                <button
                  onClick={() => { items.forEach((x) => toggleExpanded(x.id)) }}
                  aria-expanded={abierto}
                  className="flex items-center gap-2 rounded-lg px-1 py-1 text-left text-xs text-[#9BA1AA] hover:text-[#EDEEF0]"
                >
                  <Wrench className="h-3.5 w-3.5 shrink-0 text-[#FFB86B]/80" />
                  <span>Se {items.length === 1 ? 'usó' : 'usaron'} {items.length} <span className="font-mono text-[#C9CDD3]">{etiquetaTool(g.nombre)}</span></span>
                  <ChevronRight className={cn('h-3.5 w-3.5 shrink-0 transition-transform', abierto && 'rotate-90')} />
                </button>
                {abierto && (
                  <div className="mt-1 space-y-1 border-l border-[#2C3037] pl-3">
                    {items.map((t) => (
                      <div key={t.id} className="rounded-lg border border-[#22262D] bg-[#1A1D23] px-2.5 py-1.5">
                        <div className="flex items-center gap-2 text-[11px] text-[#9BA1AA]">
                          <span className="font-mono text-[#FFB86B]/80">{etiquetaTool(t.toolName || 'tool')}</span>
                          <span className="min-w-0 flex-1 truncate">{summarizeTool(t.toolName, t.content)}</span>
                          <span className="shrink-0 text-[#59616D]">{t.timestamp}</span>
                        </div>
                        <div className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-words text-[11px] leading-relaxed text-[#C9D2E0]">
                          {t.toolName === 'apply_patch' ? <ApplyPatchResult content={t.content} /> : <ToolOutput content={t.content} />}
                        </div>
                        <button
                          onClick={() => { void navigator.clipboard.writeText(t.content).catch(() => {}); setCopied(t.id); setTimeout(() => setCopied(null), 1200) }}
                          className="mt-1 text-[10px] text-[#8CC1FF] hover:underline"
                        >
                          {copied === t.id ? 'copiado' : 'copiar salida'}
                        </button>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )
          }

          const m = g.m
          if (m.role === 'system' || m.kind === 'compaction_summary') {
            return (
              <div key={m.id} className="overflow-hidden rounded-xl border border-[#4C9DFF]/25 bg-[#182233]">
                <button onClick={() => toggleExpanded(m.id)} aria-expanded={expanded.has(m.id)}
                  className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs hover:bg-[#1C2840]">
                  <Database className="h-3.5 w-3.5 shrink-0 text-[#8CC1FF]" />
                  <span className="shrink-0 font-semibold text-[#8CC1FF]">Resumen de contexto</span>
                  <span className="min-w-0 flex-1 truncate text-[#9BA1AA]">{summarize(summaryBody(m.content))}</span>
                  <span className="shrink-0 text-[#59616D]">{m.timestamp}</span>
                  <ChevronRight className={cn('h-3.5 w-3.5 shrink-0 text-[#9BA1AA] transition-transform', expanded.has(m.id) && 'rotate-90')} />
                </button>
                {expanded.has(m.id) && (
                  <div className="max-h-80 overflow-auto border-t border-[#4C9DFF]/20 px-3 py-2 text-xs leading-relaxed whitespace-pre-wrap break-words text-[#C9D2E0]">
                    {summaryBody(m.content)}
                  </div>
                )}
              </div>
            )
          }

          if (m.role === 'assistant') {
            return (
              <div key={m.id} className="group relative text-sm leading-relaxed text-[#EDEEF0]">
                {m.images && m.images.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-2">
                    {m.images.map((src, i) => (
                      <img key={i} src={src} alt={`adjunto ${i + 1}`} className="h-24 w-24 rounded-lg border border-[#2C3037] object-cover" />
                    ))}
                  </div>
                )}
                <div className="break-words"><Markdown text={sanitizeAssistantText(m.content)} /></div>
                <div className="mt-1 flex items-center gap-2 text-[11px] text-[#59616D]">
                  <span>{m.timestamp}</span>
                  <button
                    onClick={() => { void navigator.clipboard.writeText(m.content).catch(() => {}); setCopied(m.id); setTimeout(() => setCopied(null), 1200) }}
                    aria-label={copied === m.id ? 'Copiado' : 'Copiar mensaje'}
                    className="opacity-0 transition-opacity hover:text-[#EDEEF0] group-hover:opacity-100"
                  >
                    {copied === m.id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                  </button>
                </div>
              </div>
            )
          }

          return (
            <div key={m.id} className="flex justify-end">
              <div className="group relative max-w-[85%] rounded-2xl rounded-br-md bg-[#262A31] px-3.5 py-2.5 text-sm leading-relaxed text-[#EDEEF0]">
                {m.images && m.images.length > 0 && (
                  <div className="mb-2 flex flex-wrap gap-2">
                    {m.images.map((src, i) => (
                      <img key={i} src={src} alt={`adjunto ${i + 1}`} className="h-24 w-24 rounded-lg border border-[#2C3037] object-cover" />
                    ))}
                  </div>
                )}
                <div className="whitespace-pre-wrap break-words">{m.content}</div>
                <div className="mt-1 flex items-center justify-end gap-2 text-[11px] text-[#9BA1AA]">
                  <span>{m.timestamp}</span>
                  <button
                    onClick={() => { void volverAca(m) }}
                    disabled={isStreaming}
                    title="Volver acá (descarta lo posterior y trae el texto al composer)"
                    aria-label="Volver a este mensaje"
                    className="opacity-0 transition-opacity hover:text-[#EDEEF0] disabled:opacity-30 group-hover:opacity-100"
                  >
                    <Undo2 className="h-3 w-3" />
                  </button>
                  <button
                    onClick={() => { void navigator.clipboard.writeText(m.content).catch(() => {}); setCopied(m.id); setTimeout(() => setCopied(null), 1200) }}
                    aria-label={copied === m.id ? 'Copiado' : 'Copiar mensaje'}
                    className="opacity-0 transition-opacity hover:text-[#EDEEF0] group-hover:opacity-100"
                  >
                    {copied === m.id ? <Check className="h-3 w-3" /> : <Copy className="h-3 w-3" />}
                  </button>
                </div>
              </div>
            </div>
          )
        })}

        {(lastTodos && lastTodos.todos?.length > 0) && (
          <div className="rounded-xl border border-[#4C9DFF]/20 bg-[#4C9DFF]/10 p-3 text-xs">
            <div className="flex items-center gap-2 font-semibold text-[#8CC1FF]"><ListTodo className="h-3.5 w-3.5" /> Todos: {lastTodos.counts?.completed ?? 0}/{lastTodos.todos.length} completados</div>
            <div className="mt-2 space-y-1">{lastTodos.todos.map((td:any,i:number)=>(<div key={i} className="flex gap-2"><span className={td.status==='completed'?'text-[#3DDC84]':td.status==='in_progress'?'text-[#FFB86B]':'text-[#9BA1AA]'}>{td.status==='completed'?'●':td.status==='in_progress'?'◐':'○'}</span><span className={td.status==='completed'?'line-through text-[#9BA1AA]':''}>{td.content}</span><span className="ml-auto text-[#9BA1AA]">{td.status==='completed'?'hecho':td.status==='in_progress'?'en curso':'pendiente'}</span></div>))}</div>
          </div>
        )}
        {(lastSteps && lastSteps.length>0) && (
          <div className="rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 text-xs">
            <button onClick={()=>setShowTraj(v=>!v)} className="flex items-center gap-2 font-semibold text-[#EDEEF0]"><Eye className="h-3.5 w-3.5" /> Trayectoria: {lastSteps.length} pasos {isStreaming && <span className="text-[#FFB86B]">· en vivo</span>} {showTraj? '▾':'▸'}</button>
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
          <div className="flex items-center gap-2 self-start pl-1 text-xs text-[#9BA1AA]">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-[#4C9DFF]" />
            <span>Trabajando…</span>
          </div>
        )}
        <InlineAsks />
        <div ref={bottomRef} />
        </div>
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
      <div className="relative border-t border-[#2C3037] bg-[#15171B] px-4 py-3">
        {hayQueBajar && (
          <button
            onClick={irAlFinal}
            aria-label="Ir al final del chat"
            title="Ir al final"
            className="absolute -top-12 right-6 z-40 flex items-center gap-1.5 rounded-full border border-[#2C3037] bg-[#1E2127] px-3 py-1.5 text-xs text-[#C9CDD3] shadow-xl hover:bg-[#262A31] hover:text-[#EDEEF0]"
          >
            <ArrowDown className="h-3.5 w-3.5" /> Ir al final
          </button>
        )}
        <div className="mx-auto w-full max-w-3xl">
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

        <div className="rounded-2xl border border-[#2C3037] bg-[#1E2127] p-2 transition-colors focus-within:border-[#4C9DFF]/50">
          <input ref={fileInputRef} type="file" multiple className="hidden" onChange={onFilePicked} accept="*/*" />
          <textarea
            ref={textareaRef}
            value={input}
            onChange={(e)=>{ const v=e.target.value; setInput(v); if(v.startsWith('/')){ setSlashOpen(true); setSlashQuery(v.slice(1).split(' ')[0]) } else setSlashOpen(false) }}
            onKeyDown={(e)=>{ if(e.key==='Escape' && slashOpen){ e.preventDefault(); setSlashOpen(false); return } if(e.key==='Enter' && !e.shiftKey && !e.nativeEvent.isComposing){ e.preventDefault(); send() }}}
            placeholder={isStreaming ? 'Dirigir el run en curso… (Enter encola la instrucción)' : 'Preguntá, pedí un cambio o escribí / para comandos'}
            rows={1}
            aria-label="Mensaje a Nexus"
            className="max-h-40 min-h-[52px] w-full resize-none bg-transparent px-2 py-1.5 text-sm text-[#EDEEF0] placeholder:text-[#9BA1AA] focus:outline-none"
          />
          <div className="flex items-center gap-1">
            <button
              onClick={() => fileInputRef.current?.click()}
              aria-label="Adjuntar archivo"
              title="Adjuntar archivo (Ctrl+V también pega)"
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[#9BA1AA] hover:bg-[#262A31] hover:text-[#EDEEF0]"
            >
              <Paperclip className="h-4 w-4" />
            </button>
            <button
              onClick={captureScreen}
              aria-label="Capturar pantalla"
              title="Capturar pantalla"
              className="flex h-7 w-7 shrink-0 items-center justify-center rounded-md text-[#9BA1AA] hover:bg-[#262A31] hover:text-[#EDEEF0]"
            >
              <Camera className="h-4 w-4" />
            </button>
            <VoiceMic onTranscript={onTranscript} />
            <span className="mx-1 h-4 w-px bg-[#2C3037]" />
            <AgentSelector compact />
            <ModelSelector compact />
            <button
              onClick={toggleSessionMode}
              disabled={modeBusy || !activeSession}
              title={sessionMode === 'plan' ? 'Modo plan activo: solo lectura. Clic para volver a act' : 'Activar modo plan (solo lectura)'}
              aria-pressed={sessionMode === 'plan'}
              className={cn('rounded-md px-2 py-1 text-xs disabled:opacity-50',
                sessionMode === 'plan' ? 'bg-[#FFB86B]/10 text-[#FFB86B]' : 'text-[#9BA1AA] hover:bg-[#262A31] hover:text-[#EDEEF0]')}
            >
              {modeBusy ? '…' : sessionMode === 'plan' ? 'Plan' : 'Act'}
            </button>
            <span className="ml-auto hidden pr-1 text-[10px] text-[#59616D] sm:inline">
              {isStreaming ? 'Enter encola · corriendo' : 'Enter envía · Shift+Enter salto'}
            </span>
            <button
              onClick={isStreaming ? detener : () => send()}
              disabled={!isStreaming && !input.trim() && pending.length === 0}
              aria-label={isStreaming ? 'Detener la respuesta' : 'Enviar mensaje'}
              title={isStreaming ? 'Detener (Esc)' : 'Enviar mensaje'}
              className={cn(
                'flex h-7 w-7 shrink-0 items-center justify-center rounded-full transition-colors disabled:bg-[#2C3037] disabled:text-[#59616D]',
                isStreaming
                  ? 'border border-[#FF6B6B]/50 bg-[#FF6B6B]/15 text-[#FFA8A8] hover:bg-[#FF6B6B]/25'
                  : 'bg-[#4C9DFF] text-[#0B1220] hover:bg-[#6FB0FF]',
              )}
            >
              {isStreaming ? <Square className="h-3.5 w-3.5" /> : <ArrowUp className="h-4 w-4" />}
            </button>
          </div>
        </div>
        <div className="mt-2 flex items-center gap-2 text-[10px] text-[#59616D]">
          <Sparkles className="h-3 w-3 text-[#4C9DFF]" />
          <span className="truncate">Bucle TDAO · Guardia de Alcance · Compactación 4 capas</span>
          {distillMsg && <span className="truncate text-[#4C9DFF]">· {distillMsg}</span>}
          {attachToast && <span className="truncate text-[#FFB86B]">· {attachToast}</span>}
        </div>
        </div>
      </div>
    </div>
  )
}