import type { StarmapGraph } from '@/types/hermes'

export function LearningCandidates({ graph }: { graph: StarmapGraph }) {
  const candidates = graph.candidates ?? []

  if (candidates.length === 0) {
    return null
  }

  return (
    <section aria-label="Learning candidates" className="max-h-48 shrink-0 overflow-y-auto border-b border-border/60 bg-background/95 p-3">
      <div className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        Evidence ledger · {candidates.length} candidate{candidates.length === 1 ? '' : 's'}
      </div>
      <div className="grid gap-2 sm:grid-cols-2 xl:grid-cols-3">
        {candidates.map(candidate => (
          <article className="rounded-md border border-border/70 bg-muted/30 p-2" key={candidate.id}>
            <div className="flex items-center justify-between gap-2 text-sm font-medium">
              <span className="truncate">{candidate.summary}</span>
              <span className="shrink-0 rounded bg-background px-1.5 py-0.5 text-[11px] text-muted-foreground">
                {candidate.status}
              </span>
            </div>
            <div className="mt-1 text-xs text-muted-foreground">
              {candidate.subsystem} · {candidate.action} · risk {candidate.risk}
            </div>
          </article>
        ))}
      </div>
    </section>
  )
}
