/**
 * Knowledge screen — force-directed SVG canvas (zoom, pan, node drag, hover
 * highlight), legend, stats, plus a sidebar with the page list, search, and
 * a reader that follows [[wiki-links]] and backlinks.
 */
import {
  Button,
  cn,
  EmptyState,
  Input,
  ScrollArea,
  Tabs,
  TabsList,
  TabsTrigger,
  useQuery,
  useValue
} from '@hermes/plugin-sdk'
import { type PointerEvent as RPointerEvent, useEffect, useMemo, useRef, useState } from 'react'

import {
  $selectedPage,
  fetchGraph,
  fetchList,
  fetchPage,
  GRAPH_KEY,
  LIST_KEY,
  pageKey,
  searchKey,
  searchPages
} from './api'
import { forceLayout, nodeRadius } from './force-layout'
import { useKnowledgeI18n } from './i18n'
import type { KnowledgeNode } from './types'

const TYPE_COLORS: Record<string, string> = {
  guide: '#60a5fa',
  project: '#a78bfa',
  reference: '#2dd4bf',
  concept: '#fbbf24',
  note: '#94a3b8'
}

function typeColor(type?: string | null): string {
  return (type && TYPE_COLORS[type.toLowerCase()]) || '#94a3b8'
}

function GraphCanvas({
  nodes,
  edges,
  onOpen,
  k
}: {
  nodes: KnowledgeNode[]
  edges: Array<{ source: string; target: string }>
  onOpen: (id: string) => void
  k: ReturnType<typeof useKnowledgeI18n>
}) {
  const [scale, setScale] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [hovered, setHovered] = useState<string | null>(null)
  const [overrides, setOverrides] = useState<Record<string, { x: number; y: number }>>({})
  const svgRef = useRef<SVGSVGElement | null>(null)
  const panRef = useRef<{ startX: number; startY: number; panX: number; panY: number } | null>(null)
  const dragRef = useRef<{ id: string; dx: number; dy: number; moved: boolean } | null>(null)

  const degree = useMemo(() => {
    const map = new Map<string, number>()

    for (const n of nodes) {map.set(n.id, 0)}

    for (const e of edges) {
      map.set(e.source, (map.get(e.source) ?? 0) + 1)
      map.set(e.target, (map.get(e.target) ?? 0) + 1)
    }

    return map
  }, [nodes, edges])

  const base = useMemo(
    () =>
      forceLayout(
        nodes.map(n => n.id),
        edges,
        { width: 900, height: 620 }
      ),
    // Deterministic on the graph shape; recompute only when the shape changes.
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [nodes.map(n => n.id).join('|'), edges.map(e => `${e.source}>${e.target}`).join('|')]
  )

  const posOf = (id: string) => overrides[id] ?? base.get(id) ?? { x: 0, y: 0 }

  useEffect(() => {
    const svg = svgRef.current

    if (!svg) {return}

    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const factor = e.deltaY < 0 ? 1.1 : 1 / 1.1
      setScale(s => Math.min(4, Math.max(0.25, s * factor)))
    }

    svg.addEventListener('wheel', onWheel, { passive: false })

    return () => svg.removeEventListener('wheel', onWheel)
  }, [])

  const onBackgroundPointerDown = (e: RPointerEvent<SVGSVGElement>) => {
    if (e.target !== e.currentTarget) {return}
    panRef.current = { startX: e.clientX, startY: e.clientY, panX: pan.x, panY: pan.y }
    ;(e.currentTarget as SVGSVGElement).setPointerCapture(e.pointerId)
  }

  const onBackgroundPointerMove = (e: RPointerEvent<SVGSVGElement>) => {
    if (!panRef.current) {return}
    setPan({
      x: panRef.current.panX + (e.clientX - panRef.current.startX),
      y: panRef.current.panY + (e.clientY - panRef.current.startY)
    })
  }

  const onBackgroundPointerUp = (e: RPointerEvent<SVGSVGElement>) => {
    panRef.current = null
    ;(e.currentTarget as SVGSVGElement).releasePointerCapture(e.pointerId)
  }

  const onNodePointerDown = (e: RPointerEvent<SVGGElement>, id: string) => {
    e.stopPropagation()
    const p = posOf(id)
    dragRef.current = { id, dx: e.clientX - p.x * scale - pan.x, dy: e.clientY - p.y * scale - pan.y, moved: false }
    ;(e.currentTarget as SVGGElement).setPointerCapture(e.pointerId)
  }

  const onNodePointerMove = (e: RPointerEvent<SVGGElement>) => {
    const drag = dragRef.current

    if (!drag) {return}
    const x = (e.clientX - drag.dx - pan.x) / scale
    const y = (e.clientY - drag.dy - pan.y) / scale
    const current = posOf(drag.id)

    if (Math.abs(x - current.x) > 0.5 || Math.abs(y - current.y) > 0.5) {drag.moved = true}
    setOverrides(prev => ({ ...prev, [drag.id]: { x, y } }))
  }

  const onNodePointerUp = (e: RPointerEvent<SVGGElement>, id: string) => {
    const drag = dragRef.current
    dragRef.current = null
    ;(e.currentTarget as SVGGElement).releasePointerCapture(e.pointerId)

    if (drag?.moved) {return}
    onOpen(id)
  }

  return (
    <div className="relative h-full w-full overflow-hidden rounded-lg border border-(--ui-stroke-secondary) bg-(--ui-surface-secondary)">
      <svg
        className="h-full w-full cursor-grab select-none active:cursor-grabbing"
        onPointerDown={onBackgroundPointerDown}
        onPointerMove={onBackgroundPointerMove}
        onPointerUp={onBackgroundPointerUp}
        ref={svgRef}
      >
        <g transform={`translate(${pan.x} ${pan.y}) scale(${scale})`}>
          {edges.map((e, i) => {
            const a = posOf(e.source)
            const b = posOf(e.target)
            const active = hovered === e.source || hovered === e.target

            return (
              <line
                key={i}
                stroke={active ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}
                strokeWidth={active ? 1.5 : 1}
                x1={a.x}
                x2={b.x}
                y1={a.y}
                y2={b.y}
              />
            )
          })}

          {nodes.map(node => {
            const p = posOf(node.id)
            const dim = hovered !== null && hovered !== node.id

            const linked =
              hovered === null ||
              hovered === node.id ||
              edges.some(
                e => (e.source === hovered && e.target === node.id) || (e.target === hovered && e.source === node.id)
              )

            const radius = nodeRadius(degree.get(node.id) ?? 0)

            return (
              <g
                className={cn('cursor-pointer', dim && 'opacity-40')}
                key={node.id}
                onPointerDown={e => onNodePointerDown(e, node.id)}
                onPointerEnter={() => setHovered(node.id)}
                onPointerLeave={() => setHovered(null)}
                onPointerMove={onNodePointerMove}
                onPointerUp={e => onNodePointerUp(e, node.id)}
                opacity={hovered === null || linked ? 1 : 0.22}
                transform={`translate(${p.x} ${p.y})`}
              >
                <circle fill={typeColor(node.type)} opacity={0.85} r={radius} />
                {hovered === node.id && (
                  <circle fill="none" r={radius + 3} stroke="var(--ui-accent)" strokeWidth={1.5} />
                )}
                <text fill="var(--ui-text-secondary)" fontSize={10} textAnchor="middle" y={radius + 12}>
                  {node.title.length > 24 ? `${node.title.slice(0, 24)}…` : node.title}
                </text>
              </g>
            )
          })}
        </g>
      </svg>

      {/* Legend — only when typed nodes exist */}
      {nodes.some(n => n.type) && (
        <div className="absolute bottom-2 left-2 flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) bg-(--ui-surface) p-2">
          <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{k.legend}</span>
          {Object.entries(TYPE_COLORS).map(([type, color]) => {
            if (!nodes.some(n => (n.type ?? '').toLowerCase() === type)) {return null}

            return (
              <span className="flex items-center gap-1.5 text-[0.6875rem] text-(--ui-text-secondary)" key={type}>
                <span className="inline-block size-2 rounded-full" style={{ backgroundColor: color }} />
                {type}
              </span>
            )
          })}
        </div>
      )}

      <div className="absolute bottom-2 right-2 flex items-center gap-1">
        <Button onClick={() => setScale(s => Math.min(4, s * 1.25))} size="sm" variant="ghost">
          +
        </Button>
        <Button onClick={() => setScale(s => Math.max(0.25, s / 1.25))} size="sm" variant="ghost">
          −
        </Button>
        <span className="text-[0.6875rem] tabular-nums text-(--ui-text-tertiary)">
          {k.stats(nodes.length, edges.length)} · {Math.round(scale * 100)}%
        </span>
      </div>
    </div>
  )
}

