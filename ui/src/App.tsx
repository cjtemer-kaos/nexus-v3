import { Sidebar } from '@/components/Sidebar'
import { ModelSelector } from '@/components/ModelSelector'
import { AgentSelector } from '@/components/AgentSelector'
import { ChatPanel } from '@/components/chat/ChatPanel'
import { GraphView } from '@/components/graph/GraphView'
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
import { useAppStore } from '@/store/app-store'
import { Search, Command, Bell, User, FileText, Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useState, useEffect } from 'react'
import { searchApi } from '@/lib/api'

const viewLabels: Record<string, string> = {
  chat: 'Chat',
  graph: 'Grafo',
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

type SearchResult = { role: string; content: string }

export default function App() {
  const { view, setView, activeSession, refreshProviders, loadSessions, ensureSession, liveStatus, loadStatus } = useAppStore()
  const [search, setSearch] = useState('')
  const [searchResults, setSearchResults] = useState<SearchResult[] | null>(null)
  const [searching, setSearching] = useState(false)
  const [searchError, setSearchError] = useState<string | null>(null)
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

  const runSearch = async () => {
    const q = search.trim()
    if (q.length < 2) { setSearchResults(null); setSearchError(null); return }
    setSearching(true); setSearchError(null)
    try {
      const results = await searchApi(q)
      setSearchResults(results)
      if (!results.length) setSearchError('Sin resultados')
    } catch (e) {
      setSearchResults(null)
      setSearchError('Búsqueda no disponible: ' + (e as Error).message.slice(0, 80))
    } finally {
      setSearching(false)
    }
  }

  const closeSearch = () => { setSearchResults(null); setSearchError(null) }

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        document.getElementById('global-search')?.focus()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  return (
    <div className="flex h-dvh w-full overflow-hidden bg-[#15171B] text-[#EDEEF0]">
      <Sidebar />

      <div className="flex flex-1 flex-col min-w-0 min-h-0">
        {/* topbar — z-30 ensures dropdowns inside header paint above main content (backdrop-blur creates stacking context) */}
        <header className="relative z-30 flex h-14 shrink-0 items-center gap-3 border-b border-[#2C3037] bg-[#15171B]/80 backdrop-blur px-3 sm:px-4">
          <div className="hidden md:flex items-center gap-2 text-sm shrink-0">
            <span className="text-[#9BA1AA]">Arnés</span>
            <span className="text-zinc-700">/</span>
            <span className="font-medium">{viewLabels[view]}</span>
            <span className="ml-2 hidden lg:inline rounded-full bg-[#3DDC84]/15 border border-[#3DDC84]/20 px-2 py-0.5 text-xs text-[#3DDC84]">WAL activado · FTS5 · sesión {activeSession}</span>
          </div>

          <div className="flex flex-1 min-w-0 justify-center px-1">
            <div className="relative w-full max-w-md">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-[#9BA1AA]" />
              <input
                id="global-search"
                value={search}
                onChange={e=>setSearch(e.target.value)}
                onKeyDown={e=>{
                  if (e.key === 'Enter') { e.preventDefault(); void runSearch() }
                  if (e.key === 'Escape') { e.preventDefault(); closeSearch() }
                }}
                placeholder="Buscar mensajes, archivos, grafo…  (⌘K)"
                aria-label="Buscar en Nexus"
                className="w-full rounded-xl border border-[#2C3037] bg-[#1E2127] pl-9 pr-16 py-2 text-sm placeholder:text-[#9BA1AA] focus:outline-none focus:border-[#4C9DFF]/50"
              />
              <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 hidden sm:flex items-center gap-1 rounded-md border border-[#2C3037] bg-[#22262D] px-1.5 py-0.5 text-xs text-[#9BA1AA]"><Command className="h-3 w-3" />K</span>

              {(searching || searchResults || searchError) && (
                <>
                  <div className="fixed inset-0 z-[60]" onClick={closeSearch} />
                  <div className="absolute left-0 right-0 top-full z-[70] mt-2 rounded-xl border border-[#2C3037] bg-[#1E2127] shadow-2xl">
                    <div className="flex items-center gap-2 border-b border-[#2C3037] px-3 py-2 text-xs text-[#9BA1AA]">
                      {searching ? <><Loader2 className="h-3 w-3 animate-spin" /> Buscando “{search.trim()}”…</> : <><FileText className="h-3 w-3" /> {searchError ? searchError : `${searchResults!.length} resultado${searchResults!.length===1?'':'s'} FTS5 global`}</>}
                    </div>
                    {searchResults && searchResults.length > 0 && (
                      <div className="max-h-72 overflow-auto p-1.5">
                        {searchResults.map((r, i) => (
                          <button
                            key={i}
                            onMouseDown={e=>e.preventDefault()}
                            onClick={()=>{ closeSearch(); setView('chat') }}
                            className="flex w-full items-start gap-2 rounded-lg px-2 py-2 text-left hover:bg-[#2C3037]"
                          >
                            <span className={`mt-0.5 shrink-0 rounded px-1.5 py-0.5 text-[10px] font-medium ${r.role==='user' ? 'bg-[#0B62D6]/20 text-[#8CC1FF]' : r.role==='tool' ? 'bg-[#FFB86B]/15 text-[#FFB86B]' : 'bg-[#3DDC84]/15 text-[#3DDC84]'}`}>{r.role}</span>
                            <span className="line-clamp-2 text-xs leading-relaxed text-[#EDEEF0]">{r.content.slice(0, 220)}</span>
                          </button>
                        ))}
                      </div>
                    )}
                  </div>
                </>
              )}
            </div>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <AgentSelector />
            <ModelSelector />
            <div className="relative hidden sm:flex">
              <Button variant="ghost" size="icon" aria-label="Notificaciones" onClick={()=>setShowNotifications(v=>!v)}><Bell className="h-4 w-4" /></Button>
              {showNotifications && (
                <>
                  <div className="fixed inset-0 z-[60]" onClick={()=>setShowNotifications(false)} />
                  <div className="absolute right-0 top-full z-[70] mt-2 w-72 rounded-xl border border-[#2C3037] bg-[#1E2127] p-3 shadow-2xl">
                    <div className="text-sm font-semibold">Notificaciones</div>
                    <div className="mt-1 text-xs text-[#9BA1AA]">Sin notificaciones nuevas. El arnés está listo.</div>
                    <div className="mt-3 rounded-lg bg-[#1E2127] p-2 text-xs text-[#9BA1AA]">WAL+FTS activo · sesión {activeSession}</div>
                  </div>
                </>
              )}
            </div>
            <div className="h-8 w-8 rounded-full bg-[#0B62D6] flex items-center justify-center text-white font-semibold text-xs" title="Usuario Nexus">NX</div>
          </div>
        </header>

        {/* content */}
        <main className="flex-1 min-h-0 overflow-hidden bg-[radial-gradient(ellipse_at_top,_rgba(76,157,255,0.06),_transparent_55%)]">
          {view==='chat' && <ChatPanel />}
          {view==='graph' && <GraphView />}
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
          <span className="flex items-center gap-1.5"><span className={`h-2 w-2 rounded-full animate-pulse ${live?.running ? 'bg-[#4C9DFF]' : 'bg-[#3DDC84]'}`} /> {live?.running ? `${live.running} run activo` : 'arnés listo'}{live?.subs ? ` · ${live.subs} con subagentes` : ''}</span>
          <span className="hidden md:inline">·</span>
          <span className="hidden md:inline">config.yaml · nada hardcodeado · distribuible</span>
            <span className="ml-auto hidden md:inline">Alcance: según Ajustes → Permisos (full = toda la PC) · secretos bloqueados</span>
          <span className="ml-auto md:ml-0 flex items-center gap-2"><span className="rounded bg-[#2C3037] px-1.5 py-0.5 font-mono text-[11px]"><User className="h-3 w-3 inline mr-1" />nexus-v3 3.0.0-alpha</span></span>
        </footer>
      </div>
    </div>
  )
}
