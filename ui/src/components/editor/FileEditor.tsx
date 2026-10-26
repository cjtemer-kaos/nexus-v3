import { useEffect, useRef, useState } from 'react'
import Editor from '@monaco-editor/react'
import { File, Plus, X, Save, Search, Folder, RefreshCw, ArrowUp, HardDrive } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { useAppStore } from '@/store/app-store'
import { cn } from '@/lib/utils'
import { fsList, fsRead, fsWrite } from '@/lib/api'

type FsEntry = { name: string; path: string; type: 'file'|'dir'; size: number }

function Tree({ entries, childrenMap, expanded, toggle, onOpenFile, depth=0 }: {
  entries: FsEntry[]; childrenMap: Record<string, FsEntry[]>; expanded: Set<string>;
  toggle:(p:string)=>void; onOpenFile:(p:string)=>void; depth?: number
}) {
  return (
    <div>
      {entries.map((it)=> {
        const isDir = it.type==='dir'
        const isOpen = expanded.has(it.path)
        return (
          <div key={it.path}>
            <div style={{ paddingLeft: depth*12 }} className="flex items-center gap-1.5 rounded-md px-2 py-1 text-xs hover:bg-[#22262D] cursor-pointer text-[#EDEEF0]" onClick={()=> isDir ? toggle(it.path) : onOpenFile(it.path)}>
              {isDir ? (
                <>
                  <span className="text-[10px]">{isOpen ? '▾' : '▸'}</span>
                  <Folder className="h-3.5 w-3.5 text-[#4C9DFF]" />
                  <span className="font-medium text-[#EDEEF0]">{it.name}/</span>
                </>
              ) : (
                <>
                  <File className="h-3.5 w-3.5 text-[#9BA1AA]" />
                  <span>{it.name}</span>
                </>
              )}
            </div>
            {isDir && isOpen && (
              <Tree entries={childrenMap[it.path] ?? []} childrenMap={childrenMap} expanded={expanded} toggle={toggle} onOpenFile={onOpenFile} depth={depth+1} />
            )}
          </div>
        )
      })}
    </div>
  )
}

