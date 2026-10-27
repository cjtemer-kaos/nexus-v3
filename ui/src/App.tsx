import { Sidebar } from '@/components/Sidebar'
import { SessionTabs } from '@/components/chat/SessionTabs'
import { ChatPanel } from '@/components/chat/ChatPanel'
import { CommandPalette } from '@/components/CommandPalette'
import { GraphView } from '@/components/graph/GraphView'
import { MemoryPanel } from '@/components/memory/MemoryPanel'
import { FileEditor } from '@/components/editor/FileEditor'
import { TerminalPanel } from '@/components/terminal/TerminalPanel'
import { AgentsPanel } from '@/components/agents/AgentsPanel'
import { ReviewPanel } from '@/components/review/ReviewPanel'
import { BoardPanel } from '@/components/board/BoardPanel'
import { SkillsPanel } from '@/components/skills/SkillsPanel'
import { SubAgentsPanel } from '@/components/subagents/SubAgentsPanel'
import { ApprovalsPanel } from '@/components/approvals/ApprovalsPanel'
import { PlanPanel } from '@/components/plan/PlanPanel'
import { WorktreesPanel } from '@/components/worktrees/WorktreesPanel'
import { UsagePanel } from '@/components/usage/UsagePanel'
import { UpdateBanner } from '@/components/UpdateBanner'
import { useAppStore } from '@/store/app-store'
import { Search, Bell, User } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useState, useEffect } from 'react'

const viewLabels: Record<string, string> = {
  chat: 'Chat',
  graph: 'Grafo',
  memory: 'Memoria',
  editor: 'Editor',
  terminal: 'Terminal',
  agents: 'Ayudantes',
  review: 'Cambios',
  approvals: 'Aprobaciones',
  plan: 'Plan',
  worktrees: 'Worktrees',
  usage: 'Uso',
  board: 'Tareas',
  subagents: 'Sub-agentes',
  skills: 'Capacidades',
}

type SearchResult = { role: string; content: string; session_id?: string }

