import { create } from 'zustand'
import { providersApi } from '@/lib/api'

export type ProviderStatus = 'online' | 'offline' | 'loading'
export type ModelOption = { id: string; provider: string; label: string; ctx: number; status: ProviderStatus }

type View = 'chat' | 'graph' | 'editor' | 'terminal'

export type ChatMessage = { id: string; role: 'user' | 'assistant' | 'tool'; content: string; toolName?: string; timestamp: string }

type FileTab = { path: string; content: string; dirty: boolean; language: string }

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
  refreshProviders: () => Promise<void>
  setModels: (m: ModelOption[]) => void
  setProviderHealth: (h: Record<string, ProviderStatus>) => void
  // chat
  sessions: { id: string; title: string }[]
  activeSession: string
  setActiveSession: (id: string) => void
  createSession: () => void
  messages: ChatMessage[]
  addMessage: (m: ChatMessage) => void
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
  refreshProviders: async () => {
    set({ isProvidersLoading: true, providersError: null })
    try {
      const data = await providersApi()
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
  sessions: [] as { id: string; title: string }[],
  activeSession: '',
  setActiveSession: (activeSession) => set({ activeSession }),
  createSession: () => set((s) => {
    const id = `s${Date.now()}`
    return { sessions: [...s.sessions, { id, title: `Sesión ${s.sessions.length + 1}` }], activeSession: id }
  }),
  messages: [] as ChatMessage[],
  addMessage: (m) => set((s) => ({ messages: [...s.messages, m] })),
  tabs: [] as FileTab[],
  activeTab: 0,
  setActiveTab: (activeTab) => set({ activeTab }),
  updateTab: (i, content) => set((s) => { const tabs = [...s.tabs]; tabs[i] = { ...tabs[i], content, dirty: true }; return { tabs } }),
  openFile: (path, content = '# new file\n') => set((s) => ({ tabs: [...s.tabs, { path, content, dirty: false, language: path.endsWith('.py') ? 'python' : path.endsWith('.yaml') ? 'yaml' : 'typescript' }], activeTab: s.tabs.length })),
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