function Reader({ path, k }: { path: string; k: ReturnType<typeof useKnowledgeI18n> }) {
  const { data, isLoading } = useQuery({ queryKey: pageKey(path), queryFn: () => fetchPage(path) })

  if (isLoading) {
    return <div className="p-3 text-sm text-(--ui-text-tertiary)">…</div>
  }

  if (!data?.ok) {
    return <div className="p-3 text-sm text-(--ui-text-tertiary)">{k.notInstalled}</div>
  }

  const { meta, content, backlinks } = data
  // Render [[wiki-links]] as clickable buttons that navigate the reader.
  const parts = content.split(/(\[\[[^\]]+\]\])/g).filter(Boolean)

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex items-start justify-between gap-2">
        <div>
          <h2 className="text-sm font-semibold text-(--ui-text-primary)">{meta.title}</h2>
          <p className="text-[0.6875rem] text-(--ui-text-tertiary)">{meta.path}</p>
        </div>
        <Button onClick={() => $selectedPage.set(null)} size="sm" variant="ghost">
          ✕
        </Button>
      </div>

      {meta.type && <span className="text-[0.6875rem] text-(--ui-text-secondary)">type: {meta.type}</span>}
      {meta.summary && <p className="text-xs text-(--ui-text-secondary)">{meta.summary}</p>}
      {meta.tags.length > 0 && (
        <p className="text-[0.6875rem] text-(--ui-text-tertiary)">
          {k.tags}: {meta.tags.join(', ')}
        </p>
      )}

      <div className="max-h-72 overflow-y-auto whitespace-pre-wrap rounded-md border border-(--ui-stroke-secondary) bg-(--ui-surface) p-2 text-xs leading-relaxed text-(--ui-text-secondary)">
        {parts.map((part, i) => {
          const m = part.match(/^\[\[([^\]]+)\]\]$/)

          if (!m) {return <span key={i}>{part}</span>}
          const target = m[1]!.split('|')[0]!.split('#')[0]!.trim()

          return (
            <button
              className="mx-0.5 inline cursor-pointer rounded bg-(--ui-accent-soft) px-1 text-(--ui-accent) hover:underline"
              key={i}
              onClick={() => $selectedPage.set(`${target}.md`)}
              type="button"
            >
              {target}
            </button>
          )
        })}
      </div>

      {backlinks.length > 0 && (
        <div className="flex flex-col gap-1">
          <span className="text-xs text-(--ui-text-secondary)">{k.backlinks(backlinks.length)}</span>
          {backlinks.map(b => (
            <button
              className="cursor-pointer text-left text-xs text-(--ui-accent) hover:underline"
              key={b}
              onClick={() => $selectedPage.set(b)}
              type="button"
            >
              {b}
            </button>
          ))}
        </div>
      )}
    </div>
  )
}

