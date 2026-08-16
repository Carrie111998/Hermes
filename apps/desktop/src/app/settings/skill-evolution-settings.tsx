import { useQuery } from '@tanstack/react-query'
import { useCallback, useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Progress } from '@/components/ui/progress'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import { Tip } from '@/components/ui/tooltip'
import {
  approveSkillEvolutionProposal,
  getSkillEvolutionDetail,
  getSkillEvolutionOverview,
  getSkillEvolutionProposals,
  proposeSkillImprovement,
  recordSkillOutcomeManual,
  rejectSkillEvolutionProposal,
  type SkillEvolutionDetail,
  type SkillEvolutionOverview,
  type SkillEvolutionProposal,
  type SkillEvolutionSkill
} from '@/hermes'
import { useI18n } from '@/i18n'
import { triggerHaptic } from '@/lib/haptics'
import {
  Activity,
  AlertTriangle,
  BarChart3,
  Brain,
  Check,
  ChevronDown,
  ChevronRight,
  Loader2,
  Plus,
  RefreshCw,
  X
} from '@/lib/icons'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'
import {
  EmptyState,
  ListRow,
  ListRowSkeleton,
  Pill,
  SettingsContent,
  SettingsSection
} from './primitives'

// ── Shared helpers ────────────────────────────────────────────────────────
function utilityColor(score: number | null): string {
  if (score === null) return 'text-muted-foreground'
  if (score >= 0.7) return 'text-emerald-500'
  if (score >= 0.4) return 'text-amber-500'
  return 'text-red-500'
}

function utilityFillClass(score: number | null): string {
  if (score === null) return 'bg-muted'
  return score >= 0.7 ? 'bg-emerald-500' : score >= 0.4 ? 'bg-amber-500' : 'bg-red-500'
}

function healthColor(score: number): string {
  if (score >= 70) return 'text-emerald-500'
  if (score >= 40) return 'text-amber-500'
  return 'text-red-500'
}

function formatTime(iso: string | null | undefined): string {
  if (!iso) return '—'
  try {
    return new Date(iso).toLocaleString()
  } catch {
    return '—'
  }
}

function StatCard({
  label,
  value,
  sub,
  accent,
  trend
}: {
  label: string
  value: string
  sub: string
  accent?: string
  trend?: React.ReactNode
}) {
  return (
    <div className="rounded-xl border bg-card p-4 transition-colors hover:border-primary/30">
      <div className="text-[11px] uppercase tracking-wide text-muted-foreground">{label}</div>
      <div className={cn('mt-1 text-2xl font-semibold tabular-nums', accent)}>{value}</div>
      <div className="mt-0.5 flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <span>{sub}</span>
        {trend}
      </div>
    </div>
  )
}

/** Small inline trend badge: ▲/▼/＝ vs last week. */
function TrendBadge({ current, previous }: { current: number; previous: number }) {
  const { t } = useI18n()
  if (current === previous)
    return <span className="text-muted-foreground/60">＝ {t.settings.skillEvolution.trendFlat}</span>
  if (current > previous)
    return <span className="text-emerald-500">▲ {t.settings.skillEvolution.trendUp}</span>
  return <span className="text-red-500">▼ {t.settings.skillEvolution.trendDown}</span>
}

/** Stacked utility-distribution bar (low/mid/high). */
function DistributionBar({ dist }: { dist: SkillEvolutionOverview['utility_distribution'] }) {
  const { t } = useI18n()
  const total = dist.low + dist.mid + dist.high || 1
  const segs = [
    { key: 'low', n: dist.low, cls: 'bg-red-500', label: t.settings.skillEvolution.distributionLow },
    { key: 'mid', n: dist.mid, cls: 'bg-amber-500', label: t.settings.skillEvolution.distributionMid },
    { key: 'high', n: dist.high, cls: 'bg-emerald-500', label: t.settings.skillEvolution.distributionHigh }
  ]
  return (
    <div>
      <div className="flex h-2 w-full overflow-hidden rounded-full bg-muted">
        {segs.map(s => (
          <div
            key={s.key}
            className={s.cls}
            style={{ width: `${(s.n / total) * 100}%` }}
            title={`${s.label}: ${s.n}`}
          />
        ))}
      </div>
      <div className="mt-1.5 flex justify-between text-[10px] text-muted-foreground">
        {segs.map(s => (
          <span key={s.key}>
            <span className="mr-1 inline-block h-1.5 w-1.5 rounded-full align-middle" style={{ backgroundColor: s.cls.replace('bg-', '') === 'red-500' ? '#ef4444' : s.cls.replace('bg-', '') === 'amber-500' ? '#f59e0b' : '#10b981' }} />
            {s.label} · {s.n}
          </span>
        ))}
      </div>
    </div>
  )
}