export default function App() {
  const { view, setView, refreshProviders, loadSessions, ensureSession, liveStatus, loadStatus, activeSession, createSession, closeSessionTab } = useAppStore()
  const [showNotifications, setShowNotifications] = useState(false)

  useEffect(() => {
    // Poll único de /api/status (lo comparten App + SubAgentsPanel vía el store).
    void loadStatus()
    const iv = setInterval(() => { void loadStatus() }, 4000)
    return () => clearInterval(iv)
  }, [loadStatus])
  const live = liveStatus ? { running: liveStatus.counts.running, subs: liveStatus.counts.sessions_with_subagents } : null

  useEffect(() => {
    // Kick off dynamic provider detection at app start
    refreshProviders()
    // Hidratar sesiones reales del server (SessionLog WAL+FTS) y garantizar una activa
    void (async () => { await loadSessions(); await ensureSession() })()
  }, [refreshProviders, loadSessions, ensureSession])

  // Atajos globales: Ctrl+N sesión nueva,
  // Ctrl+W cierra la pestaña activa (la sesión queda guardada), Ctrl+B pliega
  // el sidebar a rail. Ctrl+K lo maneja la paleta.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!(e.metaKey || e.ctrlKey) || e.altKey || e.shiftKey) return
      const k = e.key.toLowerCase()
      if (k === 'n') { e.preventDefault(); setView('chat'); void createSession() }
      else if (k === 'w' && activeSession) { e.preventDefault(); closeSessionTab(activeSession) }
      else if (k === 'b') { e.preventDefault(); window.dispatchEvent(new Event('nexus:rail')) }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [setView, createSession, closeSessionTab, activeSession])

  return (
    <div className="flex h-dvh w-full overflow-hidden bg-[#15171B] text-[#EDEEF0]">
      <Sidebar />

      <div className="flex flex-1 flex-col min-w-0 min-h-0">
        {/* topbar — pestañas de sesión + búsqueda y cuenta */}
        <header className="relative z-30 flex h-12 shrink-0 items-stretch gap-2 border-b border-[#22262D] bg-[#15171B] px-2">
          {view === 'chat' ? (
            <div className="min-w-0 flex-1">
              <SessionTabs />
            </div>
          ) : (
            <div className="flex min-w-0 flex-1 items-center gap-2 px-2 text-sm">
              <span className="text-[#9BA1AA]">Arnés</span>
              <span className="text-[#3A3F47]">/</span>
              <span className="font-medium">{viewLabels[view]}</span>
              <span className="ml-2 hidden rounded-full border border-[#3DDC84]/20 bg-[#3DDC84]/15 px-2 py-0.5 text-xs text-[#3DDC84] lg:inline">WAL+FTS · {activeSession}</span>
            </div>
          )}

          <div className="flex shrink-0 items-center gap-2">
            <button
              onClick={() => window.dispatchEvent(new Event('nexus:palette'))}
              title="Buscar o ejecutar un comando (Ctrl+K)"
              aria-label="Abrir paleta de comandos"
              className="flex items-center gap-2 rounded-lg border border-[#2C3037] bg-[#1E2127] py-1.5 pl-2.5 pr-2 text-xs text-[#9BA1AA] hover:border-[#4C9DFF]/40 hover:text-[#EDEEF0]"
            >
              <Search className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">Buscar…</span>
              <kbd className="hidden rounded border border-[#2C3037] bg-[#22262D] px-1 py-0.5 text-[10px] sm:inline">Ctrl K</kbd>
            </button>



            <div className="relative hidden sm:flex">
              <Button variant="ghost" size="icon" aria-label="Notificaciones" onClick={()=>setShowNotifications(v=>!v)}>
                <Bell className="h-4 w-4" />
                {((liveStatus?.approvals_pending ?? 0) + (liveStatus?.questions_pending ?? 0)) > 0 && (
                  <span className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-[#FF6259] px-1 text-[9px] font-semibold text-white">
                    {(liveStatus!.approvals_pending ?? 0) + (liveStatus!.questions_pending ?? 0)}
                  </span>
                )}
              </Button>
              {showNotifications && (
                <>
                  <div className="fixed inset-0 z-[60]" onClick={()=>setShowNotifications(false)} />
                  <div className="absolute right-0 top-full z-[70] mt-2 w-72 rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 shadow-2xl">
                    <div className="text-sm font-semibold">Notificaciones</div>
                    <div className="mt-1 text-xs text-[#9BA1AA]">
                      {!liveStatus
                        ? 'Sin conexión con el backend: no se pudo leer /api/status.'
                        : (liveStatus.counts.running > 0 || liveStatus.counts.sessions_with_subagents > 0 || (liveStatus.approvals_pending ?? 0) > 0 || (liveStatus.questions_pending ?? 0) > 0)
                          ? [
                              liveStatus.counts.running > 0 ? `${liveStatus.counts.running} turno(s) en curso` : '',
                              liveStatus.counts.sessions_with_subagents > 0 ? `${liveStatus.counts.sessions_with_subagents} sesión(es) con sub-agentes` : '',
                              (liveStatus.approvals_pending ?? 0) > 0 ? `${liveStatus.approvals_pending} aprobación(es) pendiente(s)` : '',
                              (liveStatus.questions_pending ?? 0) > 0 ? `${liveStatus.questions_pending} pregunta(s) esperando tu respuesta` : '',
                            ].filter(Boolean).join(' · ')
                          : 'Sin notificaciones nuevas. El arnés está listo.'}
                    </div>
                    <div className={`mt-3 rounded-lg p-2 text-xs ${liveStatus ? 'bg-[#1E2127] text-[#9BA1AA]' : 'bg-[#FF6B6B]/10 text-[#FFA8A8]'}`}>
                      {liveStatus ? `WAL+FTS activo · sesión ${activeSession}` : 'Revisá data/backend.log'}
                    </div>
                    {(liveStatus?.questions_pending ?? 0) + (liveStatus?.approvals_pending ?? 0) > 0 && (
                      <button
                        onClick={() => { setView('chat'); setShowNotifications(false) }}
                        className="mt-2 w-full rounded-lg border border-[#4C9DFF]/40 bg-[#4C9DFF]/10 px-2 py-1.5 text-xs text-[#8CC1FF] hover:bg-[#4C9DFF]/20"
                      >
                        Responder en el chat
                      </button>
                    )}
                  </div>
                </>
              )}
            </div>
            <div className="mr-1 flex h-7 w-7 items-center justify-center rounded-full bg-[#0B62D6] text-[11px] font-semibold text-white" title="Usuario Nexus">NX</div>
          </div>

        </header>

        <UpdateBanner />

        {/* content */}
        <main className="flex-1 min-h-0 overflow-hidden bg-[radial-gradient(ellipse_at_top,_rgba(76,157,255,0.06),_transparent_55%)]">
          {view==='chat' && <ChatPanel />}
          {view==='graph' && <GraphView />}
          {view==='memory' && <MemoryPanel />}
          {view==='editor' && <FileEditor />}
          {view==='terminal' && <TerminalPanel />}
          {view==='agents' && <AgentsPanel />}
          {view==='review' && <ReviewPanel />}
          {view==='approvals' && <ApprovalsPanel />}
          {view==='plan' && <PlanPanel />}
          {view==='worktrees' && <WorktreesPanel />}
          {view==='usage' && <UsagePanel />}
          {view==='board' && <BoardPanel />}
          {view==='subagents' && <SubAgentsPanel />}
          {view==='skills' && <SkillsPanel />}
        </main>

        {/* estado inferior */}
        <footer className="flex h-6 shrink-0 items-center gap-3 border-t border-[#2C3037] bg-[#1E2127] px-3 text-xs text-[#9BA1AA]">
          <span className="flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full ${live ? (live.running ? 'bg-[#4C9DFF] animate-pulse' : 'bg-[#3DDC84]') : 'bg-[#FF6B6B]'}`} />{' '}
            <span className={live ? '' : 'text-[#FFA8A8]'}>
              {live ? (live.running ? `${live.running} run activo` : 'arnés listo') : 'sin conexión'}{live?.subs ? ` · ${live.subs} con subagentes` : ''}
            </span>
          </span>
          <span className="hidden md:inline">·</span>
          <span className="hidden md:inline">config.yaml · nada hardcodeado · distribuible</span>
            <span className="ml-auto hidden md:inline">Alcance: según Ajustes → Permisos (full = toda la PC) · secretos bloqueados</span>
          <span className="ml-auto md:ml-0 flex items-center gap-2"><span className="rounded bg-[#2C3037] px-1.5 py-0.5 font-mono text-[11px]"><User className="h-3 w-3 inline mr-1" />nexus-v3 3.0.0-alpha</span></span>
        </footer>
      </div>

      <CommandPalette onAjustes={() => window.dispatchEvent(new Event('nexus:ajustes'))} />
    </div>
  )
}