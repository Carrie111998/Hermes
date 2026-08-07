import { useStore } from '@nanostores/react'
import { useId, useMemo, useState } from 'react'
import { useLocation } from 'react-router'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'
import { ChevronDown } from '@/lib/icons'
import {
  type CurrentPlanSnapshot,
  type CurrentPlanStatus,
  latestSessionPlan,
  type TodoStatus
} from '@/lib/todos'
import { cn } from '@/lib/utils'
import { $todosBySession } from '@/store/todos'

import { isNewChatRoute, routeSessionId } from '../routes'

import { $primaryRuntimeStoredId, routeSessionIdentityMismatch, useSessionView } from './session-view'

const PLAN_STATUS_BADGE: Record<CurrentPlanStatus, 'default' | 'muted' | 'warn'> = {
  active: 'default',
  paused: 'warn',
  completed: 'default',
  superseded: 'muted',
  historical: 'muted'
}

const ITEM_GLYPH: Record<TodoStatus, { icon: string; tone: string }> = {
  pending: { icon: 'circle-large-outline', tone: 'text-muted-foreground/65' },
  in_progress: { icon: 'debug-pause', tone: 'text-amber-500/80' },
  completed: { icon: 'pass-filled', tone: 'text-emerald-500/80' },
  cancelled: { icon: 'circle-slash', tone: 'text-muted-foreground/50' }
}

function formatUpdatedAt(timestamp: number | null, locale: string): string | null {
  if (!timestamp) {
    return null
  }

  return new Intl.DateTimeFormat(locale, {
    dateStyle: 'medium',
    timeStyle: 'short'
  }).format(new Date(timestamp * 1000))
}

export interface CurrentPlanPanelProps {
  plan: CurrentPlanSnapshot
  sessionId: string
}

/** Read-only disclosure for the latest persisted todo snapshot. It is a
 * conversation-history surface, not the live composer todo panel. */
export function CurrentPlanPanel({ plan, sessionId }: CurrentPlanPanelProps) {
  const { locale, t } = useI18n()
  const [expanded, setExpanded] = useState(false)
  const detailsId = useId()
  const copy = t.currentPlan
  const completion = copy.completion(plan.completedCount, plan.totalCount)
  const status = copy.statuses[plan.status]
  const updatedAt = formatUpdatedAt(plan.updatedAt, locale)
  const turn = plan.turnNumber ? copy.turn(plan.turnNumber) : copy.unknownTurn

  return (
    <section
      aria-label={copy.title}
      className="relative z-10 shrink-0 border-b border-(--ui-stroke-tertiary) bg-(--ui-chat-surface-background)"
      data-slot="current-plan"
    >
      <Button
        aria-controls={detailsId}
        aria-expanded={expanded}
        aria-label={copy.toggle(expanded, status, completion)}
        className="w-full justify-start text-left focus-visible:bg-(--ui-control-active-background) focus-visible:text-foreground"
        onClick={() => setExpanded(value => !value)}
        size="sm"
        type="button"
        variant="ghost"
      >
        <Codicon className="text-muted-foreground/75" name="checklist" size="0.85rem" />
        <span className="font-medium text-foreground/90">{copy.title}</span>
        <Badge size="xs" variant={PLAN_STATUS_BADGE[plan.status]}>
          {status}
        </Badge>
        <span className="min-w-0 flex-1 truncate text-muted-foreground/70">{completion}</span>
        <span className="hidden shrink-0 text-[0.65rem] font-normal text-muted-foreground/60 sm:inline">
          {updatedAt ? copy.updated(updatedAt) : copy.unknownUpdateTime}
        </span>
        <ChevronDown
          aria-hidden
          className={cn('size-3.5 shrink-0 text-muted-foreground/65 transition-transform', expanded && 'rotate-180')}
        />
      </Button>

      {expanded && (
        <div
          className="flex max-h-[min(50dvh,24rem)] min-h-0 flex-col gap-2 overflow-hidden px-3 pb-3 pt-1 text-xs"
          data-slot="current-plan-details"
          id={detailsId}
        >
          <div className="flex min-w-0 shrink-0 flex-wrap items-center gap-x-2 gap-y-1 text-[0.68rem] text-muted-foreground/75">
            <span className="min-w-0 break-all">{copy.provenance(turn, sessionId)}</span>
            <span aria-hidden>·</span>
            <span>{updatedAt ? copy.updated(updatedAt) : copy.unknownUpdateTime}</span>
          </div>

          {plan.hasNewerTurnWithoutTodo && (
            <p className="shrink-0 text-[0.68rem] leading-4 text-amber-600 dark:text-amber-300">{copy.newerTurn}</p>
          )}

          <ul
            aria-label={copy.title}
            className="min-h-0 flex-1 space-y-1 overflow-y-auto overscroll-contain pr-1"
            data-slot="current-plan-items"
          >
            {plan.items.map(item => {
              const glyph = ITEM_GLYPH[item.status]

              return (
                <li className="flex min-w-0 items-start gap-2 py-0.5" key={item.id}>
                  <Codicon className={cn('mt-0.5 shrink-0', glyph.tone)} name={glyph.icon} size="0.8rem" />
                  <span className="min-w-0 flex-1 break-words text-foreground/90">{item.content}</span>
                  <span className="shrink-0 text-[0.65rem] text-muted-foreground/65">
                    {copy.itemStatuses[item.status]}
                  </span>
                </li>
              )
            })}
          </ul>

          <p className="shrink-0 text-[0.65rem] leading-4 text-muted-foreground/65">{copy.livenessNotice}</p>
        </div>
      )}
    </section>
  )
}