/** Mini sparkline of recent outcomes (success=green, failure=red, unknown=grey). */
function OutcomeSparkline({ skill }: { skill: SkillEvolutionSkill }) {
  const { t } = useI18n()
  const dots = useMemo(() => {
    const arr: { cls: string }[] = []
    for (let i = 0; i < skill.success_count; i++) arr.push({ cls: 'bg-emerald-500' })
    for (let i = 0; i < skill.failure_count; i++) arr.push({ cls: 'bg-red-500' })
    for (let i = 0; i < skill.unknown_count; i++) arr.push({ cls: 'bg-muted-foreground/30' })
    return arr.slice(-12)
  }, [skill])

  return (
    <Tip label={t.settings.skillEvolution.recentTrend}>
      <div className="flex items-center gap-0.5">
        {dots.length === 0 ? (
          <span className="text-[9px] text-muted-foreground/50">—</span>
        ) : (
          dots.map((dot, i) => (
            <span key={i} className={cn('h-1.5 w-1.5 rounded-full', dot.cls)} />
          ))
        )}
      </div>
    </Tip>
  )
}

// ── Overview cards + health + distribution ───────────────────────────────
function OverviewCards({ data }: { data: SkillEvolutionOverview }) {
  const { t } = useI18n()
  const tr = data.trends
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
      <StatCard
        label={t.settings.skillEvolution.healthScore}
        value={String(data.health_score)}
        sub={t.settings.skillEvolution.healthDesc}
        accent={healthColor(data.health_score)}
      />
      <StatCard
        label={t.settings.skillEvolution.skillsTracked}
        value={String(data.skills_total)}
        sub={`${data.skills_scored} ${t.settings.skillEvolution.scoredSub}`}
      />
      <StatCard
        label={t.settings.skillEvolution.pendingProposals}
        value={String(data.proposals_pending)}
        sub={`${data.proposals_total} ${t.settings.skillEvolution.totalSub}`}
        accent={data.proposals_pending > 0 ? 'text-amber-500' : undefined}
        trend={<TrendBadge current={tr.proposals_this_week} previous={tr.proposals_last_week} />}
      />
      <StatCard
        label={t.settings.skillEvolution.lowUtilitySkills}
        value={String(data.low_utility_candidates.length)}
        sub={t.settings.skillEvolution.lowUtilitySub}
        accent={data.low_utility_candidates.length > 0 ? 'text-red-500' : undefined}
      />
    </div>
  )
}

// ── Skill row ─────────────────────────────────────────────────────────────
function SkillRow({
  skill,
  onSelect
}: {
  skill: SkillEvolutionSkill
  onSelect?: (skill: string) => void
}) {
  const { t } = useI18n()
  const score = skill.utility_score
  const pct = score === null ? 0 : Math.round(score * 100)

  return (
    <ListRow
      title={
        <button
          type="button"
          className="flex min-w-0 items-center gap-2 text-left group"
          onClick={() => onSelect?.(skill.skill)}
        >
          <span className="truncate font-mono text-xs group-hover:underline">{skill.skill}</span>
          <Pill tone={skill.state === 'archived' ? 'muted' : 'primary'}>{skill.state ?? 'active'}</Pill>
          <ChevronRight className="size-3 shrink-0 text-muted-foreground/40 transition-transform group-hover:translate-x-0.5 group-hover:text-muted-foreground" />
        </button>
      }
      description={
        <div className="flex items-center gap-2">
          <Progress
            aria-label={t.settings.skillEvolution.utility}
            className="w-24"
            fillClassName={utilityFillClass(score)}
            size="sm"
            value={score === null ? 0 : score}
          />
          <span className={cn('text-xs font-semibold tabular-nums', utilityColor(score))}>
            {score === null ? t.settings.skillEvolution.noSignal : `${pct}%`}
          </span>
        </div>
      }
      action={
        <div className="flex shrink-0 items-center gap-4 text-right text-[10px] leading-tight text-muted-foreground">
          <OutcomeSparkline skill={skill} />
          <Tip label={`${t.settings.skillEvolution.successCount}: ${skill.success_count}`}>
            <div>
              <div className="text-emerald-500">{skill.success_count}✓</div>
              <div className="text-red-500">{skill.failure_count}✗</div>
            </div>
          </Tip>
          <Tip label={`${t.settings.skillEvolution.useCount}: ${skill.use_count} · ${t.settings.skillEvolution.patchCount}: ${skill.patch_count}`}>
            <div>
              <div>{skill.use_count}×</div>
              <div>{skill.patch_count}✎</div>
            </div>
          </Tip>
        </div>
      }
      wide
    />
  )
}

