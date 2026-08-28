import { useStore } from '@nanostores/react'
import { memo, useCallback, useMemo, useState } from 'react'

import { ContextUsagePanel } from '@/app/shell/context-usage-panel'
import { useContextBreakdown } from '@/app/shell/hooks/use-context-breakdown'
import { composerFloatingPill } from '@/components/chat/composer-dock'
import { Codicon } from '@/components/ui/codicon'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { Tip } from '@/components/ui/tooltip'
import type { HermesGateway } from '@/hermes'
import { useI18n } from '@/i18n'
import { usageContextLabel } from '@/lib/statusbar'
import { cn } from '@/lib/utils'
import { sessionCompacting } from '@/store/compaction'
import type { ContextBreakdown, UsageStats } from '@/types/hermes'

/** Flatten a breakdown into the {@link UsageStats} shape the shared gauge
 *  helpers and {@link ContextUsagePanel} read. The breakdown is the only usage
 *  this surface has: it reports MEASURED occupancy once the backend has it,
 *  falls back to its own estimate before that, and is keyed to the session it
 *  describes — unlike the global usage store, which merges across sessions. */
export function contextUsageFromBreakdown(breakdown: ContextBreakdown | null): UsageStats {
  return {
    calls: 0,
    context_max: breakdown?.context_max ?? 0,
    context_percent: breakdown?.context_percent ?? 0,
    context_used: breakdown?.context_used ?? 0,
    input: 0,
    output: 0,
    total: breakdown?.estimated_total ?? 0
  }
}

interface ContextStatusRowProps {
  /** Live turn: the transcript changes on every delta, so an estimate would be
   *  both stale and wasteful mid-turn (see `useContextBreakdown`). */
  busy: boolean
  gateway?: HermesGateway | null
  /** Runs `/compress` through the composer's own submit path, so it queues,
   *  clears, and reports exactly like typing it. Awaited, so the pill can
   *  release itself on whatever the submit settles as. */
  onCompact?: () => Promise<unknown> | void
  sessionId: null | string
}

/**
 * Context gauge for THIS conversation, in the chrome-free strip BELOW the
 * composer. Reads `<used>/<max>` and a percent at a glance; the pill opens the
 * shared {@link ContextUsagePanel} with the per-category breakdown, and its
 * neighbour compacts without going to find a slash command.
 *
 * Below and not above: the strip above the composer already carries the todo
 * list, subagents, background tasks, the queue, and the branch rail, and a
 * gauge that has to be hunted for among them is worse than no gauge. Down here
 * it holds one position that nothing else competes for.
 *
 * Renders nothing until the session reports a context window, so a fresh app
 * shows an empty strip rather than an empty gauge.
 */
export const ContextStatusRow = memo(function ContextStatusRow({
  busy,
  gateway,
  onCompact,
  sessionId
}: ContextStatusRowProps) {
  const { t } = useI18n()
  const copy = t.statusStack.context
  const [open, setOpen] = useState(false)
  const [submitting, setSubmitting] = useState(false)

  // Two sources, deliberately. The store flag is the real one — the gateway's
  // `status: compacting` event sets it and the next stream event clears it —
  // but it depends on an event arriving, and a socket drop mid-compaction
  // would leave this pill disabled with no way back. The local flag is owned
  // by the click and released in `finally`, so the pill always recovers.
  const streamCompacting = useStore(useMemo(() => sessionCompacting(sessionId), [sessionId]))
  const compacting = streamCompacting || submitting

  const runCompact = useCallback(async () => {
    if (!onCompact) {
      return
    }

    setSubmitting(true)

    try {
      await onCompact()
    } catch {
      // Swallowed on purpose: the slash path already renders the failure into
      // the transcript. Rethrowing here would only surface as an unhandled
      // rejection in the renderer.
    } finally {
      setSubmitting(false)
    }
  }, [onCompact])

  const requestGateway = useCallback(
    <T,>(method: string, params?: Record<string, unknown>): Promise<T> =>
      gateway ? gateway.request<T>(method, params) : Promise.reject(new Error('gateway unavailable')),
    [gateway]
  )

  const { breakdown, loading } = useContextBreakdown({
    busy,
    enabled: Boolean(gateway),
    requestGateway,
    sessionId
  })

  const usage = useMemo(() => contextUsageFromBreakdown(breakdown), [breakdown])
  const summary = usageContextLabel(usage)
  const percent = Math.max(0, Math.min(100, Math.round(usage.context_percent ?? 0)))

  if (!summary) {
    return null
  }

  return (
    <>
      <Popover onOpenChange={setOpen} open={open}>
        <PopoverTrigger asChild>
          <button
            className={cn(composerFloatingPill, 'gap-2 tabular-nums')}
            data-slot="context-status-pill"
            type="button"
          >
            <Codicon aria-hidden className="text-(--ui-text-tertiary)" name="pulse" size="0.8rem" />

            <span className="text-(--ui-text-tertiary)">{copy.label}</span>

            <span data-slot="context-status-summary">{summary}</span>

            {/* The number that actually drives the decision to compact, so it
                gets the emphasis and warms as the window fills. */}
            <span
              className={cn(
                percent >= 90 ? 'text-(--ui-red)' : percent >= 70 ? 'text-(--ui-orange)' : 'text-(--ui-text-tertiary)'
              )}
            >
              {percent}%
            </span>
          </button>
        </PopoverTrigger>

        {/* Same chrome the statusbar gauge gives this panel: the panel paints
            its own padding, so the popover contributes none. */}
        <PopoverContent align="start" className="w-auto border-(--ui-stroke-secondary) p-0" side="top">
          <ContextUsagePanel breakdown={breakdown} loading={loading} usage={usage} />
        </PopoverContent>
      </Popover>

      {onCompact && (
        <Tip label={copy.compactHint}>
          <button
            aria-label={copy.compact}
            className={cn(composerFloatingPill, 'disabled:cursor-default disabled:opacity-45')}
            data-slot="context-compact-pill"
            disabled={busy || compacting}
            onClick={() => void runCompact()}
            type="button"
          >
            {compacting ? (
              <GlyphSpinner ariaLabel={copy.compacting} className="text-[0.8rem] leading-none" spinner="braille" />
            ) : (
              <Codicon aria-hidden name="fold" size="0.8rem" />
            )}

            <span>{compacting ? copy.compacting : copy.compact}</span>
          </button>
        </Tip>
      )}
    </>
  )
})
