import { create } from 'zustand'
import { providersApi, sessionsApi, createSessionApi, deleteSessionApi, sessionMessagesApi } from '@/lib/api'

export type ProviderStatus = 'online' | 'offline' | 'loading'
export type ModelOption = { id: string; provider: string; label: string; ctx: number; status: ProviderStatus }

type View = 'chat' | 'graph' | 'editor' | 'terminal' | 'agents' | 'review' | 'board' | 'skills'

export type ChatMessage = { id: string; role: 'user' | 'assistant' | 'tool'; content: string; toolName?: string; timestamp: string; images?: string[] }

export type SessionMeta = { id: string; title: string; project?: string; summary?: string; source?: 'server' | 'local' }

type FileTab = { path: string; content: string; dirty: boolean; language: string }

const fmtTime = (iso: string): string => {
  try {
    const d = new Date(iso)
    return isNaN(d.getTime()) ? '' : d.toLocaleTimeString().slice(0, 5)
  } catch {
    return ''
  }
}

type AppState = {
  view: View
  setView: (v: View) => void
  // models — dynamic from /api/providers
  models: ModelOption[]
  selectedModel: string
  setModel: (id: string) => void
  providerHealth: Record<string, ProviderStatus>
  isProvidersLoading: boolean
  providersError: string | null
  refreshProviders: (force?: boolean) => Promise<void>
  setModels: (m: ModelOption[]) => void
  setProviderHealth: (h: Record<string, ProviderStatus>) => void
  // chat
  sessions: SessionMeta[]
  activeSession: string
  setActiveSession: (id: string) => void
  loadSessions: () => Promise<void>
  createSession: () => void
  deleteSession: (id: string) => void
  messages: ChatMessage[]
  addMessage: (m: ChatMessage) => void
  updateMessage: (id: string, content: string) => void
  setMessages: (m: ChatMessage[]) => void
  loadMessages: (sessionId: string) => Promise<void>
  // editor
  tabs: FileTab[]
  activeTab: number
  setActiveTab: (i: number) => void
  updateTab: (i: number, content: string) => void
  openFile: (path: string, content?: string) => void
  closeTab: (i: number) => void
  saveTab: (i: number) => void
  // terminal
  terminalLines: string[]
  appendTerminal: (line: string) => void
  clearTerminal: () => void
}