// ── Proposal card with expand/collapse + status ──────────────────────────
function ProposalCard({
  proposal,
  onAction
}: {
  proposal: SkillEvolutionProposal
  onAction: () => void
}) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const [expanded, setExpanded] = useState(false)
  const status = proposal.status ?? 'pending'

  const act = useCallback(
    async (kind: 'approve' | 'reject') => {
      setBusy(true)
      try {
        if (kind === 'approve') {
          await approveSkillEvolutionProposal(proposal.skill, proposal.proposal_id)
        } else {
          await rejectSkillEvolutionProposal(proposal.skill, proposal.proposal_id)
        }
        triggerHaptic(kind === 'approve' ? 'success' : 'warning')
        onAction()
      } catch (err) {
        notifyError(err, t.settings.skillEvolution.actionFailed)
      } finally {
        setBusy(false)
      }
    },
    [onAction, proposal, t]
  )

  const statusTone =
    status === 'applied' ? 'primary' : status === 'rejected' ? 'warn' : status === 'reviewed' ? 'muted' : 'warn'
  const statusLabels: Record<string, string> = {
    pending: t.settings.skillEvolution.statusPending,
    reviewed: t.settings.skillEvolution.statusReviewed,
    applied: t.settings.skillEvolution.statusApplied,
    rejected: t.settings.skillEvolution.statusRejected
  }
  const statusLabel = statusLabels[status] ?? status
  const longDiagnosis = (proposal.diagnosis ?? '').length > 120

  return (
    <div className="rounded-xl border bg-card transition-colors hover:border-primary/30">
      <div className="flex items-start justify-between gap-3 p-4">
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <span className="font-mono text-xs font-semibold">{proposal.skill}</span>
            <Pill tone={statusTone}>{statusLabel}</Pill>
            <Pill tone="primary">{proposal.heading ?? proposal.target_section}</Pill>
            {proposal.utility_score !== null && proposal.utility_score !== undefined && (
              <Pill tone={proposal.utility_score >= 0.4 ? 'muted' : 'warn'}>
                {t.settings.skillEvolution.utility} {Math.round(proposal.utility_score * 100)}%
              </Pill>
            )}
          </div>
          <p className={cn('mt-1.5 text-sm text-muted-foreground', !expanded && longDiagnosis && 'line-clamp-2')}>
            {proposal.diagnosis}
          </p>
          {longDiagnosis && (
            <button
              type="button"
              className="mt-1 flex items-center gap-0.5 text-[11px] text-muted-foreground hover:text-foreground"
              onClick={() => setExpanded(v => !v)}
            >
              {expanded ? <ChevronDown className="size-3" /> : <ChevronRight className="size-3" />}
              {expanded ? t.settings.skillEvolution.collapse : t.settings.skillEvolution.expand}
            </button>
          )}
          {proposal.failure_types && proposal.failure_types.length > 0 && (
            <div className="mt-1.5 flex flex-wrap gap-1">
              {proposal.failure_types.slice(0, 4).map(ft => (
                <span
                  key={ft}
                  className="rounded bg-muted px-1.5 py-0.5 font-mono text-[10px] text-muted-foreground"
                >
                  {ft}
                </span>
              ))}
            </div>
          )}
          <div className="mt-2.5 rounded-lg bg-muted/40 p-2.5 text-xs leading-relaxed">
            <span className="font-medium text-foreground">{t.settings.skillEvolution.suggestedFix}:</span>{' '}
            <span className="text-muted-foreground">{proposal.suggested_fix}</span>
          </div>
        </div>
      </div>
      <div className="flex items-center justify-between border-t px-4 py-2.5">
        <span className="text-[10px] text-muted-foreground">{formatTime(proposal.created_at)}</span>
        {status === 'pending' ? (
          <div className="flex gap-2">
            <Button
              aria-label={t.settings.skillEvolution.reject}
              size="sm"
              variant="ghost"
              disabled={busy}
              onClick={() => void act('reject')}
            >
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <X className="size-3.5" />}
              {t.settings.skillEvolution.reject}
            </Button>
            <Button
              aria-label={t.settings.skillEvolution.approve}
              size="sm"
              disabled={busy}
              onClick={() => void act('approve')}
            >
              {busy ? <Loader2 className="size-3.5 animate-spin" /> : <Check className="size-3.5" />}
              {t.settings.skillEvolution.approve}
            </Button>
          </div>
        ) : (
          <span className="text-[10px] text-muted-foreground">{statusLabel}</span>
        )}
      </div>
    </div>
  )
}

