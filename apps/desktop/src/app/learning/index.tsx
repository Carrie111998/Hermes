import { useQuery, useQueryClient } from '@tanstack/react-query'
import type * as React from 'react'
import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router'

import { PageLoader } from '@/components/page-loader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  getHermesConfigRecord,
  getLearningInbox,
  getLearningInboxItem,
  resolveLearningInboxItem,
  saveHermesConfig
} from '@/hermes'
import { useI18n } from '@/i18n'
import { Brain, Check, Clock, RefreshCw, X, Zap } from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notify, notifyError } from '@/store/notifications'
import type { LearningInboxDetail, LearningInboxItem, LearningInboxKind } from '@/types/hermes'

import { useRefreshHotkey } from '../hooks/use-refresh-hotkey'
import { SKILLS_ROUTE, STARMAP_ROUTE } from '../routes'

const KIND_ICONS: Record<LearningInboxKind, typeof Brain> = {
  memory: Brain,
  skill: Zap,
  automation: Clock
}

const EMPTY_ITEMS: LearningInboxItem[] = []

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function kindLabel(kind: LearningInboxKind, copy: ReturnType<typeof useI18n>['t']['learningInbox']): string {
  return copy[kind]
}

function formatCandidateTime(value: null | string): string {
  if (!value) {
    return ''
  }

  const date = new Date(value)

  return Number.isNaN(date.getTime()) ? '' : date.toLocaleString()
}

function itemFromId(items: LearningInboxItem[], id: null | string): LearningInboxItem | null {
  return id ? (items.find(item => item.id === id) ?? null) : null
}

function typeClasses(kind: LearningInboxKind): string {
  if (kind === 'memory') {
    return 'border-sky-500/25 bg-sky-500/10 text-sky-200'
  }

  if (kind === 'skill') {
    return 'border-violet-500/25 bg-violet-500/10 text-violet-200'
  }

  return 'border-amber-500/25 bg-amber-500/10 text-amber-200'
}

interface LearningInboxViewProps extends React.ComponentProps<'section'> {}

