import { useEffect, useMemo, useRef, useState } from 'react'

import {
  DEFAULT_KEEP_RECENT_TURNS,
  KEEP_RECENT_TURN_OPTIONS,
  type KeepRecentTurns
} from '@/app/shell/context-compression-action'
import { Button } from '@/components/ui/button'
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select'
import { useI18n } from '@/i18n'
import { compactNumber } from '@/lib/format'
import { AlertTriangle } from '@/lib/icons'
import { contextCompressionPressure, contextTokensUntilCompression } from '@/lib/statusbar'
import { cn } from '@/lib/utils'
import type { ContextBreakdown, ContextUsageCategory, UsageStats } from '@/types/hermes'

interface ContextUsagePanelProps {
  compressNowDisabled?: boolean
  currentUsage: UsageStats
  keepRecentTurns?: KeepRecentTurns
  onCompressNow?: (keepRecentTurns: KeepRecentTurns) => void
  onKeepRecentTurnsChange?: (keepRecentTurns: KeepRecentTurns) => void
  onUsageSnapshot?: (
    usage: Pick<
      UsageStats,
      | 'compression_threshold_percent'
      | 'compression_threshold_tokens'
      | 'context_max'
      | 'context_percent'
      | 'context_used'
    >
  ) => void
  requestGateway: <T = unknown>(method: string, params?: Record<string, unknown>) => Promise<T>
  sessionId: string | null
}