// ── Skill detail sheet (ESC + scroll-lock via Sheet) ─────────────────────
function SkillDetail({ skill, onClose }: { skill: string; onClose: () => void }) {
  const { t } = useI18n()
  const [busy, setBusy] = useState(false)
  const detail = useQuery({
    queryKey: ['skill-evolution-detail', skill],
    queryFn: () => getSkillEvolutionDetail(skill)
  })
  const [proposed, setProposed] = useState(false)

  const propose = useCallback(async () => {
    setBusy(true)
    try {
      const r = await proposeSkillImprovement(skill)
      if (r.ok) {
        triggerHaptic('success')
        setProposed(true)
      } else if (r.reason === 'already_pending') {
        notifyError(new Error('already_pending'), t.settings.skillEvolution.alreadyPending)
      } else {
        notifyError(new Error('propose_failed'), t.settings.skillEvolution.actionFailed)
      }
    } catch (err) {
      notifyError(err, t.settings.skillEvolution.actionFailed)
    } finally {
      setBusy(false)
    }
  }, [skill, t])

  const report = useCallback(
    async (outcome: 'success' | 'failure') => {
      setBusy(true)
      try {
        await recordSkillOutcomeManual(skill, outcome)
        triggerHaptic('success')
        detail.refetch()
      } catch (err) {
        notifyError(err, t.settings.skillEvolution.actionFailed)
      } finally {
        setBusy(false)
      }
    },
    [skill, detail]
  )

  const d: SkillEvolutionDetail | undefined = detail.data
  const score = d?.utility_score ?? null

  return (
    <Sheet open onOpenChange={open => !open && onClose()}>
      <SheetContent side="right" className="w-full max-w-md overflow-y-auto p-5">
        <SheetHeader>
          <SheetTitle className="flex items-center gap-2 font-mono text-sm">
            {skill}
            {score !== null && (
              <Pill tone={score >= 0.4 ? 'muted' : 'warn'}>
                {t.settings.skillEvolution.utility} {Math.round(score * 100)}%
              </Pill>
            )}
          </SheetTitle>
          <SheetDescription>{t.settings.skillEvolution.viewDetails}</SheetDescription>
        </SheetHeader>

        {detail.isLoading ? (
          <div className="mt-4 space-y-2">
            <ListRowSkeleton />
            <ListRowSkeleton />
          </div>
        ) : d ? (
          <>
            {/* Summary stats (i18n labels) */}
            <div className="mt-4 grid grid-cols-3 gap-2">
              <StatCard label={t.settings.skillEvolution.successCount} value={String(d.summary.success_count)} sub="✓" />
              <StatCard label={t.settings.skillEvolution.failureCount} value={String(d.summary.failure_count)} sub="✗" />
              <StatCard label={t.settings.skillEvolution.useCount} value={String(d.summary.use_count)} sub="×" />
            </div>

            {/* Failure patterns */}
            {d.failure_patterns && (d.failure_patterns.top_error_types?.length ?? 0) > 0 && (
              <div className="mt-5">
                <SectionHeadingSmall icon={AlertTriangle} title={t.settings.skillEvolution.topFailures} />
                <div className="space-y-1.5">
                  {(d.failure_patterns.top_error_types ?? []).slice(0, 5).map(([et, count]) => (
                    <div key={et} className="flex items-center justify-between rounded-lg bg-muted/40 px-3 py-2 text-xs">
                      <span className="font-mono">{et}</span>
                      <span className="text-muted-foreground">{count}×</span>
                    </div>
                  ))}
                </div>
                {d.failure_patterns.failure_rate !== undefined && d.failure_patterns.failure_rate > 0 && (
                  <p className="mt-1.5 text-[11px] text-muted-foreground">
                    {t.settings.skillEvolution.failureRate}: {Math.round(d.failure_patterns.failure_rate * 100)}%
                  </p>
                )}
              </div>
            )}

            {/* Actions */}
            <div className="mt-5 flex flex-col gap-2">
              <Button size="sm" variant="outline" disabled={busy || proposed} onClick={() => void propose()}>
                <Plus className="mr-1.5 size-3.5" />
                {proposed ? t.settings.skillEvolution.proposed : t.settings.skillEvolution.generateProposal}
              </Button>
              <div className="flex gap-2">
                <Button size="sm" variant="outline" className="flex-1" disabled={busy} onClick={() => void report('success')}>
                  <Check className="mr-1.5 size-3.5" />
                  {t.settings.skillEvolution.markSuccess}
                </Button>
                <Button
                  size="sm"
                  variant="outline"
                  className="flex-1 text-red-500"
                  disabled={busy}
                  onClick={() => void report('failure')}
                >
                  <X className="mr-1.5 size-3.5" />
                  {t.settings.skillEvolution.markFailure}
                </Button>
              </div>
            </div>

            {/* Proposal history */}
            {d.proposals.length > 0 && (
              <div className="mt-5">
                <SectionHeadingSmall icon={Brain} title={t.settings.skillEvolution.history} />
                <div className="space-y-2">
                  {d.proposals.slice(0, 5).map(p => (
                    <div key={p.proposal_id} className="rounded-lg border bg-card p-3 text-xs">
                      <div className="flex items-center justify-between">
                        <Pill tone={p.status === 'applied' ? 'primary' : p.status === 'rejected' ? 'warn' : 'muted'}>
                          {p.status ?? 'pending'}
                        </Pill>
                        <span className="text-[10px] text-muted-foreground">{formatTime(p.created_at)}</span>
                      </div>
                      <p className="mt-1.5 text-muted-foreground">{p.diagnosis}</p>
                    </div>
                  ))}
                </div>
              </div>
            )}
          </>
        ) : (
          <EmptyState title={t.settings.skillEvolution.loadFailed} />
        )}
      </SheetContent>
    </Sheet>
  )
}