export function LearningInboxView({ className, ...props }: LearningInboxViewProps) {
  const { t } = useI18n()
  const copy = t.learningInbox
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [selectedId, setSelectedId] = useState<null | string>(null)
  const [busyId, setBusyId] = useState<null | string>(null)
  const [enablingApprovals, setEnablingApprovals] = useState(false)

  const inboxQuery = useQuery({
    queryKey: ['learning-inbox'],
    queryFn: getLearningInbox,
    staleTime: 0
  })

  const items = inboxQuery.data?.items ?? EMPTY_ITEMS
  const selectedItem = itemFromId(items, selectedId)

  const detailQuery = useQuery({
    queryKey: ['learning-inbox-item', selectedId],
    queryFn: () => getLearningInboxItem(selectedId as string),
    enabled: selectedId !== null,
    staleTime: 0
  })

  useEffect(() => {
    if (items.length === 0) {
      setSelectedId(null)

      return
    }

    if (!selectedId || !selectedItem) {
      setSelectedId(items[0].id)
    }
  }, [items, selectedId, selectedItem])

  const refresh = useCallback(async () => {
    await inboxQuery.refetch()
  }, [inboxQuery])

  useRefreshHotkey(refresh)

  const resolveItem = useCallback(
    async (item: LearningInboxItem, action: 'approve' | 'dismiss') => {
      setBusyId(item.id)

      try {
        const result = await resolveLearningInboxItem(item.id, action)

        if (!result.ok) {
          throw new Error(result.error || copy.actionFailed)
        }

        await queryClient.invalidateQueries({ queryKey: ['learning-inbox'] })
        await queryClient.invalidateQueries({ queryKey: ['learning-inbox-item', item.id] })
        setSelectedId(null)
        notify({
          kind: 'success',
          title: action === 'approve' ? copy.approved : copy.dismissed,
          message: item.title
        })
      } catch (error) {
        notifyError(error, copy.actionFailed)
      } finally {
        setBusyId(null)
      }
    },
    [copy, queryClient]
  )

  const enableApprovals = useCallback(async () => {
    setEnablingApprovals(true)

    try {
      const config = await getHermesConfigRecord()
      const memory = isRecord(config.memory) ? config.memory : {}
      const skills = isRecord(config.skills) ? config.skills : {}

      const result = await saveHermesConfig({
        ...config,
        memory: { ...memory, write_approval: true },
        skills: { ...skills, write_approval: true }
      })

      if (!result.ok) {
        throw new Error(copy.actionFailed)
      }

      await queryClient.invalidateQueries({ queryKey: ['learning-inbox'] })
      notify({ kind: 'success', title: copy.approvalsEnabled, message: copy.approvalHint })
    } catch (error) {
      notifyError(error, copy.actionFailed)
    } finally {
      setEnablingApprovals(false)
    }
  }, [copy, queryClient])

  if (inboxQuery.isLoading) {
    return <PageLoader label={copy.loading} />
  }

  if (inboxQuery.isError) {
    return (
      <section className="grid h-full place-items-center p-8 text-center" {...props}>
        <div className="max-w-md space-y-3">
          <p className="font-mono text-sm font-semibold text-(--ui-text-primary)">{copy.loadFailed}</p>
          <Button onClick={() => void refresh()} size="sm" variant="outline">
            <RefreshCw className="size-3.5" />
            {t.common.retry}
          </Button>
        </div>
      </section>
    )
  }

  const approvalsEnabled = Boolean(
    inboxQuery.data?.settings.memory_write_approval && inboxQuery.data.settings.skills_write_approval
  )

  const detail = detailQuery.data

  return (
    <section
      className={cn('flex h-full min-h-0 flex-col overflow-hidden bg-(--ui-editor-surface-background)', className)}
      {...props}
    >
      <header className="flex shrink-0 flex-wrap items-start justify-between gap-4 border-b border-(--ui-border) px-6 py-5">
        <div className="min-w-0 space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h1 className="font-mono text-lg font-semibold tracking-tight text-(--ui-text-primary)">{copy.title}</h1>
            <span className="rounded-full border border-(--ui-border) px-2 py-0.5 font-mono text-[10px] text-(--ui-text-tertiary)">
              {copy.candidateCount(items.length)}
            </span>
          </div>
          <p className="max-w-2xl text-xs leading-5 text-(--ui-text-tertiary)">{copy.subtitle}</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Button onClick={() => navigate(STARMAP_ROUTE)} size="sm" variant="ghost">
            {copy.openGraph}
          </Button>
          <Button onClick={() => navigate(SKILLS_ROUTE)} size="sm" variant="ghost">
            {copy.openSkills}
          </Button>
          <Button disabled={inboxQuery.isFetching} onClick={() => void refresh()} size="sm" variant="outline">
            <RefreshCw className={cn('size-3.5', inboxQuery.isFetching && 'animate-spin')} />
            {copy.refresh}
          </Button>
        </div>
      </header>

      {!approvalsEnabled && (
        <div className="mx-6 mt-4 flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-500/25 bg-amber-500/10 px-4 py-3">
          <div className="min-w-0">
            <p className="font-mono text-xs font-semibold text-amber-100">{copy.approvalOff}</p>
            <p className="mt-1 text-xs text-amber-100/70">{copy.approvalHint}</p>
          </div>
          <Button disabled={enablingApprovals} onClick={() => void enableApprovals()} size="sm" variant="outline">
            {enablingApprovals ? t.common.saving : copy.enableApprovals}
          </Button>
        </div>
      )}

      {approvalsEnabled && (
        <div className="mx-6 mt-4 flex items-center gap-2 rounded-md border border-emerald-500/20 bg-emerald-500/8 px-4 py-2 text-xs text-emerald-100/80">
          <Check className="size-3.5" />
          {copy.approvalsEnabled}
        </div>
      )}

      <div className="grid min-h-0 flex-1 grid-cols-[minmax(250px,0.34fr)_minmax(0,0.66fr)] divide-x divide-(--ui-border) pt-4">
        <aside className="min-h-0 overflow-y-auto px-4 pb-6">
          {items.length === 0 ? (
            <div className="grid min-h-64 place-items-center px-4 text-center">
              <div className="max-w-xs space-y-2">
                <Brain className="mx-auto size-8 text-(--ui-text-quaternary)" />
                <p className="font-mono text-sm font-semibold text-(--ui-text-primary)">{copy.emptyTitle}</p>
                <p className="text-xs leading-5 text-(--ui-text-tertiary)">{copy.emptyDesc}</p>
              </div>
            </div>
          ) : (
            <div className="space-y-2">
              {items.map(item => {
                const Icon = KIND_ICONS[item.kind]
                const selected = item.id === selectedId
                const busy = item.id === busyId

                return (
                  <button
                    aria-pressed={selected}
                    className={cn(
                      'w-full rounded-md border p-3 text-left transition-colors',
                      selected
                        ? 'border-(--ui-border-active) bg-(--ui-surface-active)'
                        : 'border-(--ui-border) bg-(--ui-surface) hover:border-(--ui-border-hover)'
                    )}
                    disabled={busy}
                    key={item.id}
                    onClick={() => setSelectedId(item.id)}
                    type="button"
                  >
                    <div className="flex items-start gap-3">
                      <span className={cn('mt-0.5 rounded border p-1.5', typeClasses(item.kind))}>
                        <Icon className="size-3.5" />
                      </span>
                      <span className="min-w-0 flex-1">
                        <span className="flex items-center justify-between gap-2">
                          <span className="truncate font-mono text-xs font-semibold text-(--ui-text-primary)">
                            {item.title}
                          </span>
                          {busy && <RefreshCw className="size-3 shrink-0 animate-spin text-(--ui-text-tertiary)" />}
                        </span>
                        <span className="mt-1 flex items-center gap-2 text-[10px] text-(--ui-text-tertiary)">
                          <span>{kindLabel(item.kind, copy)}</span>
                          <span>·</span>
                          <span>{item.source}</span>
                        </span>
                        {item.summary && (
                          <span className="mt-2 line-clamp-2 block text-xs leading-4 text-(--ui-text-secondary)">
                            {item.summary}
                          </span>
                        )}
                      </span>
                    </div>
                  </button>
                )
              })}
            </div>
          )}
        </aside>

        <main className="min-h-0 overflow-y-auto px-6 pb-8">
          {!selectedItem ? (
            <div className="grid min-h-64 place-items-center text-center text-xs text-(--ui-text-tertiary)">
              {copy.emptyTitle}
            </div>
          ) : detailQuery.isLoading ? (
            <PageLoader label={copy.loading} />
          ) : detailQuery.isError || !detail ? (
            <div className="grid min-h-64 place-items-center text-center text-xs text-(--ui-text-tertiary)">
              {copy.loadFailed}
            </div>
          ) : (
            <LearningDetail
              busy={busyId === selectedItem.id}
              copy={copy}
              detail={detail}
              onAction={action => void resolveItem(selectedItem, action)}
            />
          )}
        </main>
      </div>
    </section>
  )
}