export function ContextUsagePanel({
  compressNowDisabled = false,
  currentUsage,
  keepRecentTurns: controlledKeepRecentTurns,
  onCompressNow,
  onKeepRecentTurnsChange,
  onUsageSnapshot,
  requestGateway,
  sessionId
}: ContextUsagePanelProps) {
  const { t } = useI18n()
  const copy = t.shell.statusbar.contextUsagePanel
  const [breakdown, setBreakdown] = useState<ContextBreakdown | null>(null)
  const [loading, setLoading] = useState(false)
  const [internalKeepRecentTurns, setInternalKeepRecentTurns] = useState<KeepRecentTurns>(DEFAULT_KEEP_RECENT_TURNS)
  const keepRecentTurns = controlledKeepRecentTurns === undefined ? internalKeepRecentTurns : controlledKeepRecentTurns
  const onUsageSnapshotRef = useRef(onUsageSnapshot)
  onUsageSnapshotRef.current = onUsageSnapshot

  useEffect(() => {
    if (!sessionId) {
      setBreakdown(null)
      setLoading(false)

      return
    }

    let cancelled = false
    setLoading(true)

    void requestGateway<ContextBreakdown>('session.context_breakdown', { session_id: sessionId })
      .then(data => {
        if (!cancelled) {
          setBreakdown(data)
          onUsageSnapshotRef.current?.({
            compression_threshold_percent: data.compression_threshold_percent,
            compression_threshold_tokens: data.compression_threshold_tokens,
            context_max: data.context_max,
            context_percent: data.context_percent,
            context_used: data.context_used
          })
        }
      })
      .catch(() => {
        if (!cancelled) {
          setBreakdown(null)
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false)
        }
      })

    return () => {
      cancelled = true
    }
  }, [requestGateway, sessionId])

  const contextMax = breakdown?.context_max ?? currentUsage.context_max ?? 0
  const contextUsed = breakdown?.context_used ?? currentUsage.context_used ?? 0

  const compressionThresholdTokens =
    breakdown?.compression_threshold_tokens ?? currentUsage.compression_threshold_tokens ?? 0

  const compressionThresholdPercent = Math.max(
    0,
    Math.min(
      100,
      Math.round(breakdown?.compression_threshold_percent ?? currentUsage.compression_threshold_percent ?? 0)
    )
  )

  const contextPercent = Math.max(
    0,
    Math.min(100, Math.round(breakdown?.context_percent ?? currentUsage.context_percent ?? 0))
  )

  const categories = useMemo(
    () =>
      (breakdown?.categories ?? []).map(category => ({
        ...category,
        label: copy.categories[category.id as keyof typeof copy.categories] ?? category.label
      })),
    [breakdown?.categories, copy]
  )

  const segmentTotal = categories.reduce((sum, category) => sum + category.tokens, 0) || contextUsed || 1

  const pressureUsage: UsageStats = {
    ...currentUsage,
    compression_threshold_percent: compressionThresholdPercent,
    compression_threshold_tokens: compressionThresholdTokens,
    context_max: contextMax,
    context_percent: contextPercent,
    context_used: contextUsed
  }

  const compressionPressure = contextCompressionPressure(pressureUsage)
  const tokensUntilCompression = contextTokensUntilCompression(pressureUsage)

  return (
    <div className="flex w-72 flex-col gap-3 p-3 text-[0.75rem]" data-slot="context-usage-panel">
      <div className="flex items-baseline justify-between gap-2">
        <p className="font-medium text-foreground">{copy.title}</p>

        <span className="text-[0.6875rem] text-muted-foreground">
          {copy.tokenSummary(`~${compactNumber(contextUsed)}`, compactNumber(contextMax))}
        </span>
      </div>

      <p className="text-[0.6875rem] text-foreground">{copy.percentFull(contextPercent)}</p>

      {compressionThresholdTokens > 0 && (
        <div
          className={cn(
            'rounded-md border px-2.5 py-2 text-[0.6875rem]',
            compressionPressure === 'due'
              ? 'border-destructive/30 bg-destructive/8 text-destructive'
              : compressionPressure === 'near'
                ? 'border-amber-500/30 bg-amber-500/8 text-amber-700 dark:text-amber-300'
                : 'border-(--ui-stroke-tertiary) bg-(--ui-bg-elevated) text-muted-foreground'
          )}
          data-pressure={compressionPressure}
          data-slot="context-compression-threshold"
        >
          <p className="flex min-w-0 items-center gap-1.5 font-medium">
            {compressionPressure !== 'normal' && <AlertTriangle className="size-3 shrink-0" />}
            <span>
              {copy.automaticCompression(compressionThresholdPercent, compactNumber(compressionThresholdTokens))}
            </span>
          </p>
          <p className="mt-1 opacity-80">
            {tokensUntilCompression === 0
              ? copy.compressionDue
              : copy.tokensRemaining(compactNumber(tokensUntilCompression))}
          </p>

          {compressionPressure !== 'normal' && onCompressNow && (
            <div className="mt-2 flex items-center justify-end gap-1">
              <Select
                disabled={compressNowDisabled}
                onValueChange={value => {
                  const next = value === 'all' ? null : Number(value)
                  setInternalKeepRecentTurns(next)
                  onKeepRecentTurnsChange?.(next)
                }}
                value={keepRecentTurns === null ? 'all' : String(keepRecentTurns)}
              >
                <SelectTrigger
                  aria-label={copy.keepRecent}
                  className="h-6 w-24 px-2 text-[0.6875rem]"
                  size="xs"
                  title={copy.keepRecentTitle}
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent align="end">
                  <SelectItem value="all">{copy.keepRecentAll}</SelectItem>
                  {KEEP_RECENT_TURN_OPTIONS.map(turns => (
                    <SelectItem key={turns} value={String(turns)}>
                      {copy.keepRecentTurns(turns)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
              <Button
                className="h-6 shrink-0 px-2 text-[0.6875rem]"
                disabled={compressNowDisabled}
                onClick={() => onCompressNow(keepRecentTurns)}
                size="xs"
                title={compressNowDisabled ? copy.compressUnavailable : copy.compressNowTitle}
                type="button"
                variant="outline"
              >
                {copy.compressNow}
              </Button>
            </div>
          )}
        </div>
      )}

      <ContextUsageBar categories={categories} segmentTotal={segmentTotal} />

      <ul className="flex flex-col gap-1.5">
        {categories.map(category => (
          <li className="flex items-center justify-between gap-2" key={category.id}>
            <span className="flex min-w-0 items-center gap-2">
              <span className="size-2 shrink-0 rounded-[2px]" style={{ background: category.color }} />

              <span className="truncate text-muted-foreground">{category.label}</span>
            </span>

            <span className="shrink-0 tabular-nums text-foreground">{compactNumber(category.tokens)}</span>
          </li>
        ))}
      </ul>

      {loading && <p className="text-[0.6875rem] text-muted-foreground">{copy.loading}</p>}

      {!loading && !categories.length && <p className="text-[0.6875rem] text-muted-foreground">{copy.empty}</p>}
    </div>
  )
}

function ContextUsageBar({
  categories,
  segmentTotal
}: {
  categories: readonly ContextUsageCategory[]
  segmentTotal: number
}) {
  return (
    <div
      className={cn(
        'flex h-1.5 overflow-hidden rounded-full',
        categories.length ? 'bg-(--ui-stroke-tertiary)' : 'dither bg-(--ui-bg-elevated)'
      )}
      data-slot="context-usage-bar"
    >
      {categories.map(category => (
        <span
          className="h-full min-w-px"
          key={category.id}
          style={{
            background: category.color,
            width: `${(category.tokens / segmentTotal) * 100}%`
          }}
        />
      ))}
    </div>
  )
}
