import { ThinkingOrb } from 'thinking-orbs'

import { formatElapsed } from '@/components/chat/activity-timer'
import type { MoaAdvisorState, MoaProgressState } from '@/lib/moa-progress'
import { cn } from '@/lib/utils'

const SETTLED = new Set(['complete', 'failed', 'interrupted', 'skipped'])

function AdvisorMarker({ advisor }: { advisor: MoaAdvisorState }) {
  const copy = {
    complete: 'complete',
    failed: 'failed',
    interrupted: 'interrupted',
    queued: 'queued',
    running: 'running',
    skipped: 'skipped'
  }[advisor.status]

  return (
    <span
      aria-label={`${advisor.label}: ${copy}`}
      className={cn(
        'relative inline-flex size-2.5 shrink-0 items-center justify-center rounded-full border',
        advisor.status === 'running' && 'border-primary bg-primary/25',
        advisor.status === 'queued' && 'rounded-[2px] border-muted-foreground/45',
        advisor.status === 'complete' &&
          'border-emerald-500 bg-emerald-500 motion-safe:animate-[ping_450ms_ease-out_1]',
        advisor.status === 'failed' && 'border-destructive bg-destructive',
        advisor.status === 'interrupted' && 'border-amber-500 bg-amber-500/70',
        advisor.status === 'skipped' && 'border-muted-foreground/40 bg-muted-foreground/30'
      )}
      role="img"
    >
      <span aria-hidden className="text-[7px] font-bold leading-none text-background">
        {advisor.status === 'complete'
          ? '✓'
          : advisor.status === 'failed'
            ? '×'
            : advisor.status === 'interrupted'
              ? '–'
              : advisor.status === 'skipped'
                ? '/'
                : ''}
      </span>
    </span>
  )
}

function AdvisorDetail({ advisor }: { advisor: MoaAdvisorState }) {
  const content = advisor.output?.trim()

  if (!content) {
    return (
      <div className="flex items-center gap-2 py-1 text-xs text-muted-foreground">
        <AdvisorMarker advisor={advisor} />
        <span className="min-w-0 flex-1 truncate">{advisor.label}</span>
        <span className="capitalize">{advisor.status}</span>
      </div>
    )
  }

  return (
    <details className="group/advisor py-1 text-xs">
      <summary className="flex cursor-pointer list-none items-center gap-2 text-muted-foreground marker:content-none">
        <AdvisorMarker advisor={advisor} />
        <span className="min-w-0 flex-1 truncate">{advisor.label}</span>
        <span className="capitalize">{advisor.status}</span>
        <span aria-hidden>›</span>
      </summary>
      <div className="mt-1 whitespace-pre-wrap break-words pl-[18px] text-muted-foreground/85">{content}</div>
    </details>
  )
}

export function MoaOrchestration({ state }: { state: MoaProgressState }) {
  const settled = state.advisors.filter(advisor => SETTLED.has(advisor.status)).length
  const failed = state.advisors.filter(advisor => advisor.status === 'failed').length
  const interrupted = state.advisors.filter(advisor => advisor.status === 'interrupted').length
  const total = state.advisors.length
  const duration = state.settledAt ? Math.max(0, state.settledAt - state.startedAt) : null
  const active = state.phase !== 'settled'
  const phaseCopy =
    state.phase === 'reference'
      ? state.guidanceReused
        ? 'guidance reused'
        : `${settled}/${total} advisors in parallel → aggregator waiting`
      : state.phase === 'aggregating'
        ? `${total}/${total} advisors${state.guidanceReused ? ' · guidance reused' : ''} → aggregator acting`
        : `${total} advisors${state.guidanceReused ? ' · guidance reused' : ''} → ${state.aggregator || 'aggregator'}${duration === null ? '' : ` · ${formatElapsed(duration)}`}`
  const accessible = `Mixture of Agents. ${phaseCopy}.${failed ? ` ${failed} failed.` : ''}${interrupted ? ` ${interrupted} interrupted.` : ''}`

  return (
    <details className="group/moa my-0.5 text-[length:var(--conversation-tool-font-size)] text-(--ui-text-tertiary)">
      <summary
        aria-label={accessible}
        className="flex cursor-pointer list-none items-center gap-2 rounded-sm py-0.5 marker:content-none focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
      >
        <span aria-live={active ? 'polite' : 'off'} className="sr-only" role="status">
          {accessible}
        </span>
        {active ? (
          <ThinkingOrb
            aria-label={state.phase === 'aggregating' ? 'Aggregator combining advisor guidance' : 'Reference advisors reasoning'}
            className="shrink-0"
            size={20}
            state={state.phase === 'aggregating' ? 'weaving' : state.guidanceReused ? 'breathing' : 'solving'}
          />
        ) : (
          <span aria-hidden className="inline-flex size-5 items-center justify-center text-emerald-500">
            ✓
          </span>
        )}
        <span className="font-medium text-foreground/80">MoA</span>
        {!state.guidanceReused && state.phase === 'reference' ? (
          <span className="hidden text-muted-foreground sm:inline">parallel</span>
        ) : null}
        <span className="min-w-0 flex-1 truncate">{phaseCopy}</span>
        <span aria-hidden className="flex shrink-0 gap-1">
          {state.advisors.map(advisor => (
            <AdvisorMarker advisor={advisor} key={advisor.index} />
          ))}
        </span>
        <span aria-hidden className="transition-transform group-open/moa:rotate-90">
          ›
        </span>
      </summary>
      <div className="ml-7 mt-1 border-l border-border/60 pl-2">
        <div className="pb-1 text-[11px] text-muted-foreground">
          {state.guidanceReused ? `Reused ${state.fanout} guidance` : `Fan-out cadence: ${state.fanout}`}
        </div>
        {state.advisors.map(advisor => (
          <AdvisorDetail advisor={advisor} key={advisor.index} />
        ))}
        <div className="flex items-center gap-2 border-t border-border/50 py-1 text-xs text-muted-foreground">
          <span className={cn('size-2.5 rounded-full', state.phase === 'reference' ? 'border border-muted-foreground/45' : 'bg-primary')} />
          <span className="min-w-0 flex-1 truncate">{state.aggregator || 'Aggregator'}</span>
          <span>{state.phase === 'reference' ? 'waiting' : state.phase === 'aggregating' ? 'acting' : 'complete'}</span>
        </div>
      </div>
    </details>
  )
}