export const useAppStore = create<AppState>((set, get) => ({
  view: 'chat',
  setView: (view) => set({ view }),
  // Dynamic — populated via /api/providers health check. No hardcoded placeholders.
  models: [] as ModelOption[],
  selectedModel: 'auto',
  setModel: (selectedModel) => set({ selectedModel }),
  providerHealth: {} as Record<string, ProviderStatus>,
  isProvidersLoading: true as boolean,
  providersError: null as string | null,
  refreshProviders: async (force = false) => {
    set({ isProvidersLoading: true, providersError: null })
    try {
      const data = await providersApi(force)
      const health: Record<string, ProviderStatus> = {}
      for (const [k, v] of Object.entries(data.providers)) {
        if (k === 'error') continue
        health[k] = (v as { status: ProviderStatus }).status
      }
      // Filter out embed models already filtered server-side, keep only chat models
      const models: ModelOption[] = data.models as ModelOption[]
      // Auto-select first available if current selection missing or empty.
      // 'auto' = el backend enruta por hint/gema. Si no hay 'auto', preferir
      // qwen3.5-9b (modelo general que trabaja) en vez del director-orquestador.
      const current = get().selectedModel
      let nextSelect = current
      // Preferir 'auto' (router elige mejor modelo por hint/gema) — evita hang de modelo local lento
      if (!current || !models.find(m => m.id === current)) {
        nextSelect = 'auto'
      }
      set({ models, providerHealth: health, selectedModel: nextSelect, isProvidersLoading: false })
    } catch (e) {
      set({ isProvidersLoading: false, providersError: (e as Error).message ?? String(e) })
    }
  },
  setModels: (models: ModelOption[]) => set({ models }),
  setProviderHealth: (providerHealth: Record<string, ProviderStatus>) => set({ providerHealth }),
  sessions: [] as SessionMeta[],
  activeSession: '',
  // Cambiar de sesión limpia el buffer local (evita ver mensajes de otra sesión
  // mientras carga el historial desde el server).
  setActiveSession: (activeSession) =>
    set((s) => (s.activeSession === activeSession ? s : { activeSession, messages: [] })),
  loadSessions: async () => {
    try {
      const data = await sessionsApi()
      const sessions: SessionMeta[] = (data.sessions ?? []).map((s, i) => ({
        id: s.id,
        title: s.summary || s.project || `Sesión ${i + 1}`,
        project: s.project,
        summary: s.summary,
        source: 'server',
      }))
      set({ sessions, activeSession: get().activeSession || sessions[0]?.id || '' })
    } catch {
      // server caido — conservar sesiones locales cargadas
    }
  },
  createSession: () => {
    void (async () => {
      try {
        const s = await createSessionApi()
        set((st) => ({
          sessions: [...st.sessions, { id: s.id, title: s.summary || s.project || `Sesión ${st.sessions.length + 1}`, project: s.project, summary: s.summary, source: 'server' as const }],
          activeSession: s.id,
          messages: [],  // limpiar (evita filtrar la conversación de la sesión previa)
        }))
      } catch {
        // fallback local sin server
        set((st) => {
          const id = `s${Date.now()}`
          return { sessions: [...st.sessions, { id, title: `Sesión ${st.sessions.length + 1}`, source: 'local' as const }], activeSession: id }
        })
      }
    })()
  },
  deleteSession: (id) => {
    void (async () => {
      try { await deleteSessionApi(id) } catch { /* si falla el server, igual lo quitamos del listado */ }
      set((st) => {
        const sessions = st.sessions.filter((x) => x.id !== id)
        const wasActive = st.activeSession === id
        const activeSession = wasActive ? (sessions[0]?.id ?? '') : st.activeSession
        return { sessions, activeSession, messages: wasActive ? [] : st.messages }
      })
      // Si borramos la activa, cargar el historial de la nueva activa.
      const next = get().activeSession
      if (next) void get().loadMessages(next)
    })()
  },
  messages: [] as ChatMessage[],
  addMessage: (m) => set((s) => ({ messages: [...s.messages, m] })),
  updateMessage: (id, content) => set((s) => ({ messages: s.messages.map((m) => (m.id === id ? { ...m, content } : m)) })),
  setMessages: (messages) => set({ messages }),
  loadMessages: async (sessionId) => {
    if (!sessionId) return
    try {
      const raw = await sessionMessagesApi(sessionId)
      if (get().activeSession !== sessionId) return // carrera: la sesión cambió
      const mapped: ChatMessage[] = raw
        .filter((m) => m.role === 'user' || m.role === 'assistant' || m.role === 'tool')
        .map((m, i) => ({
          id: `${sessionId}-${i}`,
          role: m.role as ChatMessage['role'],
          content: m.content ?? '',
          toolName: m.tool_name || undefined,
          timestamp: m.created_at ? fmtTime(m.created_at) : '',
        }))
      // Merge: no pisar mensajes locales (recién enviados) que aún no están en el server.
      set((s) => {
        const serverIds = new Set(mapped.map((m) => m.id))
        const sig = new Set(mapped.map((m) => `${m.role}\u0000${m.content}`))
        const localOnly = s.messages.filter(
          (m) => !serverIds.has(m.id) && !m.id.startsWith(`${sessionId}-`) && !sig.has(`${m.role}\u0000${m.content}`),
        )
        return { messages: [...mapped, ...localOnly] }
      })
    } catch {
      // server caído o sesión local (fallback) → buffer vacío, nunca mensajes ajenos
      if (get().activeSession !== sessionId) return
      set({ messages: [] })
    }
  },
  tabs: [] as FileTab[],
  activeTab: 0,
  setActiveTab: (activeTab) => set({ activeTab }),
  updateTab: (i, content) => set((s) => { const tabs = [...s.tabs]; tabs[i] = { ...tabs[i], content, dirty: true }; return { tabs } }),
  openFile: (path, content = '# new file\n') => set((s) => {
    const existing = s.tabs.findIndex((t) => t.path === path)
    if (existing >= 0) {
      // ya abierto: no duplicar pestaña; solo activarla (y refrescar contenido si vino explícito)
      const tabs = content === '# new file\n' ? s.tabs : s.tabs.map((t, i) => (i === existing ? { ...t, content } : t))
      return { tabs, activeTab: existing }
    }
    return {
      tabs: [...s.tabs, {
        path,
        content,
        dirty: false,
        language: path.endsWith('.py') ? 'python'
          : path.endsWith('.yaml') || path.endsWith('.yml') ? 'yaml'
          : path.endsWith('.json') ? 'json'
          : path.endsWith('.md') ? 'markdown'
          : path.endsWith('.txt') ? 'plaintext'
          : 'typescript',
      }],
      activeTab: s.tabs.length,
    }
  }),
  closeTab: (i) => set((s) => {
    if (s.tabs.length <= 1) return s
    const tabs = s.tabs.filter((_, idx) => idx !== i)
    let activeTab = s.activeTab
    if (i < activeTab) activeTab -= 1
    else if (i === activeTab) activeTab = Math.min(activeTab, tabs.length - 1)
    return { tabs, activeTab }
  }),
  saveTab: (i) => set((s) => { const tabs = [...s.tabs]; tabs[i] = { ...tabs[i], dirty: false }; return { tabs } }),
  terminalLines: ['Nexus v3 — Harnés listo. Usa "ayuda" para comandos.'],
  appendTerminal: (line) => set((s) => ({ terminalLines: [...s.terminalLines, line] })),
  clearTerminal: () => set({ terminalLines: [] }),
}))