function SettledCurrentPlan({ hasRuntime, sessionId }: { hasRuntime: boolean; sessionId: string }) {
  const messages = useStore(useSessionView().$messages)

  const plan = useMemo(
    () => latestSessionPlan(messages, { busy: false, hasRuntime }),
    [hasRuntime, messages]
  )

  return plan ? <CurrentPlanPanel key={sessionId} plan={plan} sessionId={sessionId} /> : null
}

/**
 * Mounts the persisted plan only when the live todo surface is absent.
 *
 * A live turn or the finished todo panel's four-second linger owns checklist
 * presentation exclusively. Once that transient state clears, this component
 * re-derives the latest snapshot from hydrated message history. It never writes
 * to or restores `$todosBySession`.
 */
export function CurrentPlanSurface({ suppressed = false }: { suppressed?: boolean } = {}) {
  const view = useSessionView()
  const busy = useStore(view.$busy)
  const runtimeId = useStore(view.$runtimeId)
  const storedId = useStore(view.$storedId)
  const todosBySession = useStore($todosBySession)
  const transientTodos = runtimeId ? todosBySession[runtimeId] : undefined

  if (suppressed || busy || transientTodos !== undefined || !storedId) {
    return null
  }

  return <SettledCurrentPlan hasRuntime={Boolean(runtimeId)} sessionId={storedId} />
}

/** Route-aware composition used by ChatView. It owns the identity gate so the
 * persisted surface cannot be wired without route/selection/runtime agreement. */
export function RoutedCurrentPlanSurface() {
  const view = useSessionView()
  const location = useLocation()
  const selectedStoredId = useStore(view.$storedId)
  const runtimeStoredId = useStore($primaryRuntimeStoredId)
  const routedStoredId = view.kind === 'primary' ? routeSessionId(location.pathname) : selectedStoredId

  const suppressed =
    view.kind === 'primary' &&
    (isNewChatRoute(location.pathname) ||
      (Boolean(routedStoredId) && routeSessionIdentityMismatch(routedStoredId, selectedStoredId, runtimeStoredId)))

  return <CurrentPlanSurface suppressed={suppressed} />
}
