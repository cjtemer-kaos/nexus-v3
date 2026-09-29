import { Sidebar } from '@/components/Sidebar'
import { ModelSelector } from '@/components/ModelSelector'
import { ChatPanel } from '@/components/chat/ChatPanel'
import { GraphView } from '@/components/graph/GraphView'
import { FileEditor } from '@/components/editor/FileEditor'
import { TerminalPanel } from '@/components/terminal/TerminalPanel'
import { useAppStore } from '@/store/app-store'
import { Search, Command, Bell, User } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useState, useEffect } from 'react'

const viewLabels: Record<string, string> = {
  chat: 'Chat',
  graph: 'Grafo',
  editor: 'Editor',
  terminal: 'Terminal',
}

export default function App() {
  const { view, setView, activeSession, refreshProviders } = useAppStore()
  const [search, setSearch] = useState('')
  const [showNotifications, setShowNotifications] = useState(false)

  useEffect(() => {
    // Kick off dynamic provider detection at app start
    refreshProviders()
  }, [refreshProviders])

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
    <div className="flex h-screen overflow-hidden bg-zinc-950 text-zinc-100">
      <Sidebar />

      <div className="flex flex-1 flex-col min-w-0">
        {/* topbar — z-30 ensures dropdowns inside header paint above main content (backdrop-blur creates stacking context) */}
        <header className="relative z-30 flex h-14 shrink-0 items-center gap-3 border-b border-white/10 bg-zinc-950/80 backdrop-blur px-3 sm:px-4">
          {/* mobile nav */}
          <div className="flex md:hidden items-center gap-1">
            {(Object.keys(viewLabels) as Array<keyof typeof viewLabels>).map(v=>(
              <button key={v} onClick={()=>setView(v as any)} aria-label={`Ir a ${viewLabels[v]}`} className={`rounded-lg px-2.5 py-1.5 text-xs font-medium ${view===v ? 'bg-violet-600 text-white' : 'text-zinc-500'}`}>{viewLabels[v]}</button>
            ))}
          </div>

          <div className="hidden md:flex items-center gap-2 text-sm">
            <span className="text-zinc-500">Arnés</span>
            <span className="text-zinc-700">/</span>
            <span className="font-medium">{viewLabels[view]}</span>
            <span className="ml-2 hidden lg:inline rounded-full bg-emerald-500/15 border border-emerald-500/20 px-2 py-0.5 text-xs text-emerald-400">WAL activado · FTS5 · sesión {activeSession}</span>
          </div>

          <div className="hidden sm:flex flex-1 justify-center">
            <div className="relative w-full max-w-md">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500" />
              <input id="global-search" value={search} onChange={e=>setSearch(e.target.value)} placeholder="Buscar mensajes, archivos, grafo…  (⌘K)" aria-label="Buscar en Nexus" className="w-full rounded-xl border border-white/10 bg-white/[0.04] pl-9 pr-16 py-2 text-sm placeholder:text-zinc-500 focus:outline-none focus:border-violet-500/50" />
              <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 hidden sm:flex items-center gap-1 rounded-md border border-white/10 bg-white/[0.06] px-1.5 py-0.5 text-xs text-zinc-500"><Command className="h-3 w-3" />K</span>
            </div>
          </div>

          <div className="ml-auto flex items-center gap-2">
            <ModelSelector />
            <div className="relative hidden sm:flex">
              <Button variant="ghost" size="icon" aria-label="Notificaciones" onClick={()=>setShowNotifications(v=>!v)}><Bell className="h-4 w-4" /></Button>
              {showNotifications && (
                <>
                  <div className="fixed inset-0 z-[60]" onClick={()=>setShowNotifications(false)} />
                  <div className="absolute right-0 top-full z-[70] mt-2 w-72 rounded-xl border border-white/10 bg-zinc-900 p-3 shadow-2xl">
                    <div className="text-sm font-semibold">Notificaciones</div>
                    <div className="mt-1 text-xs text-zinc-400">Sin notificaciones nuevas. El arnés está listo.</div>
                    <div className="mt-3 rounded-lg bg-white/[0.04] p-2 text-xs text-zinc-500">WAL+FTS activo · sesión {activeSession}</div>
                  </div>
                </>
              )}
            </div>
            <div className="h-8 w-8 rounded-full bg-gradient-to-br from-violet-600 to-cyan-500 flex items-center justify-center text-white font-semibold text-xs" title="Usuario Nexus">NX</div>
          </div>
        </header>

        {/* content */}
        <main className="flex-1 overflow-hidden bg-[radial-gradient(ellipse_at_top,_rgba(139,92,246,0.07),_transparent_50%),radial-gradient(ellipse_at_bottom,_rgba(6,182,214,0.05),_transparent_60%)]">
          {view==='chat' && <ChatPanel />}
          {view==='graph' && <GraphView />}
          {view==='editor' && <FileEditor />}
          {view==='terminal' && <TerminalPanel />}
        </main>

        {/* estado inferior */}
        <footer className="hidden sm:flex h-6 items-center gap-3 border-t border-white/10 bg-zinc-900 px-3 text-xs text-zinc-500">
          <span className="flex items-center gap-1.5"><span className="h-2 w-2 rounded-full bg-emerald-500 animate-pulse" /> arnés listo</span>
          <span className="hidden md:inline">·</span>
          <span className="hidden md:inline">config.yaml · nada hardcodeado · distribuible</span>
          <span className="ml-auto hidden md:inline">Alcance: raíces permitidas ./workspace, ./data · denegado **/.env</span>
          <span className="ml-auto md:ml-0 flex items-center gap-2"><span className="rounded bg-white/10 px-1.5 py-0.5 font-mono text-[11px]"><User className="h-3 w-3 inline mr-1" />nexus-v3 3.0.0-alpha</span></span>
        </footer>
      </div>
    </div>
  )
}