function LearningDetail({
  busy,
  copy,
  detail,
  onAction
}: {
  busy: boolean
  copy: ReturnType<typeof useI18n>['t']['learningInbox']
  detail: LearningInboxDetail
  onAction: (action: 'approve' | 'dismiss') => void
}) {
  const Icon = KIND_ICONS[detail.kind]
  const created = formatCandidateTime(detail.created_at)

  return (
    <article className="mx-auto max-w-3xl space-y-6 pt-2">
      <header className="flex flex-wrap items-start justify-between gap-4">
        <div className="flex min-w-0 items-start gap-3">
          <span className={cn('mt-1 rounded border p-2', typeClasses(detail.kind))}>
            <Icon className="size-4" />
          </span>
          <div className="min-w-0 space-y-1">
            <div className="flex flex-wrap items-center gap-2">
              <h2 className="font-mono text-base font-semibold text-(--ui-text-primary)">{detail.title}</h2>
              <Badge className={cn('normal-case', typeClasses(detail.kind))} variant="outline">
                {kindLabel(detail.kind, copy)}
              </Badge>
            </div>
            {detail.summary && <p className="text-sm leading-6 text-(--ui-text-secondary)">{detail.summary}</p>}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Button disabled={busy} onClick={() => onAction('dismiss')} size="sm" variant="ghost">
            <X className="size-3.5" />
            {copy.dismiss}
          </Button>
          <Button disabled={busy} onClick={() => onAction('approve')} size="sm">
            <Check className="size-3.5" />
            {copy.approve}
          </Button>
        </div>
      </header>

      <section className="space-y-2">
        <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-(--ui-text-tertiary)">
          {copy.detail}
        </h3>
        <pre className="max-h-[48vh] overflow-auto rounded-md border border-(--ui-border) bg-(--ui-surface) p-4 font-mono text-xs leading-5 whitespace-pre-wrap text-(--ui-text-secondary)">
          {detail.detail || detail.preview}
        </pre>
      </section>

      <section className="space-y-2">
        <h3 className="font-mono text-[11px] font-semibold uppercase tracking-wider text-(--ui-text-tertiary)">
          {copy.evidence}
        </h3>
        <dl className="grid gap-2 rounded-md border border-(--ui-border) bg-(--ui-surface) p-4 text-xs sm:grid-cols-[auto_1fr]">
          <dt className="font-mono text-(--ui-text-tertiary)">{copy.source}</dt>
          <dd className="text-(--ui-text-secondary)">{detail.source}</dd>
          <dt className="font-mono text-(--ui-text-tertiary)">{copy.origin}</dt>
          <dd className="text-(--ui-text-secondary)">{detail.evidence.origin || detail.evidence.source || '—'}</dd>
          {created && (
            <>
              <dt className="font-mono text-(--ui-text-tertiary)">Created</dt>
              <dd className="text-(--ui-text-secondary)">{created}</dd>
            </>
          )}
          {detail.evidence.session_id ? (
            <>
              <dt className="font-mono text-(--ui-text-tertiary)">Session</dt>
              <dd className="font-mono text-(--ui-text-secondary)">{detail.evidence.session_id}</dd>
            </>
          ) : (
            <dd className="text-(--ui-text-tertiary) sm:col-start-2">{copy.noSessionEvidence}</dd>
          )}
        </dl>
      </section>
    </article>
  )
}
