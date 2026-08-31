import { useStore } from '@nanostores/react'
import { useEffect } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Progress } from '@/components/ui/progress'
import { useI18n } from '@/i18n'
import { compactNumber } from '@/lib/format'
import { fmtDayTime } from '@/lib/time'
import { cn } from '@/lib/utils'
import { $providerUsage, refreshProviderUsage } from '@/store/provider-usage'
import type { ProviderUsageSnapshot, ProviderUsageWindow } from '@/types/hermes'

/** Decimal strings on the wire (JSON floats lose cents); parse only to render. */
function num(value: null | string | undefined): null | number {
  if (value === null || value === undefined || value === '') {
    return null
  }

  const parsed = Number(value)

  return Number.isFinite(parsed) ? parsed : null
}

/** The figure a window leads with, in its OWN unit — never a percentage that
 *  the provider did not give us. */
function windowFigure(window: ProviderUsageWindow): string {
  const remaining = num(window.remaining)
  const limit = num(window.limit)
  const used = num(window.used)

  if (window.unit === 'currency') {
    const amount = remaining ?? used
    const symbol = window.currency === 'USD' || !window.currency ? '$' : `${window.currency} `

    return amount === null ? '' : `${symbol}${amount.toFixed(2)}`
  }

  if (window.unit === 'percent') {
    // used_percent, not the raw `used`: the backend already derived and clamped
    // it from that same field, and the bar below renders it. One number, one
    // clamp, one place it can be wrong.
    const percent = window.used_percent ?? null

    return percent === null ? '' : `${Math.round(percent)}%`
  }

  if (remaining !== null && limit !== null) {
    return `${compactNumber(remaining)} / ${compactNumber(limit)}`
  }

  return remaining !== null ? compactNumber(remaining) : used !== null ? compactNumber(used) : ''
}

function ResetHint({ at }: { at: null | string | undefined }) {
  const { t } = useI18n()

  if (!at) {
    return null
  }

  const when = new Date(at)

  if (Number.isNaN(when.getTime())) {
    return null
  }

  return (
    <span className="shrink-0 text-[0.625rem] text-muted-foreground/55">
      {t.shell.statusbar.accountUsagePanel.resetsAt(fmtDayTime.format(when))}
    </span>
  )
}

function UsageBar({ percent }: { percent: null | number }) {
  if (percent === null) {
    return null
  }

  const clamped = Math.max(0, Math.min(100, percent))

  // The shared meter owns the track, the sizing, and role=progressbar with
  // its aria values. Only the fill tone is ours, via the sanctioned override,
  // exactly as the billing bars do it.
  return (
    <Progress
      data-slot="account-usage-bar"
      fillClassName={clamped >= 90 ? 'bg-(--ui-red)' : clamped >= 70 ? 'bg-(--ui-orange)' : 'bg-(--ui-green)'}
      size="sm"
      value={clamped / 100}
    />
  )
}

function ProviderCard({ snapshot }: { snapshot: ProviderUsageSnapshot }) {
  const { t } = useI18n()
  const copy = t.shell.statusbar.accountUsagePanel

  return (
    <li
      // Cached numbers being refreshed behind the panel: dim rather than
      // blank, so stale-while-revalidate is visible instead of invisible.
      className={cn('flex flex-col gap-1.5', snapshot.stale && 'opacity-60')}
      data-slot="account-usage-provider"
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="truncate font-medium text-foreground">{snapshot.display_name}</span>

        {snapshot.plan && <span className="shrink-0 text-[0.625rem] text-muted-foreground/70">{snapshot.plan}</span>}
      </div>

      {snapshot.state !== 'ok' ? (
        // Typed state, one translated string each — never a raw provider
        // message, which would be untranslatable and unactionable.
        <span className="text-[0.6875rem] text-muted-foreground/70">{copy.states[snapshot.state]}</span>
      ) : (
        snapshot.windows.map(window => (
          <div className="flex flex-col gap-1" key={`${snapshot.provider}:${window.label}`}>
            <div className="flex items-baseline justify-between gap-2">
              <span className="truncate text-muted-foreground">{copy.windows[window.label] ?? window.label}</span>

              <span className="flex shrink-0 items-baseline gap-2">
                <ResetHint at={window.reset_at} />
                <span className="tabular-nums text-foreground">{windowFigure(window)}</span>
              </span>
            </div>

            <UsageBar percent={window.used_percent ?? null} />

            {window.detail && <span className="text-[0.625rem] text-muted-foreground/55">{window.detail}</span>}
          </div>
        ))
      )}
    </li>
  )
}

/**
 * Every connected subscription's plan state in one place — Claude, Codex,
 * Kimi, OpenRouter, and whatever a provider plugin adds.
 *
 * Fetches on mount, which is when the popover opens. The backend caches per
 * provider and serves stale-while-revalidate, so reopening is nearly free and
 * the panel never holds several cross-host round-trips before first paint.
 * Refresh is the explicit way to pay for fresh numbers.
 */
export function AccountUsagePanel() {
  const { t } = useI18n()
  const copy = t.shell.statusbar.accountUsagePanel
  const { loading, providers } = useStore($providerUsage)

  useEffect(() => {
    void refreshProviderUsage()
  }, [])

  return (
    <div className="flex w-80 flex-col gap-3 p-3 text-[0.75rem]" data-slot="account-usage-panel">
      <div className="flex items-baseline justify-between gap-2">
        <p className="font-medium text-foreground">{copy.title}</p>

        <Button
          aria-label={copy.refresh}
          className="size-5 text-muted-foreground/60 hover:text-foreground"
          disabled={loading}
          onClick={() => void refreshProviderUsage({ force: true })}
          size="icon-xs"
          type="button"
          variant="ghost"
        >
          <Codicon className={cn(loading && 'animate-spin')} name="refresh" size="0.75rem" />
        </Button>
      </div>

      {providers.length > 0 ? (
        <ul className="flex flex-col gap-3">
          {providers.map(snapshot => (
            <ProviderCard key={snapshot.provider} snapshot={snapshot} />
          ))}
        </ul>
      ) : (
        <p className="text-[0.6875rem] text-muted-foreground">{loading ? copy.loading : copy.empty}</p>
      )}
    </div>
  )
}