export function FileEditor() {
  const { tabs, activeTab, setActiveTab, updateTab, openFile, closeTab, saveTab } = useAppStore()
  const [find, setFind] = useState('')
  const [findCount, setFindCount] = useState(0)
  const editorRef = useRef<any>(null)
  const findIdx = useRef(0)
  const [toast, setToast] = useState<string | null>(null)
  const [root, setRoot] = useState('.')
  const [rootEntries, setRootEntries] = useState<FsEntry[]>([])
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [childrenMap, setChildrenMap] = useState<Record<string, FsEntry[]>>({})
  const [loading, setLoading] = useState(false)
  const [pathInput, setPathInput] = useState('.')
  const showToast = (m: string) => { setToast(m); setTimeout(()=>setToast(null), 2200) }

  const loadRoot = async (r: string = root) => {
    setLoading(true)
    try{
      const res = await fsList(r)
      setRootEntries(res.entries)
      setRoot(r); setPathInput(r)
      setExpanded(new Set()); setChildrenMap({})
    } catch(e){ showToast(String(e).slice(0,160)) }
    finally{ setLoading(false) }
  }
  useEffect(()=>{ loadRoot('.') }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const toggleDir = async (path: string) => {
    if(expanded.has(path)){ setExpanded(prev=>{ const n=new Set(prev); n.delete(path); return n }); return }
    setExpanded(prev=> new Set(prev).add(path))
    if(childrenMap[path]) return
    try{
      const r = await fsList(path)
      setChildrenMap(m=>({...m, [path]: r.entries}))
    } catch(e){ showToast(String(e).slice(0,160)) }
  }

  // Subir un nivel. En la raiz de una unidad -> lista de unidades ("").
  const goUp = () => {
    if (!root || root === '/' || root === '\\') { loadRoot(''); return }
    const norm = root.replace(/[\\/]+$/, '')
    const parts = norm.split(/[\\/]/)
    if (parts.length <= 1) { loadRoot(''); return }
    parts.pop()
    let parent = parts.join('/')
    if (/^[A-Za-z]:$/.test(parent)) parent += '/'
    loadRoot(parent || '')
  }

  const handleOpenFile = async (path: string) => {
    try{
      const r = await fsRead(path)
      openFile(path, r.content)
    } catch(e){
      showToast(`No se pudo leer ${path}: ${String(e).slice(0,140)}`)
    }
  }

  const handleSave = async () => {
    const tab = tabs[activeTab]
    if(!tab) return
    const path = tab.path
    try{
      await fsWrite(path, tab.content)
      const idx = useAppStore.getState().tabs.findIndex(t => t.path === path)
      if (idx >= 0) saveTab(idx)
      showToast(`Guardado: ${path}`)
    } catch(e){
      showToast(`Error guardando: ${String(e).slice(0,180)}`)
    }
  }

  const tab = tabs[activeTab]

  const explorer = (
    <div className="w-[300px] shrink-0 border-r border-[#2C3037] bg-[#15171B]/60 flex flex-col">
      <div className="flex items-center justify-between px-3 py-2.5 border-b border-[#2C3037]">
        <span className="text-xs font-semibold tracking-widest text-[#9BA1AA]">EXPLORADOR</span>
        <div className="flex gap-1">
          <Button variant="ghost" size="xs" onClick={()=>openFile(`nuevo-${tabs.length+1}.py`, '# nuevo\n')} aria-label="Nuevo archivo"><Plus className="h-3 w-3" /></Button>
          <Button variant="ghost" size="xs" onClick={()=>loadRoot()} aria-label="Recargar"><RefreshCw className="h-3 w-3" /></Button>
        </div>
      </div>
      {/* barra de ruta + navegacion por toda la PC */}
      <div className="flex items-center gap-1 border-b border-[#2C3037] px-2 py-1.5">
        <Button variant="ghost" size="xs" onClick={goUp} aria-label="Subir un nivel" title="Subir"><ArrowUp className="h-3.5 w-3.5" /></Button>
        <input
          value={pathInput}
          onChange={e=>setPathInput(e.target.value)}
          onKeyDown={e=>{ if(e.key==='Enter') loadRoot(pathInput.trim()) }}
          aria-label="Ruta a explorar"
          placeholder="ruta o unidad (ej. C:\ o /home)"
          className="min-w-0 flex-1 rounded-md border border-[#2C3037] bg-[#1E2127] px-2 py-1 font-mono text-[11px] text-[#EDEEF0] placeholder:text-zinc-600 focus:outline-none"
        />
        <Button variant="ghost" size="xs" onClick={()=>loadRoot('')} aria-label="Unidades del disco" title="Unidades"><HardDrive className="h-3.5 w-3.5" /></Button>
        <Button variant="ghost" size="xs" onClick={()=>loadRoot('.')} title="Proyecto">.</Button>
      </div>
      <div className="flex-1 overflow-auto p-2">
        {loading ? <div className="text-xs text-[#9BA1AA] px-2">cargando…</div> : (
          <>
            <Tree entries={rootEntries} childrenMap={childrenMap} expanded={expanded} toggle={toggleDir} onOpenFile={handleOpenFile} />
            {rootEntries.length===0 && <div className="text-xs text-zinc-600 px-2 py-4">Carpeta vacía.</div>}
          </>
        )}
      </div>
      <div className="border-t border-[#2C3037] p-2">
        <div className="rounded-lg bg-amber-500/10 border border-amber-500/20 px-2.5 py-2 text-xs leading-relaxed text-amber-200">
          <span className="font-semibold">Guardia de alcance</span><br />
          Acceso según Ajustes → Permisos (<span className="font-mono">full</span> = toda la PC). Secretos <span className="font-mono">**/.env</span> y <span className="font-mono">**/*.key</span> siempre bloqueados.
        </div>
      </div>
    </div>
  )

  // empty state — muestra explorador real + CTA
  if (!tab) {
    return (
      <div className="flex h-full">
        {explorer}
        <div className="flex flex-1 flex-col items-center justify-center gap-3 text-center p-6">
          <File className="h-8 w-8 text-zinc-700" />
          <div className="text-sm font-medium text-[#EDEEF0]">Sin archivos abiertos</div>
          <div className="text-xs text-[#9BA1AA] max-w-xs">Explorá cualquier carpeta de la PC con la barra de ruta, o crea un archivo nuevo.</div>
          <Button variant="ghost" size="sm" onClick={() => openFile(`nuevo-${tabs.length + 1}.py`, '# nuevo archivo\n')}><Plus className="h-3.5 w-3.5 mr-1" /> Nuevo archivo</Button>
          {toast && <div className="text-xs text-amber-400">{toast}</div>}
        </div>
      </div>
    )
  }

  const countMatches = (q: string) => {
    const model = editorRef.current?.getModel()
    setFindCount(model && q.trim() ? model.findMatches(q, false, false, false, null, false).length : 0)
  }

  const findNext = () => {
    const ed = editorRef.current
    const q = find.trim()
    if (!ed || !q) return
    const model = ed.getModel()
    const matches = model ? model.findMatches(q, false, false, false, null, false) : []
    setFindCount(matches.length)
    if (!matches.length) return
    const i = findIdx.current % matches.length
    findIdx.current = i + 1
    ed.setSelection(matches[i].range)
    ed.revealRangeInCenter(matches[i].range)
    ed.focus()
  }

  return (
    <div className="flex h-full">
      {explorer}

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
              <input value={find} onChange={e=>{ setFind(e.target.value); findIdx.current = 0; countMatches(e.target.value) }} onKeyDown={e=>{ if(e.key==='Enter'){ e.preventDefault(); findNext() } }} placeholder="Buscar (Enter)" className="h-7 w-32 rounded-md border border-[#2C3037] bg-[#1E2127] pl-7 pr-7 text-xs placeholder:text-zinc-600 focus:outline-none" />
              {find && <span className="absolute right-1.5 top-1/2 -translate-y-1/2 text-[10px] text-[#9BA1AA]">{findCount}</span>}
            </div>
          </div>
        </div>

        <div className="flex-1 bg-[#0a0a0f]">
          <Editor
            height="100%"
            path={tab.path}
            language={tab.language}
            value={tab.content}
            onChange={(v)=> updateTab(activeTab, v ?? '')}
            onMount={(ed)=> { editorRef.current = ed }}
            theme="vs-dark"
            options={{
              fontFamily: 'Consolas, "Courier New", monospace',
              fontLigatures: false,
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
            <span className="hidden sm:inline opacity-80">Alcance según Permisos · 1.0 MB límite</span>
          </div>
        </div>
        {toast && <div className="absolute bottom-10 right-4 z-10 rounded-lg bg-[#1E2127] border border-[#2C3037] px-3 py-2 text-xs text-white shadow-xl">{toast}</div>}
      </div>
    </div>
  )
}
