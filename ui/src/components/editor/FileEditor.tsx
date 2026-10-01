import { useEffect, useState } from 'react'
import Editor from '@monaco-editor/react'
import { File, Plus, X, Save, Search, SplitSquareHorizontal, MoreHorizontal, Folder, RefreshCw } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { fsList, fsRead, fsWrite } from '@/lib/api'

type FsEntry = { name: string; path: string; type: 'file'|'dir'; size: number }

function Tree({ entries, onOpenDir, onOpenFile, depth=0, expanded, toggle }: {
  entries: FsEntry[]; onOpenDir:(p:string)=>void; onOpenFile:(p:string)=>void;
  depth?: number; expanded: Set<string>; toggle:(p:string)=>void
}) {
  return (
    <div>
      {entries.map((it)=> (
        <div key={it.path} style={{ paddingLeft: depth*12 }}>
          {it.type==='dir' ? (
            <div className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs hover:bg-[#22262D] cursor-pointer text-[#EDEEF0]" onClick={()=>{ toggle(it.path); onOpenDir(it.path)}}>
              <span className="text-[10px]">{expanded.has(it.path) ? '▾' : '▸'}</span>
              <Folder className="h-3.5 w-3.5 text-[#4C9DFF]" />
              <span className="font-medium text-[#EDEEF0]">{it.name}/</span>
            </div>
          ) : (
            <div className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs hover:bg-[#22262D] cursor-pointer text-[#EDEEF0]" onClick={()=> onOpenFile(it.path)}>
              <File className="h-3.5 w-3.5 text-[#9BA1AA]" />
              <span>{it.name}</span>
            </div>
          )}
        </div>
      ))}
    </div>
  )
}

export function FileEditor() {
  const { tabs, activeTab, setActiveTab, updateTab, openFile, closeTab, saveTab } = useAppStore()
  const [find, setFind] = useState('')
  const [toast, setToast] = useState<string | null>(null)
  const [rootEntries, setRootEntries] = useState<FsEntry[]>([])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [childrenMap, setChildrenMap] = useState<Record<string, FsEntry[]>>({})
  const [loading, setLoading] = useState(false)
  const showToast = (m: string) => { setToast(m); setTimeout(()=>setToast(null), 2200) }

  const loadRoot = async () => {
    setLoading(true)
    try{
      const r = await fsList('./workspace')
      setRootEntries(r.entries)
    } catch(e){ showToast(String(e).slice(0,120)) }
    finally{ setLoading(false)}
  }
  useEffect(()=>{ loadRoot() }, [])

  const toggleDir = async (path: string) => {
    const next = new Set(expanded)
    if(next.has(path)){ next.delete(path); setExpanded(next); return }
    next.add(path); setExpanded(next)
    if(childrenMap[path]) return
    try{
      const r = await fsList(path)
      setChildrenMap(m=>({...m, [path]: r.entries}))
    } catch(e){ showToast(String(e).slice(0,120)) }
  }

  const handleOpenFile = async (path: string) => {
    try{
      const r = await fsRead(path)
      const lang = path.endsWith('.py') ? 'python' : path.endsWith('.yaml')||path.endsWith('.yml') ? 'yaml' : path.endsWith('.json') ? 'json' : 'typescript'
      openFile(path, r.content)
      // override language for opened tab
      const idx = useAppStore.getState().tabs.findIndex(t=>t.path===path)
      // openFile appends; ensure language is correct via direct update if needed — store handles it via extension
    } catch(e){
      showToast(`No se pudo leer ${path}: ${String(e).slice(0,120)}`)
    }
  }

  const handleSave = async () => {
    const tab = tabs[activeTab]
    if(!tab) return
    try{
      await fsWrite(tab.path, tab.content)
      saveTab(activeTab)
      showToast(`Guardado: ${tab.path}`)
      // refrescar listing si es workspace
      loadRoot()
    } catch(e){
      showToast(`Error guardando: ${String(e).slice(0,180)}`)
    }
  }

  const tab = tabs[activeTab]

  // empty state — muestra explorador real + CTA
  if (!tab) {
    return (
      <div className="flex h-full">
        <div className="w-[260px] shrink-0 border-r border-[#2C3037] bg-[#15171B]/60 flex flex-col">
          <div className="flex items-center justify-between px-3 py-2.5 border-b border-[#2C3037]">
            <span className="text-xs font-semibold tracking-widest text-[#9BA1AA]">EXPLORADOR — ./workspace</span>
            <Button variant="ghost" size="xs" onClick={loadRoot} aria-label="Recargar"><RefreshCw className="h-3 w-3" /></Button>
          </div>
          <div className="flex-1 overflow-auto p-2">
            {loading ? <div className="text-xs text-[#9BA1AA] px-2">cargando…</div> : (
              <>
                <Tree entries={rootEntries} onOpenDir={toggleDir} onOpenFile={handleOpenFile} expanded={expanded} toggle={toggleDir} />
                {Object.entries(childrenMap).map(([dir, kids])=> expanded.has(dir) && (
                  <div key={dir} className="ml-2 border-l border-[#2C3037] pl-2">
                    <Tree entries={kids} onOpenDir={toggleDir} onOpenFile={handleOpenFile} depth={1} expanded={expanded} toggle={toggleDir} />
                  </div>
                ))}
                {rootEntries.length===0 && <div className="text-xs text-zinc-600 px-2 py-4">Carpeta vacía — crea un archivo.</div>}
              </>
            )}
          </div>
          <div className="border-t border-[#2C3037] p-2">
            <div className="rounded-lg bg-amber-500/10 border border-amber-500/20 px-2.5 py-2 text-xs leading-relaxed text-amber-200">
              <span className="font-semibold">Guardia de alcance</span><br /> FS jail: <span className="font-mono">./workspace</span>, <span className="font-mono">./data</span> · <span className="font-mono">**/.env</span> bloqueado.
            </div>
          </div>
        </div>
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-center p-6">
          <File className="h-8 w-8 text-zinc-700" />
          <div className="text-sm font-medium text-[#EDEEF0]">Sin archivos abiertos</div>
          <div className="text-xs text-[#9BA1AA] max-w-xs">Elegí un archivo del explorador (datos reales vía <span className="font-mono">GET /api/fs/*</span>) o crea uno nuevo.</div>
          <Button variant="ghost" size="sm" onClick={() => openFile(`workspace/nuevo-${tabs.length + 1}.py`, '# nuevo archivo\n')}><Plus className="h-3.5 w-3.5 mr-1" /> Nuevo archivo</Button>
          {toast && <div className="text-xs text-amber-400">{toast}</div>}
        </div>
      </div>
    )
  }

  return (
    <div className="flex h-full">
      <div className="w-[260px] shrink-0 border-r border-[#2C3037] bg-[#15171B]/60 flex flex-col">
        <div className="flex items-center justify-between px-3 py-2.5 border-b border-[#2C3037]">
          <span className="text-xs font-semibold tracking-widest text-[#9BA1AA]">EXPLORADOR</span>
          <div className="flex gap-1">
            <Button variant="ghost" size="xs" onClick={()=>openFile(`workspace/new-${tabs.length+1}.py`, '# nuevo\n')}><Plus className="h-3 w-3" /></Button>
            <Button variant="ghost" size="xs" onClick={loadRoot}><RefreshCw className="h-3 w-3" /></Button>
            <Button variant="ghost" size="xs"><MoreHorizontal className="h-3 w-3" /></Button>
          </div>
        </div>
        <div className="flex-1 overflow-auto p-2">
          <Tree entries={rootEntries} onOpenDir={toggleDir} onOpenFile={handleOpenFile} expanded={expanded} toggle={toggleDir} />
          {Object.entries(childrenMap).map(([dir, kids])=> expanded.has(dir) && (
            <div key={dir} className="ml-2 border-l border-[#2C3037] pl-2">
              <Tree entries={kids} onOpenDir={toggleDir} onOpenFile={handleOpenFile} depth={1} expanded={expanded} toggle={toggleDir} />
            </div>
          ))}
        </div>
        <div className="border-t border-[#2C3037] p-2">
          <div className="rounded-lg bg-amber-500/10 border border-amber-500/20 px-2.5 py-2 text-xs leading-relaxed text-amber-200">
            <span className="font-semibold">Guardia de alcance</span><br /> Jaula FS: solo <span className="font-mono">./workspace</span>, <span className="font-mono">./data</span> permitidos.
          </div>
        </div>
      </div>

      <div className="relative flex flex-1 flex-col min-w-0">
        <div className="flex items-center gap-0 border-b border-[#2C3037] bg-[#1E2127]/60 overflow-x-auto">
          {tabs.map((t,i)=> (
            <button
              key={t.path}
              onClick={()=> setActiveTab(i)}
              className={cn('flex items-center gap-2 border-r border-[#2C3037] px-3 py-2.5 text-xs whitespace-nowrap', i===activeTab ? 'bg-[#1E2127] text-white' : 'bg-[#15171B]/60 text-[#9BA1AA] hover:text-[#EDEEF0]')}
            >
              <File className="h-3.5 w-3.5" /> {t.path.split('/').pop()} {t.dirty && <span className="h-1.5 w-1.5 rounded-full bg-amber-400" />}
              <X className="h-3 w-3 opacity-60 hover:opacity-100 ml-1" onClick={(e)=>{e.stopPropagation(); closeTab(i)}} aria-label={`Cerrar ${t.path}`} />
            </button>
          ))}
          <div className="ml-auto flex items-center gap-1 px-2">
            <div className="relative hidden sm:block">
              <Search className="absolute left-2 top-1/2 -translate-y-1/2 h-3.5 w-3.5 text-[#9BA1AA]" />
              <input value={find} onChange={e=>setFind(e.target.value)} placeholder="Buscar" className="h-7 w-28 rounded-md border border-[#2C3037] bg-[#1E2127] pl-7 pr-2 text-xs placeholder:text-zinc-600 focus:outline-none" />
            </div>
            <Button variant="ghost" size="xs"><SplitSquareHorizontal className="h-3.5 w-3.5" /></Button>
          </div>
        </div>

        <div className="flex-1 bg-[#0a0a0f]">
          <Editor
            height="100%"
            language={tab.language}
            value={tab.content}
            onChange={(v)=> updateTab(activeTab, v ?? '')}
            theme="vs-dark"
            options={{
              fontFamily: 'Geist Mono, JetBrains Mono, monospace',
              fontSize: 13,
              lineHeight: 20,
              minimap: { enabled: false },
              scrollBeyondLastLine: false,
              wordWrap: 'on',
              padding: { top: 12, bottom: 12 },
              smoothScrolling: true,
            }}
          />
        </div>

        <div className="flex items-center justify-between border-t border-[#2C3037] bg-[#0B62D6] px-3 py-1.5 text-xs text-white">
          <div className="flex items-center gap-3">
            <span className="font-mono">{tab.path}</span>
            <span className="opacity-80">{tab.language}</span>
            {tab.dirty && <span className="rounded bg-[#3A4049] px-1.5 py-0.5">modificado</span>}
          </div>
          <div className="flex items-center gap-2">
            <Button variant="ghost" size="xs" onClick={handleSave} className="h-6 text-white hover:bg-[#3A4049] border border-[#3A4049]"><Save className="h-3 w-3" /> Guardar (→ PUT /api/fs/write)</Button>
            <span className="hidden sm:inline opacity-80">Alcance: jail activo · 1.0 MB límite</span>
          </div>
        </div>
        {toast && <div className="absolute bottom-10 right-4 z-10 rounded-lg bg-[#1E2127] border border-[#2C3037] px-3 py-2 text-xs text-white shadow-xl">{toast}</div>}
      </div>
    </div>
  )
}