function SectionHeadingSmall({ icon: Icon, title }: { icon: typeof Activity; title: string }) {
  return (
    <div className="mb-2 flex items-center gap-2 text-sm font-medium">
      <Icon className="size-4 shrink-0 text-muted-foreground" />
      <span>{title}</span>
    </div>
  )
}

// ── Main page ────────────────────────────────────────────────────────────
type ProposalFilter = 'pending' | 'applied' | 'rejected' | 'all'

const TOP_SKILLS_LIMIT = 10

export function SkillEvolutionSettings() {
  const { t } = useI18n()
  const [refreshKey, setRefreshKey] = useState(0)
  const [filter, setFilter] = useState<ProposalFilter>('pending')
  const [showAllTop, setShowAllTop] = useState(false)
  const [selectedSkill, setSelectedSkill] = useState<string | null>(null)
  const refresh = useCallback(() => setRefreshKey(k => k + 1), [])

  const overview = useQuery({
    queryKey: ['skill-evolution-overview', refreshKey],
    queryFn: getSkillEvolutionOverview
  })
  const proposals = useQuery({
    queryKey: ['skill-evolution-proposals', filter, refreshKey],
    queryFn: () =>
      getSkillEvolutionProposals(filter === 'pending', filter === 'all' ? undefined : filter)
  })

  const reload = useCallback(() => refresh(), [refresh])
  const loading = overview.isLoading || proposals.isLoading
  const error = overview.error ?? proposals.error

  const pendingCount = overview.data?.proposals_pending ?? 0
  const topSkills = overview.data?.top_skills ?? []
  const visibleTopSkills = showAllTop ? topSkills : topSkills.slice(0, TOP_SKILLS_LIMIT)

  const filterOptions = [
    { id: 'pending' as const, label: t.settings.skillEvolution.filterPending },
    { id: 'applied' as const, label: t.settings.skillEvolution.filterApplied },
    { id: 'rejected' as const, label: t.settings.skillEvolution.filterRejected },
    { id: 'all' as const, label: t.settings.skillEvolution.filterAll }
  ]

  return (
    <SettingsContent>
      {/* Header */}
      <SettingsSection
        icon={Activity}
        meta={loading ? undefined : String(pendingCount)}
        title={t.settings.skillEvolution.title}
        aside={
          <Button
            aria-label={t.settings.skillEvolution.refresh}
            size="sm"
            variant="ghost"
            onClick={reload}
            disabled={loading}
          >
            {loading ? <Loader2 className="size-3.5 animate-spin" /> : <RefreshCw className="size-3.5" />}
            {t.settings.skillEvolution.refresh}
          </Button>
        }
      >
        <p className="text-xs text-muted-foreground">{t.settings.skillEvolution.blurb}</p>
      </SettingsSection>

      {/* Overview stats */}
      {overview.data && <OverviewCards data={overview.data} />}

      {/* Utility distribution */}
      {overview.data && (overview.data.utility_distribution.low + overview.data.utility_distribution.mid + overview.data.utility_distribution.high) > 0 && (
        <div className="mt-6">
          <SettingsSection icon={BarChart3} title={t.settings.skillEvolution.distribution}>
            <DistributionBar dist={overview.data.utility_distribution} />
          </SettingsSection>
        </div>
      )}

      {/* Proposals with filter tabs */}
      <div className="mt-8">
        <SettingsSection
          icon={Brain}
          title={t.settings.skillEvolution.proposalsQueue}
          aside={
            <SegmentedControl
              options={filterOptions}
              value={filter}
              onChange={setFilter}
              className="hidden sm:flex"
            />
          }
        >
          <SegmentedControl
            options={filterOptions}
            value={filter}
            onChange={setFilter}
            className="mb-3 flex sm:hidden"
          />
          {proposals.isLoading ? (
            <div className="space-y-3">
              <ListRowSkeleton />
              <ListRowSkeleton />
            </div>
          ) : proposals.error ? (
            <EmptyState title={t.settings.skillEvolution.loadFailed} />
          ) : proposals.data && proposals.data.proposals.length > 0 ? (
            <div className="space-y-3">
              {proposals.data.proposals.map(p => (
                <ProposalCard key={p.proposal_id} proposal={p} onAction={reload} />
              ))}
            </div>
          ) : (
            <EmptyState
              title={t.settings.skillEvolution.noProposals}
              description={
                filter === 'pending'
                  ? t.settings.skillEvolution.noProposalsDesc
                  : t.settings.skillEvolution.noHistoryDesc
              }
            />
          )}
        </SettingsSection>
      </div>

      {/* Top skills (limit + show more) */}
      {overview.data && topSkills.length > 0 && (
        <div className="mt-8">
          <SettingsSection
            icon={BarChart3}
            title={t.settings.skillEvolution.topSkills}
            aside={
              topSkills.length > TOP_SKILLS_LIMIT ? (
                <Button size="sm" variant="ghost" onClick={() => setShowAllTop(v => !v)}>
                  {showAllTop ? t.settings.skillEvolution.showLess : t.settings.skillEvolution.showMore}
                </Button>
              ) : undefined
            }
          >
            <div className="divide-y">
              {visibleTopSkills.map(s => (
                <SkillRow key={s.skill} skill={s} onSelect={setSelectedSkill} />
              ))}
            </div>
          </SettingsSection>
        </div>
      )}

      {/* Skills needing attention */}
      {overview.data && overview.data.low_utility_candidates.length > 0 && (
        <div className="mt-8">
          <SettingsSection
            icon={AlertTriangle}
            title={t.settings.skillEvolution.attentionSkills}
            aside={<Pill tone="warn">{overview.data.low_utility_candidates.length}</Pill>}
          >
            <div className="divide-y">
              {overview.data.low_utility_candidates.slice(0, 8).map(c => (
                <ListRow
                  key={c.skill}
                  title={
                    <button
                      type="button"
                      className="font-mono text-xs hover:underline"
                      onClick={() => setSelectedSkill(c.skill)}
                    >
                      {c.skill}
                    </button>
                  }
                  description={
                    <span className="text-xs">
                      {c.failure_count}✗ / {c.success_count}✓
                    </span>
                  }
                  action={
                    <span className="text-xs font-semibold tabular-nums text-red-500">
                      {Math.round(c.utility_score * 100)}%
                    </span>
                  }
                  wide
                />
              ))}
            </div>
          </SettingsSection>
        </div>
      )}

      {/* Error fallback */}
      {error && !loading && (
        <div className="mt-8">
          <SettingsSection icon={AlertTriangle} title={t.settings.skillEvolution.loadFailed}>
            <EmptyState
              title={t.settings.skillEvolution.loadFailed}
              description={String(error instanceof Error ? error.message : error)}
            />
            <div className="flex justify-center pt-2">
              <Button size="sm" variant="outline" onClick={reload}>
                <RefreshCw className="mr-1.5 size-3.5" />
                {t.settings.skillEvolution.retry}
              </Button>
            </div>
          </SettingsSection>
        </div>
      )}

      {/* Skill detail sheet */}
      {selectedSkill && <SkillDetail skill={selectedSkill} onClose={() => setSelectedSkill(null)} />}
    </SettingsContent>
  )
}
