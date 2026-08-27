import { memo, useState } from 'react'

import { ContextUsagePanel } from '@/app/shell/context-usage-panel'
import { StatusRow } from '@/components/chat/status-row'
import { Codicon } from '@/components/ui/codicon'
import { Popover, PopoverContent, PopoverTrigger } from '@/components/ui/popover'
import { useI18n } from '@/i18n'
import { contextBarLabel, usageContextLabel } from '@/lib/statusbar'
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
  breakdown: ContextBreakdown | null
  loading: boolean
  usage: UsageStats
}

/**
 * Context readout for THIS conversation, in the composer status stack directly
 * under the transcript. Reads `<used>/<max>` and a percent at a glance; the row
 * IS the button — clicking it opens the shared {@link ContextUsagePanel} with
 * the per-category breakdown.
 *
 * The statusbar carries the same gauge, but it is hideable and lives at the far
 * edge of the window. This row is the one that answers "what is in my context
 * right now" without leaving the conversation.
 *
 * Presentational, like {@link ContextUsagePanel}: the stack fetches, because it
 * has to know whether this row has anything to say before it decides to render
 * the card at all.
 */
export const ContextStatusRow = memo(function ContextStatusRow({
  breakdown,
  loading,
  usage
}: ContextStatusRowProps) {
  const { t } = useI18n()
  const copy = t.statusStack.context
  const [open, setOpen] = useState(false)

  const summary = usageContextLabel(usage)
  const bar = contextBarLabel(usage)

  return (
    <Popover onOpenChange={setOpen} open={open}>
      <PopoverTrigger asChild>
        <StatusRow
          leading={<Codicon aria-hidden className="text-muted-foreground/70" name="pulse" size="0.8rem" />}
          onActivate={() => setOpen(value => !value)}
        >
          <span className="min-w-0 truncate text-[0.73rem] leading-4 text-foreground/92">{copy.label}</span>

          <span
            className="ml-auto flex shrink-0 items-center gap-2 text-[0.68rem] leading-4 text-muted-foreground/75 tabular-nums"
            data-slot="context-status-summary"
          >
            <span>{summary}</span>
            {bar && <span className="font-mono text-muted-foreground/60">{bar}</span>}
          </span>
        </StatusRow>
      </PopoverTrigger>

      {/* Same chrome the statusbar gauge gives this panel: the panel paints its
          own padding, so the popover contributes none. */}
      <PopoverContent align="start" className="w-auto border-(--ui-stroke-secondary) p-0" side="top">
        <ContextUsagePanel breakdown={breakdown} loading={loading} usage={usage} />
      </PopoverContent>
    </Popover>
  )
})