export function KnowledgeScreen() {
  const k = useKnowledgeI18n()
  const selected = useValue($selectedPage)
  const [tab, setTab] = useState<'pages' | 'search'>('pages')
  const [query, setQuery] = useState('')

  const {
    data: graph,
    isLoading,
    isError
  } = useQuery({ queryKey: GRAPH_KEY, queryFn: () => fetchGraph(), refetchInterval: 60_000 })

  const { data: list } = useQuery({ queryKey: LIST_KEY, queryFn: () => fetchList(), refetchInterval: 60_000 })

  const { data: search } = useQuery({
    queryKey: searchKey(query),
    queryFn: () => searchPages(query),
    enabled: query.trim().length > 0
  })

  if (isLoading) {
    return <div className="flex h-full items-center justify-center text-sm text-(--ui-text-tertiary)">…</div>
  }

  if (isError || !graph?.ok) {
    return <EmptyState description={k.notInstalledBody} title={k.notInstalled} />
  }

  return (
    <div className="flex h-full gap-3 p-4">
      <div className="flex min-w-0 flex-1 flex-col gap-2">
        <div className="flex items-center justify-between">
          <h1 className="text-lg font-semibold text-(--ui-text-primary)">{k.title}</h1>
          <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{graph.root}</span>
        </div>
        {graph.nodes.length === 0 ? (
          <div className="flex flex-1 items-center justify-center">
            <EmptyState description={k.emptyBody} title={k.emptyTitle} />
          </div>
        ) : (
          <GraphCanvas edges={graph.edges} k={k} nodes={graph.nodes} onOpen={id => $selectedPage.set(id)} />
        )}
      </div>

      <div className="flex w-[340px] shrink-0 flex-col overflow-hidden rounded-lg border border-(--ui-stroke-secondary)">
        {selected ? (
          <Reader k={k} path={selected} />
        ) : (
          <>
            <Tabs onValueChange={value => setTab(value as 'pages' | 'search')} value={tab}>
              <TabsList className="m-2">
                <TabsTrigger value="pages">{k.pages}</TabsTrigger>
                <TabsTrigger value="search">{k.search}</TabsTrigger>
              </TabsList>
            </Tabs>
            {tab === 'search' && (
              <div className="px-2 pb-2">
                <Input onChange={e => setQuery(e.target.value)} placeholder={k.searchPlaceholder} value={query} />
              </div>
            )}
            <ScrollArea className="min-h-0 flex-1">
              {tab === 'pages' && (
                <ul className="flex flex-col">
                  {(list?.pages ?? []).map(page => (
                    <button
                      className="flex cursor-pointer flex-col gap-0.5 border-b border-(--ui-stroke-secondary) px-3 py-2 text-left hover:bg-(--chrome-action-hover)"
                      key={page.path}
                      onClick={() => $selectedPage.set(page.path)}
                      type="button"
                    >
                      <span className="text-sm text-(--ui-text-primary)">{page.title}</span>
                      <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{page.path}</span>
                    </button>
                  ))}
                  {(list?.pages ?? []).length === 0 && (
                    <p className="p-3 text-xs text-(--ui-text-tertiary)">{k.emptyTitle}</p>
                  )}
                </ul>
              )}
              {tab === 'search' && (
                <ul className="flex flex-col">
                  {(search?.matches ?? []).map((m, i) => (
                    <button
                      className="flex cursor-pointer flex-col gap-0.5 border-b border-(--ui-stroke-secondary) px-3 py-2 text-left hover:bg-(--chrome-action-hover)"
                      key={i}
                      onClick={() => $selectedPage.set(m.path)}
                      type="button"
                    >
                      <span className="text-sm text-(--ui-text-primary)">{m.title}</span>
                      <span className="text-[0.6875rem] text-(--ui-text-tertiary)">
                        {m.path}:{m.line}
                      </span>
                      <span className="line-clamp-2 text-xs text-(--ui-text-secondary)">{m.text}</span>
                    </button>
                  ))}
                  {query.trim().length > 0 && (search?.matches ?? []).length === 0 && (
                    <p className="p-3 text-xs text-(--ui-text-tertiary)">{k.noResults}</p>
                  )}
                  {query.trim().length === 0 && (
                    <p className="p-3 text-xs text-(--ui-text-tertiary)">{k.searchPlaceholder}</p>
                  )}
                </ul>
              )}
            </ScrollArea>
          </>
        )}
      </div>
    </div>
  )
}
