import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ForceGraph2D from 'react-force-graph-2d'
import { Search, Maximize2, RefreshCw, X, Network } from 'lucide-react'
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
  stats: { total_nodes: number; total_edges: number; visible_nodes?: number; visible_edges?: number; categories?: string[]; error?: string };
  communities: { id: number; size: number; color: string; label: string }[];
}
type NodeDetail = {
  id: number; title: string; category: string; source: string; importance: number;
  tags: string; content: string; access_count: number; degree: number;
  neighbors: { edge_id: number; relation: string; weight: number; other_id: number; other_title: string; other_category: string; direction: string }[];
}

const CATEGORY_COLOR: Record<string, string> = {
  general: '#6b7280', architecture: '#8b5cf6', concept: '#06b6d4',
  deploy: '#f59e0b', fix: '#10b981', fact: '#ec4899', lesson: '#84cc16',
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
  const [selected, setSelected] = useState<number | null>(null)
  const [detail, setDetail] = useState<NodeDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [hovered, setHovered] = useState<number | null>(null)
  const [isFullscreen, setIsFullscreen] = useState(false)
  const [dims, setDims] = useState({ w: 800, h: 500 })

  const load = useCallback(async (opts?: { q?: string; category?: string; community?: string }) => {
    setLoading(true); setError(null)
    try {
      const params = new URLSearchParams({ limit: '150' })
      const qq = opts?.q ?? qApplied
      const cc = opts?.category ?? category
      const cm = opts?.community ?? community
      if (qq) params.set('q', qq)
      if (cc) params.set('category', cc)
      if (cm) params.set('community', cm)
      const res = await apiFetch(`/api/graph?${params.toString()}`)
      if (!res.ok) throw new Error(`graph ${res.status}`)
      const d = (await res.json()) as GData
      if (d.stats?.error) throw new Error(d.stats.error)
      setData(d)
      setSelected(null); setDetail(null)
    } catch (e) {
      setError((e as Error).message ?? String(e))
      setData(null)
    } finally {
      setLoading(false)
    }
  }, [qApplied, category, community])

  useEffect(() => { load() }, []) // eslint-disable-line react-hooks/exhaustive-deps

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
    } catch (e) {
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
    setQApplied(q)
    load({ q })
  }

  return (
    <div className={isFullscreen ? "fixed inset-0 z-50 flex flex-col bg-zinc-950" : "flex h-full flex-col"}>
      <div className="flex flex-wrap items-center gap-2 border-b border-white/10 px-4 py-3">
        <form onSubmit={submitSearch} className="relative flex-1 min-w-[200px] max-w-sm">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 h-4 w-4 text-zinc-500" />
          <input
            value={q} onChange={e => setQ(e.target.value)}
            placeholder="Buscar en el grafo… (título, contenido, tag)"
            aria-label="Buscar en el grafo de conocimiento"
            className="w-full rounded-xl border border-white/10 bg-white/[0.04] pl-9 pr-3 py-2 text-sm placeholder:text-zinc-500 focus:outline-none focus:border-violet-500/50"
          />
        </form>
        <select
          value={category} aria-label="Filtrar por categoría"
          onChange={e => { setCategory(e.target.value); load({ category: e.target.value }) }}
          className="rounded-xl border border-white/10 bg-zinc-900 px-2 py-2 text-xs text-zinc-300 focus:outline-none"
        >
          <option value="">todas las categorías</option>
          {(data?.stats.categories ?? []).map(c => <option key={c} value={c}>{c}</option>)}
        </select>
        {data && data.communities.length > 0 && (
          <select
            value={community} aria-label="Filtrar por comunidad"
            onChange={e => { setCommunity(e.target.value); load({ community: e.target.value }) }}
            className="rounded-xl border border-white/10 bg-zinc-900 px-2 py-2 text-xs text-zinc-300 focus:outline-none max-w-[160px]"
          >
            <option value="">todas las comunidades</option>
            {data.communities.slice(0, 30).map(c => <option key={c.id} value={String(c.id)}>{c.label} ({c.size})</option>)}
          </select>
        )}
        <div className="ml-auto flex items-center gap-1.5">
          <Button variant="ghost" size="icon" aria-label="Recargar grafo" onClick={() => load()}><RefreshCw className="h-4 w-4" /></Button>
          <Button variant="ghost" size="icon" aria-label={isFullscreen ? "Salir de pantalla completa" : "Pantalla completa"} onClick={() => setIsFullscreen(v => !v)}><Maximize2 className="h-4 w-4" /></Button>
        </div>
      </div>

      <div ref={wrapRef} className="relative flex-1 overflow-hidden bg-[radial-gradient(ellipse_at_top,_rgba(139,92,246,0.10),_transparent_60%)]">
        {loading && (
          <div className="absolute inset-0 z-10 flex items-center justify-center text-sm text-zinc-400">
            <RefreshCw className="h-5 w-5 animate-spin mr-2" /> Cargando grafo de conocimiento…
          </div>
        )}
        {error && !loading && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 text-sm">
            <div className="text-zinc-300 font-medium">No se pudo cargar el grafo</div>
            <div className="text-xs text-zinc-500 max-w-md text-center break-all">{error}</div>
            <button onClick={() => load()} className="mt-2 rounded-xl bg-violet-600 hover:bg-violet-500 px-3 py-1.5 text-xs font-medium text-white">Reintentar</button>
          </div>
        )}
        {data && data.nodes.length === 0 && !loading && !error && (
          <div className="absolute inset-0 z-10 flex flex-col items-center justify-center gap-2 text-sm text-zinc-500">
            <Network className="h-6 w-6 text-zinc-600" />
            Sin nodos para este filtro — probá otra búsqueda o quitá filtros.
          </div>
        )}
        {data && data.nodes.length > 0 && (
          <ForceGraph2D
            ref={fgRef}
            width={dims.w}
            height={dims.h}
            graphData={graphData}
            nodeLabel={(n: any) => `${n.label}`}
            nodeColor={(n: any) => n.color ?? CATEGORY_COLOR[n.category] ?? '#8b5cf6'}
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
              const dimmed = neighborIds !== null && !neighborIds.has(n.id)
              if (dimmed || (!neighborIds && selected !== null && n.id !== selected)) return
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
          <div className="absolute bottom-4 left-4 z-10 rounded-2xl border border-white/10 bg-zinc-900/95 backdrop-blur p-4 shadow-2xl max-w-sm max-h-[60%] overflow-auto">
            <div className="flex items-start justify-between gap-2">
              <div className="text-sm font-semibold">Nodo #{selected}</div>
              <button aria-label="Cerrar detalle" onClick={() => { setSelected(null); setDetail(null) }} className="rounded p-1 hover:bg-white/10 text-zinc-400"><X className="h-4 w-4" /></button>
            </div>
            {detailLoading && <div className="mt-2 text-xs text-zinc-500">Cargando detalle…</div>}
            {detail && (
              <div className="mt-2 space-y-2 text-xs">
                <div className="font-medium text-zinc-100 leading-snug">{detail.title?.split('\n')[0]?.slice(0, 120)}</div>
                <div className="flex flex-wrap gap-1">
                  <Badge variant="outline">{detail.category}</Badge>
                  <Badge variant="outline">importancia {detail.importance}</Badge>
                  <Badge variant="outline">{detail.degree} conexiones</Badge>
                </div>
                {detail.content && <div className="text-zinc-400 leading-relaxed whitespace-pre-wrap max-h-40 overflow-auto">{detail.content.slice(0, 600)}{detail.content.length > 600 ? '…' : ''}</div>}
                {detail.neighbors.length > 0 && (
                  <div>
                    <div className="text-zinc-500 tracking-widest text-[11px] mb-1">VECINOS ({detail.neighbors.length})</div>
                    <div className="space-y-1">
                      {detail.neighbors.slice(0, 12).map(nb => (
                        <button key={nb.edge_id} onClick={() => loadDetail(nb.other_id)} className="block w-full text-left rounded-lg bg-white/[0.04] hover:bg-white/[0.08] px-2 py-1.5">
                          <span className="text-violet-300 font-mono text-[11px]">{nb.relation}</span>{' '}
                          <span className="text-zinc-300">{nb.other_title.slice(0, 60)}</span>
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
          <div className="pointer-events-none absolute right-4 top-4 rounded-xl border border-white/10 bg-zinc-900/70 backdrop-blur px-3 py-2 text-xs text-zinc-400">
            {data.stats.visible_nodes ?? data.nodes.length} nodos · {data.stats.visible_edges ?? data.edges.length} aristas · de {data.stats.total_nodes} totales
          </div>
        )}
      </div>
    </div>
  )
}
