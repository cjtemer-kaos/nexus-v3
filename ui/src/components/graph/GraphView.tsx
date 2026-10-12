import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import { Search, Maximize2, RefreshCw, X, Network, Sparkles } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { Badge } from '@/components/ui/badge'
import { apiFetch } from '@/lib/api'

type GNode = {
  id: number; label: string; title: string; category: string; source: string;
  importance: number; degree: number; community: number; pagerank: number;
  size: number; color: string; content?: string;
  x?: number; y?: number; fx?: number | null; fy?: number | null;
}
type GLink = { id: number; source: number | GNode; target: number | GNode; relation: string; weight: number; label: string }
type GData = {
  nodes: GNode[]; edges: GLink[];
  stats: { total_nodes: number; total_edges: number; filtered_edges?: number; visible_nodes?: number; visible_edges?: number; categories?: string[]; relations?: string[]; centrality?: string; error?: string };
  communities: { id: number; size: number; color: string; label: string }[];
}
type GraphEntity = { name: string; mentions: number; nodes: number }
type SearchHit = { id: number; title: string; category: string; snippet: string; topic_key?: string; revision?: number }
type GraphStatus = { connected_nodes: number; entities: number; topic_nodes: number; total_revisions: number }
type NodeDetail = {
  id: number; title: string; category: string; source: string; importance: number;
  tags: string; content: string; access_count: number; degree: number;
  neighbors: { edge_id: number; relation: string; weight: number; other_id: number; other_title: string; other_category: string; direction: string }[];
}

const CATEGORY_COLOR: Record<string, string> = {
  general: '#788493', architecture: '#4C9DFF', concept: '#61C5D9',
  deploy: '#FFC857', fix: '#3DDC84', fact: '#F2A65A', lesson: '#A7B8CC',
}

