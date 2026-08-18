import { useMemo, useState } from 'react'

import { useI18n } from '@/i18n'
import type { ChatMessage } from '@/lib/chat-messages'
import { cn } from '@/lib/utils'

import { projectTrajectory, type TrajectoryRecord } from './trajectory-projector'

interface TrajectoryPanelProps {
  messages: readonly ChatMessage[]
  model: string
  provider: string
}

function countLabel(value: number, label: string): string {
  return `${value} ${label}`
}

function elapsed(value: number | null): string {
  if (value === null) {
    return '—'
  }

  if (value < 1000) {
    return `${value} ms`
  }

  return `${(value / 1000).toFixed(value < 10_000 ? 2 : 1)} s`
}

function detail(value: unknown): string {
  if (value === undefined) {
    return '—'
  }

  if (typeof value === 'string') {
    return value || '—'
  }

  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

function compactDetail(value: unknown, limit = 260): string {
  if (value === undefined) {
    return ''
  }

  const text = typeof value === 'string' ? value : JSON.stringify(value)

  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text
}

function RecordBadge({ record }: { record: TrajectoryRecord }) {
  const { t } = useI18n()

  const labels = {
    assistant: t.trajectory.assistant,
    reasoning: t.trajectory.reasoning,
    tool: t.trajectory.tool,
    user: t.trajectory.user
  } as const

  return (
    <span
      className={cn(
        'min-w-20 shrink-0 font-mono text-[10px] font-semibold tracking-[0.12em]',
        record.kind === 'tool' && 'text-amber-500',
        record.kind === 'reasoning' && 'text-violet-400',
        record.kind === 'user' && 'text-sky-400',
        record.kind === 'assistant' && 'text-violet-400'
      )}
    >
      {labels[record.kind]}
    </span>
  )
}

function ExecutionOverview({ records }: { records: readonly TrajectoryRecord[] }) {
  const { t } = useI18n()

  const lanes = [
    { label: t.trajectory.input, records: records.filter(record => record.kind === 'user') },
    {
      label: t.trajectory.modelLane,
      records: records.filter(record => record.kind === 'assistant' || record.kind === 'reasoning')
    },
    { label: t.trajectory.toolsLane, records: records.filter(record => record.kind === 'tool') }
  ]

  const starts = records.map(record => record.startedAt).filter((value): value is number => value !== null)
  const endings = records.map(record => record.completedAt ?? record.startedAt).filter((value): value is number => value !== null)
  const start = starts.length ? Math.min(...starts) : 0
  const end = endings.length ? Math.max(...endings) : start + 1
  const span = Math.max(end - start, 0.001)

  return (
    <div aria-label={t.trajectory.executionOverview} className="border-b border-(--ui-border) bg-(--ui-surface) px-3 py-2" role="region">
      <div className="space-y-1">
        {lanes.map(lane => (
          <div className="flex h-4 items-center gap-2" key={lane.label}>
            <span className="w-11 shrink-0 text-[10px] text-(--ui-text-faint)">{lane.label}</span>
            <div className="relative h-2.5 min-w-0 flex-1 overflow-hidden rounded-[2px] bg-(--ui-background)">
              {lane.records.map((record, index) => {
                const recordStart = record.startedAt ?? start + (index / Math.max(lane.records.length, 1)) * span
                const recordEnd = record.completedAt ?? recordStart + span * 0.015
                const left = Math.max(0, Math.min(98, ((recordStart - start) / span) * 100))
                const width = Math.max(1.2, Math.min(100 - left, ((recordEnd - recordStart) / span) * 100))

                return (
                  <span
                    className={cn(
                      'absolute inset-y-0 rounded-[2px]',
                      record.kind === 'user' && 'bg-sky-400/80',
                      record.kind === 'reasoning' && 'bg-violet-400/75',
                      record.kind === 'assistant' && 'bg-emerald-400/75',
                      record.kind === 'tool' && record.status !== 'error' && 'bg-amber-500/85',
                      record.kind === 'tool' && record.status === 'error' && 'bg-(--ui-danger)'
                    )}
                    key={record.id}
                    style={{ left: `${left}%`, width: `${width}%` }}
                    title={`${record.name || record.kind} · ${elapsed(record.durationMs)}`}
                  />
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}

export function TrajectoryPanel({ messages, model, provider }: TrajectoryPanelProps) {
  const { t } = useI18n()
  const [query, setQuery] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const projection = useMemo(() => projectTrajectory(messages, { model, provider }), [messages, model, provider])

  const normalizedQuery = query.trim().toLowerCase()

  const records = useMemo(
    () =>
      normalizedQuery
        ? projection.records.filter(record =>
            [record.kind, record.name, record.text, detail(record.payload), detail(record.result)]
              .filter(Boolean)
              .some(value => String(value).toLowerCase().includes(normalizedQuery))
          )
        : projection.records,
    [normalizedQuery, projection.records]
  )

  return (
    <section aria-label={t.trajectory.label} className="relative z-10 flex h-full min-h-0 flex-col bg-(--ui-chat-surface-background)">
      <div className="flex flex-wrap items-center gap-x-5 gap-y-2 border-b border-(--ui-border) bg-(--ui-surface) px-3 py-2 text-xs text-(--ui-text-muted)">
        <span>{t.trajectory.duration} <strong className="font-medium text-(--ui-text)">{elapsed(projection.summary.durationMs)}</strong></span>
        <span>{t.trajectory.turns} <strong className="font-medium text-(--ui-text)">{projection.summary.turns}</strong></span>
        <span>{t.trajectory.calls} <strong className="font-medium text-(--ui-text)">{projection.summary.toolCalls}</strong></span>
        <span className="hidden text-(--ui-text-faint) xl:inline">{provider || t.trajectory.unknown} / {model || t.trajectory.unknown}</span>
        {projection.summary.errors > 0 && <span className="text-(--ui-danger)">{countLabel(projection.summary.errors, t.trajectory.error)}</span>}
        <label className="ml-auto flex min-w-52 items-center rounded border border-(--ui-border) bg-(--ui-background) px-2 py-1.5 focus-within:border-(--ui-accent)">
          <span className="sr-only">{t.trajectory.search}</span>
          <input
            aria-label={t.trajectory.search}
            className="w-full bg-transparent text-xs text-(--ui-text) outline-none placeholder:text-(--ui-text-faint)"
            onChange={event => setQuery(event.currentTarget.value)}
            placeholder={t.trajectory.searchPlaceholder}
            type="search"
            value={query}
          />
        </label>
      </div>

      <ExecutionOverview records={projection.records} />

      <div aria-label={t.trajectory.timeline} className="min-h-0 min-w-0 flex-1 overflow-y-auto" role="region">
          {records.length ? (
            <div>
              {records.map((record, index) => {
                const expanded = selectedId === record.id
                const primary = record.name || record.text || t.trajectory.emptyEvent
                const payload = record.kind === 'tool' ? compactDetail(record.payload) : ''
                const result = record.kind === 'tool' ? compactDetail(record.result) : ''

                return (
                  <div className="relative border-b border-(--ui-border) pl-8" key={record.id}>
                    {index < records.length - 1 && <span className="absolute bottom-0 left-[15px] top-0 w-px bg-(--ui-border)" />}
                    <span
                      className={cn(
                        'absolute left-[12px] top-[17px] size-[7px] rounded-full ring-2 ring-(--ui-chat-surface-background)',
                        record.status === 'error' ? 'bg-(--ui-danger)' : 'bg-(--ui-text-faint)'
                      )}
                    />
                    <button
                      aria-expanded={expanded}
                      aria-label={`${record.kind} ${record.name || record.text}`}
                      className={cn(
                        'group grid w-full grid-cols-[5.75rem_minmax(0,1fr)_auto] items-center gap-3 px-3 py-2 text-left hover:bg-(--ui-hover)',
                        expanded && 'bg-(--ui-selected)'
                      )}
                      onClick={() => setSelectedId(expanded ? null : record.id)}
                      type="button"
                    >
                      <RecordBadge record={record} />
                      <span className={cn('min-w-0 truncate text-[13px] text-(--ui-text)', record.kind === 'tool' && 'font-mono text-[12px]')}>
                        <strong className="font-medium">{primary}</strong>
                        {payload && <span className="ml-2 text-(--ui-text-muted)">{payload}</span>}
                        {record.kind === 'tool' && <span className="mx-2 text-(--ui-text-faint)">→</span>}
                        {record.kind === 'tool' && (
                          <span className={record.status === 'error' ? 'text-(--ui-danger)' : 'text-(--ui-text-muted)'}>
                            {result || (record.status === 'running' ? t.trajectory.running : '—')}
                          </span>
                        )}
                      </span>
                      <span className="whitespace-nowrap font-mono text-[10px] text-(--ui-text-faint)">
                        {record.step === null ? `T${record.turn}` : `T${record.turn} · S${record.step}`} · {elapsed(record.durationMs)}
                      </span>
                    </button>
                    {expanded && (
                      <div className="grid gap-3 border-t border-(--ui-border) bg-(--ui-background) px-3 py-3 text-xs lg:grid-cols-2">
                        <div>
                          <p className="mb-1 font-semibold uppercase tracking-wider text-(--ui-text-faint)">{t.trajectory.payload}</p>
                          <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-(--ui-text-muted)">{detail(record.payload ?? record.text)}</pre>
                        </div>
                        {record.kind === 'tool' && (
                          <div>
                            <p className="mb-1 font-semibold uppercase tracking-wider text-(--ui-text-faint)">{t.trajectory.result}</p>
                            <pre className="max-h-56 overflow-auto whitespace-pre-wrap break-words font-mono text-[11px] text-(--ui-text-muted)">{detail(record.result)}</pre>
                          </div>
                        )}
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          ) : (
            <div className="grid h-full place-items-center px-8 text-center text-sm text-(--ui-text-muted)">
              {projection.records.length ? t.trajectory.noMatches : t.trajectory.waiting}
            </div>
          )}
      </div>
    </section>
  )
}