export function GraphView() {
  const wrapRef = useRef<HTMLDivElement>(null)
  const fgRef = useRef<any>(null)
  const [data, setData] = useState<GData | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [q, setQ] = useState('')
  const [qApplied, setQApplied] = useState('')
  const [category, setCategory] = useState('')
  const [community, setCommunity] = useState('')
  const [relation, setRelation] = useState('')
  const [minWeight, setMinWeight] = useState(0)
  const [centrality, setCentrality] = useState('hybrid')
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<NodeDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [hovered, setHovered] = useState<number | null>(null)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [dims, setDims] = useState({ w: 800, h: 500 })
  const [searchResults, setSearchResults] = useState<SearchHit[]>([])
  const [searchLoading, setSearchLoading] = useState(false)
  const [showEntities, setShowEntities] = useState(false)
  const [entities, setEntities] = useState<GraphEntity[] | null>(null)
  const [entityError, setEntityError] = useState<string | null>(null)
  const [graphStatus, setGraphStatus] = useState<GraphStatus | null>(null)
  const searchSequence = useRef(0)

  const loadSeq = useRef(0)

  const load = useCallback(async (opts?: {
    q?: string; category?: string; community?: string; relation?: string;
    minWeight?: number; centrality?: string
  }) => {
    const seq = ++loadSeq.current
    setLoading(true); setError(null)
    try {
      const params = new URLSearchParams({ limit: '150' })
      const qq = opts?.q ?? qApplied
      const cc = opts?.category ?? category
      const cm = opts?.community ?? community
      const rr = opts?.relation ?? relation
      const mw = opts?.minWeight ?? minWeight
      const ct = opts?.centrality ?? centrality
      if (qq) params.set('q', qq)
      if (cc) params.set('category', cc)
      if (cm) params.set('community', cm)
      if (rr) params.set('relation', rr)
      if (mw > 0) params.set('min_weight', String(mw))
      params.set('centrality', ct)
      const res = await apiFetch(`/api/knowledge/graph?${params.toString()}`)
      if (!res.ok) throw new Error(`graph ${res.status}`)
      const d = (await res.json()) as GData
      if (seq !== loadSeq.current) return // respuesta vieja: descartar (evita pisar la nueva)
      if (d.stats?.error) throw new Error(d.stats.error)
      setData(d)
      setSelected(null); setDetail(null)
    } catch (e) {
      if (seq !== loadSeq.current) return
      setError((e as Error).message ?? String(e))
      setData(null)
    } finally {
      if (seq === loadSeq.current) setLoading(false)
    }
  }, [qApplied, category, community, relation, minWeight, centrality])

  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    let cancelled = false
    apiFetch('/api/graph/status')
      .then(res => res.ok ? res.json() : Promise.reject(new Error(`status ${res.status}`)))
      .then((status: GraphStatus) => { if (!cancelled) setGraphStatus(status) })
      .catch(() => { if (!cancelled) setGraphStatus(null) })
    return () => { cancelled = true }
  }, [])

  useEffect(() => {
    if (!showEntities || entities !== null) return
    let cancelled = false
    apiFetch('/api/graph/entities?limit=12')
      .then(res => res.ok ? res.json() : Promise.reject(new Error(`entidades ${res.status}`)))
      .then(data => { if (!cancelled) setEntities(data.entities ?? []) })
      .catch(e => { if (!cancelled) setEntityError((e as Error).message) })
    return () => { cancelled = true }
  }, [showEntities, entities])

  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const measure = () => setDims({ w: el.clientWidth, h: el.clientHeight })
    measure()
    const obs = new ResizeObserver(measure)
    obs.observe(el)
    return () => obs.disconnect()
  }, [isFullscreen])

  const loadDetail = useCallback(async (id: number) => {
    setSelected(id); setDetail(null); setDetailLoading(true)
    try {
      const res = await apiFetch(`/api/graph/node/${id}`)
      if (!res.ok) throw new Error(`nodo ${res.status}`)
      setDetail(await res.json())
    } catch {
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }, [])

  const graphData = useMemo(() => {
    if (!data) return { nodes: [], links: [] }
    return {
      nodes: data.nodes.map(n => ({ ...n })),
      links: data.edges.map(e => ({ ...e, source: e.source, target: e.target })),
    }
  }, [data])

  const focusId = hovered ?? selected
  const neighborIds = useMemo(() => {
    if (!focusId || !data) return null
    const s = new Set<number>([focusId])
    for (const e of data.edges) {
      const src = typeof e.source === 'object' ? (e.source as GNode).id : e.source
      const tgt = typeof e.target === 'object' ? (e.target as GNode).id : e.target
      if (src === focusId) s.add(tgt)
      if (tgt === focusId) s.add(src)
    }
    return s
  }, [focusId, data])

  const submitSearch = (e?: React.FormEvent) => {
    e?.preventDefault()
    const query = q.trim()
    setQ(query)
    setQApplied(query)
    load({ q: query })
    const requestId = ++searchSequence.current
    if (!query) {
      setSearchResults([])
      setSearchLoading(false)
      return
    }
    setSearchResults([])
    setSearchLoading(true)
    apiFetch(`/api/graph/search?q=${encodeURIComponent(query)}&limit=8`)
      .then(res => res.ok ? res.json() : Promise.reject(new Error(`búsqueda ${res.status}`)))
      .then(data => { if (requestId === searchSequence.current) setSearchResults(data.results ?? []) })
      .catch(() => { if (requestId === searchSequence.current) setSearchResults([]) })
      .finally(() => { if (requestId === searchSequence.current) setSearchLoading(false) })
  }

  const applyEntity = (name: string) => {
    setQ(name)
    setQApplied(name)
    load({ q: name })
    setShowEntities(false)
    const requestId = ++searchSequence.current
    setSearchResults([])
    setSearchLoading(true)
    apiFetch(`/api/graph/search?q=${encodeURIComponent(name)}&limit=8`)
      .then(res => res.ok ? res.json() : Promise.reject(new Error(`búsqueda ${res.status}`)))
      .then(data => { if (requestId === searchSequence.current) setSearchResults(data.results ?? []) })
      .catch(() => { if (requestId === searchSequence.current) setSearchResults([]) })
      .finally(() => { if (requestId === searchSequence.current) setSearchLoading(false) })
  }

  return (
    <div className={isFullscreen ? "fixed inset-0 z-50 flex flex-col bg-[#15171B]" : "flex h-full flex-col"}>
      <div className="flex flex-wrap items-center gap-2 border-b border-[#2C3037] px-4 py-3">
        <form onSubmit={submitSearch} className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-[#9BA1AA]" />
          <input
            value={q} onChange={e => setQ(e.target.value)}
            placeholder="Buscar en el grafo… (título, contenido, tag)"
            aria-label="Buscar en el grafo de conocimiento"
            className="w-full rounded-xl border border-[#2C3037] bg-[#1E2127] pl-9 pr-3 py-2 text-sm placeholder:text-[#9BA1AA] focus:outline-none focus:border-[#4C9DFF]/50"
          />
        </form>
        <select
          value={category} aria-label="Filtrar por categoría"
          onChange={e => { setCategory(e.target.value); load({ category: e.target.value }) }}
          className="rounded-xl border border-[#2C3037] bg-[#1E2127] px-2 py-2 text-xs text-[#EDEEF0] focus:outline-none"
        >
          <option value="">todas las categorías</option>
          {(data?.stats.categories ?? []).map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        {data && (data.communities?.length ?? 0) > 0 && (
          <select
            value={community} aria-label="Filtrar por comunidad"
            onChange={e => { setCommunity(e.target.value); load({ community: e.target.value }) }}
            className="rounded-xl border border-[#2C3037] bg-[#1E2127] px-2 py-2 text-xs text-[#EDEEF0] focus:outline-none max-w-[160px]"
          >
            <option value="">todas las comunidades</option>
            {data.communities.slice(0, 30).map(c => <option key={c.id} value={String(c.id)}>{c.label} ({c.size})</option>)}
          </select>
        )}
        <select
          value={relation} aria-label="Filtrar por tipo de relación"
          onChange={e => { setRelation(e.target.value); load({ relation: e.target.value }) }}
          className="rounded-xl border border-[#2C3037] bg-[#1E2127] px-2 py-2 text-xs text-[#EDEEF0] focus:outline-none max-w-[170px]"
        >
          <option value="">todas las relaciones</option>
          {(data?.stats.relations ?? []).map(r => <option key={r} value={r}>{r.replace(/_/g, ' ')}</option>)}
        </select>
        <select
          value={centrality} aria-label="Ordenar centralidad"
          onChange={e => { setCentrality(e.target.value); load({ centrality: e.target.value }) }}
          className="rounded-xl border border-[#2C3037] bg-[#1E2127] px-2 py-2 text-xs text-[#EDEEF0] focus:outline-none"
        >
          <option value="hybrid">centralidad mixta</option>
          <option value="degree">por conexiones</option>
          <option value="pagerank">por PageRank</option>
        </select>
        <label className="flex items-center gap-2 rounded-xl border border-[#2C3037] bg-[#1E2127] px-2.5 py-2 text-[11px] text-[#9BA1AA]">
          <span>Peso ≥ {minWeight.toFixed(1)}</span>
          <input
            type="range" min="0" max="1" step="0.1" value={minWeight}
            aria-label="Peso mínimo de las relaciones"
            className="w-20 accent-[#4C9DFF]"
            onChange={e => setMinWeight(Number(e.target.value))}
            onPointerUp={e => load({ minWeight: Number(e.currentTarget.value) })}
            onKeyUp={e => load({ minWeight: Number(e.currentTarget.value) })}
          />
        </label>
        <div className="ml-auto flex items-center gap-1.5">
          <Button
            variant="ghost" size="sm" aria-expanded={showEntities}
            aria-label={showEntities ? 'Cerrar panel de entidades' : 'Abrir panel de entidades'}
            onClick={() => { setEntityError(null); setShowEntities(v => !v) }}
            className="gap-1.5 text-xs text-[#9BA1AA] hover:text-[#EDEEF0]"
          >
            <Sparkles className="h-4 w-4" /> Entidades
            {graphStatus && <span className="font-mono text-[10px]">{graphStatus.entities}</span>}
          </Button>
          <Button variant="ghost" size="icon" aria-label="Recargar grafo" onClick={() => load()}><RefreshCw className="h-4 w-4" /></Button>
          <Button variant="ghost" size="icon" aria-label={isFullscreen ? "Salir de pantalla completa" : "Pantalla completa"} onClick={() => setIsFullscreen(v => !v)}><Maximize2 className="h-4 w-4" /></Button>
        </div>
      </div>

      <div ref={wrapRef} className="relative flex-1 overflow-hidden bg-[radial-gradient(ellipse_at_top,_rgba(76,157,255,0.07),_transparent_60%)]">
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center text-sm text-[#9BA1AA]">
            <RefreshCw className="h-5 w-5 animate-spin mr-2" /> Cargando grafo de conocimiento…
          </div>
        )}
        {error && !loading && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 text-sm">
            <div className="text-[#EDEEF0] font-medium">No se pudo cargar el grafo</div>
            <div className="text-xs text-[#9BA1AA] max-w-md text-center break-all">{error}</div>
            <button onClick={() => load()} className="mt-2 rounded-xl bg-[#0B62D6] hover:bg-[#4C9DFF] px-3 py-1.5 text-xs font-medium text-white">Reintentar</button>
          </div>
        )}
        {data && data.nodes.length === 0 && !loading && !error && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 text-sm text-[#9BA1AA]">
            <Network className="h-6 w-6 text-[#59616D]" />
            Sin nodos para este filtro — probá otra búsqueda o quitá filtros.
          </div>
        )}
        {qApplied && (
          <div className="absolute left-4 top-4 z-20 w-[min(22rem,calc(100%-2rem))] rounded-2xl border border-[#2C3037] bg-[#1E2127]/95 p-3 shadow-2xl backdrop-blur">
            <div className="mb-2 flex items-center justify-between text-[10px] font-semibold uppercase tracking-widest text-[#9BA1AA]">
              <span>Recall FTS5 · {searchResults.length} resultados</span>
              <button aria-label="Cerrar resultados de búsqueda" onClick={() => setSearchResults([])} className="rounded p-1 hover:bg-[#2C3037]"><X className="h-3.5 w-3.5" /></button>
            </div>
            {searchLoading && <div className="text-xs text-[#9BA1AA]">Buscando en el grafo…</div>}
            {!searchLoading && searchResults.length === 0 && <div className="text-xs text-[#9BA1AA]">Sin coincidencias FTS5.</div>}
            <div className="max-h-56 space-y-1 overflow-auto">
              {searchResults.map(hit => (
                <button key={hit.id} onClick={() => loadDetail(hit.id)} className="block w-full rounded-lg px-2.5 py-2 text-left hover:bg-[#22262D]">
                  <span className="block truncate text-xs font-medium text-[#EDEEF0]">{hit.title || `Nodo #${hit.id}`}</span>
                  <span className="mt-0.5 block line-clamp-2 text-[10px] leading-relaxed text-[#9BA1AA]">{hit.snippet}</span>
                </button>
              ))}
            </div>
          </div>
        )}
        {data && data.nodes.length > 0 && (
          <ForceGraph2D
            ref={fgRef}
            width={dims.w}
            height={dims.h}
            graphData={graphData}
            nodeLabel={(n: any) => `${n.label}`}
            nodeColor={(n: any) => n.color ?? CATEGORY_COLOR[n.category] ?? '#4C9DFF'}
            nodeVal={(n: any) => Math.max(2, n.size ?? 4)}
            nodeRelSize={4}
            linkColor={() => 'rgba(139,147,167,0.25)'}
            linkWidth={0.6}
            linkDirectionalParticles={2}
            linkDirectionalParticleWidth={1.5}
            cooldownTicks={120}
            d3AlphaDecay={0.02}
            d3VelocityDecay={0.3}
            onNodeClick={(n: any) => loadDetail(n.id)}
            onNodeHover={(n: any) => setHovered(n ? n.id : null)}
            onBackgroundClick={() => { setSelected(null); setDetail(null) }}
            nodeCanvasObjectMode={() => 'after'}
            nodeCanvasObject={(n: any, ctx: CanvasRenderingContext2D, globalScale: number) => {
              if (focusId === null || (neighborIds !== null && !neighborIds.has(n.id))) return
              const label = String(n.label ?? n.id)
              const fontSize = Math.max(11 / globalScale, 3)
              ctx.font = `${fontSize}px Sans-Serif`
              ctx.textAlign = 'center'
              ctx.textBaseline = 'top'
              ctx.fillStyle = 'rgba(255,255,255,0.85)'
              ctx.fillText(label.length > 40 ? label.slice(0, 38) + '…' : label, n.x, n.y + 8 / globalScale + 4)
            }}
            enableNodeDrag
            enableZoomInteraction
            enablePanInteraction
          />
        )}

        {(selected !== null) && (
          <div className="absolute bottom-4 left-4 z-10 rounded-2xl border border-[#2C3037] bg-[#1E2127]/95 backdrop-blur p-4 shadow-2xl max-w-sm max-h-[60%] overflow-auto">
            <div className="flex items-start justify-between gap-2">
              <div className="text-sm font-semibold">Nodo #{selected}</div>
              <button aria-label="Cerrar detalle" onClick={() => { setSelected(null); setDetail(null) }} className="rounded p-1 hover:bg-[#2C3037] text-[#9BA1AA]"><X className="h-4 w-4" /></button>
            </div>
            {detailLoading && <div className="mt-2 text-xs text-[#9BA1AA]">Cargando detalle…</div>}
            {detail && (
              <div className="mt-2 space-y-2 text-xs">
                <div className="font-medium text-[#EDEEF0] leading-snug">{detail.title?.split('\n')[0]?.slice(0, 120)}</div>
                <div className="flex flex-wrap gap-1">
                  <Badge variant="outline">{detail.category}</Badge>
                  <Badge variant="outline">importancia {detail.importance}</Badge>
                  <Badge variant="outline">{detail.degree} conexiones</Badge>
                </div>
                <Button variant="outline" size="sm" onClick={async () => {
                  try {
                    const r = await apiFetch(`/api/graph/node/${detail.id}/cite`)
                    const j = await r.json()
                    await navigator.clipboard.writeText(j.citation || `[n${detail.id}] ${detail.title}`)
                  } catch { /* clipboard no disponible */ }
                }}>Copiar cita</Button>
                {detail.content && <div className="text-[#9BA1AA] leading-relaxed whitespace-pre-wrap max-h-40 overflow-auto">{detail.content.slice(0, 600)}{detail.content.length > 600 ? '…' : ''}</div>}
                {(detail.neighbors?.length ?? 0) > 0 && (
                  <div>
                    <div className="text-[#9BA1AA] tracking-widest text-[11px] mb-1">VECINOS ({detail.neighbors?.length ?? 0})</div>
                    <div className="space-y-1">
                      {(detail.neighbors ?? []).slice(0, 12).map(nb => (
                        <button key={nb.edge_id} onClick={() => loadDetail(nb.other_id)} className="block w-full text-left rounded-lg bg-[#1E2127] hover:bg-[#22262D] px-2 py-1.5">
                          <span className="text-[#8CC1FF] font-mono text-[11px]">{nb.relation}</span>{' '}
                          <span className="text-[#EDEEF0]">{(nb.other_title ?? '').slice(0, 60)}</span>
                        </button>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        {data && (
          <div className="pointer-events-none absolute right-4 top-4 rounded-xl border border-[#2C3037] bg-[#1E2127]/70 backdrop-blur px-3 py-2 text-xs text-[#9BA1AA]">
            {data.stats.visible_nodes ?? data.nodes.length} nodos · {data.stats.visible_edges ?? data.edges.length} aristas · {graphStatus?.connected_nodes ?? data.stats.total_nodes} conectados · {graphStatus?.entities ?? 0} entidades
          </div>
        )}
        {showEntities && (
          <div className="absolute bottom-4 right-4 z-20 w-[min(20rem,calc(100%-2rem))] rounded-2xl border border-[#2C3037] bg-[#1E2127]/95 p-4 shadow-2xl backdrop-blur">
            <div className="mb-3 flex items-center justify-between">
              <div>
                <div className="text-xs font-semibold text-[#EDEEF0]">Entidades del grafo</div>
                <div className="mt-0.5 text-[10px] text-[#9BA1AA]">Selecciona una para buscar sus conexiones</div>
              </div>
              <button aria-label="Cerrar entidades" onClick={() => setShowEntities(false)} className="rounded p-1 text-[#9BA1AA] hover:bg-[#2C3037]"><X className="h-4 w-4" /></button>
            </div>
            {entityError && <div className="text-xs text-[#FF8B84]">No se pudieron cargar: {entityError}</div>}
            {!entities && !entityError && <div className="text-xs text-[#9BA1AA]">Cargando entidades…</div>}
            {entities && entities.length === 0 && <div className="text-xs text-[#9BA1AA]">Aún no hay entidades indexadas.</div>}
            <div className="max-h-64 space-y-1 overflow-auto">
              {entities?.map(entity => (
                <button key={entity.name} onClick={() => applyEntity(entity.name)} className="flex w-full items-center justify-between gap-3 rounded-lg px-2.5 py-2 text-left hover:bg-[#22262D]">
                  <span className="truncate text-xs text-[#8CC1FF]">{entity.name}</span>
                  <span className="shrink-0 font-mono text-[10px] text-[#9BA1AA]">{entity.nodes} nodos</span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
