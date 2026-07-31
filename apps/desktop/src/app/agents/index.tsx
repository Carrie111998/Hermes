import { useStore } from '@nanostores/react'
import { type ReactNode, useEffect, useMemo, useState } from 'react'

import { useElapsedSeconds } from '@/components/chat/activity-timer'
import { ActivityTimerText } from '@/components/chat/activity-timer-text'
import { Codicon } from '@/components/ui/codicon'
import { FadeText } from '@/components/ui/fade-text'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { getCronJobs, getProfiles } from '@/hermes'
import { type Translations, useI18n } from '@/i18n'
import { compactNumber } from '@/lib/format'
import { AlertCircle, CheckCircle2 } from '@/lib/icons'
import { asText } from '@/lib/text'
import { useEnterAnimation } from '@/lib/use-enter-animation'
import { cn } from '@/lib/utils'
import {
  $subagentsBySession,
  allSubagents,
  buildSubagentTree,
  type SubagentNode,
  type SubagentStatus,
  type SubagentStreamEntry
} from '@/store/subagents'
import type { CronJob, ProfileInfo } from '@/types/hermes'

import { jobState, jobTitle, STATE_DOT } from '../cron/job-state'
import { Panel, PanelEmpty, PanelHeader } from '../overlays/panel'

// Mirrors statusGlyph() in tool-fallback.tsx so subagent rows speak the
// same visual vocabulary as the chat tool blocks.
function statusGlyph(status: SubagentStatus, a: Translations['agents']): ReactNode {
  if (status === 'running' || status === 'queued') {
    return (
      <GlyphSpinner
        ariaLabel={a.running}
        className="size-3.5 shrink-0 text-[0.95rem] text-muted-foreground/80"
        spinner="breathe"
      />
    )
  }

  if (status === 'failed' || status === 'interrupted') {
    return <AlertCircle aria-label={a.failed} className="size-3.5 shrink-0 text-destructive" />
  }

  return <CheckCircle2 aria-label={a.done} className="size-3.5 shrink-0 text-emerald-600/85 dark:text-emerald-400/85" />
}

const STREAM_TONE: Record<SubagentStreamEntry['kind'], string> = {
  progress: 'text-muted-foreground/75',
  summary: 'text-foreground/85',
  thinking: 'text-muted-foreground/80',
  tool: 'text-foreground/85'
}

function streamGlyph(entry: SubagentStreamEntry): ReactNode {
  if (entry.isError) {
    return <AlertCircle aria-hidden className="mt-0.5 size-3 shrink-0 text-destructive" />
  }

  if (entry.kind === 'tool') {
    return <span aria-hidden className="mt-0.5 size-1.5 shrink-0 rounded-full bg-foreground/55" />
  }

  if (entry.kind === 'summary') {
    return <CheckCircle2 aria-hidden className="mt-0.5 size-3 shrink-0 text-emerald-600/85 dark:text-emerald-400/85" />
  }

  if (entry.kind === 'thinking') {
    return (
      <span aria-hidden className="font-mono text-[0.7rem] leading-none text-muted-foreground/70">
        …
      </span>
    )
  }

  return <span aria-hidden className="mt-0.5 size-1 shrink-0 rounded-full bg-muted-foreground/55" />
}

interface AgentsViewProps {
  onClose: () => void
}

export function AgentsView({ onClose }: AgentsViewProps) {
  const { t } = useI18n()
  const subagentsBySession = useStore($subagentsBySession)

  // Aggregate every session, matching the status-bar indicator — a subagent
  // running in a background session must still be visible here, or the two
  // desync ("Agents N running" vs an empty tree).
  const tree = useMemo(() => buildSubagentTree(allSubagents(subagentsBySession)), [subagentsBySession])

  return (
    <Panel closeLabel={t.agents.close} onClose={onClose}>
      <PanelHeader subtitle={t.agents.localBoardSubtitle} title={t.agents.title} />
      <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-6 overflow-hidden">
        <LocalAgentsBoard />
        {tree.length === 0 ? (
          <section className="shrink-0 rounded-xl border border-border/60 bg-muted/15 p-4">
            <PanelEmpty description={t.agents.emptyDesc} icon="hubot" title={t.agents.emptyTitle} />
          </section>
        ) : (
          <section className="flex min-h-0 min-w-0 flex-1 flex-col gap-3 overflow-hidden">
            <div className="flex shrink-0 items-center gap-2">
              <Codicon className="text-muted-foreground/75" name="hubot" size="1rem" />
              <h3 className="text-sm font-semibold text-foreground/90">{t.agents.liveSubagentsTitle}</h3>
            </div>
            <SubagentTree tree={tree} />
          </section>
        )}
      </div>
    </Panel>
  )
}

function formatDateTime(iso?: null | string): string {
  if (!iso) {
    return '—'
  }

  const date = new Date(iso)

  return Number.isNaN(date.valueOf()) ? iso : date.toLocaleString()
}

function scheduleText(job: CronJob): string {
  return asText(job.schedule_display) || asText(job.schedule?.display) || asText(job.schedule?.expr) || '—'
}

function jobPrompt(job: CronJob): string {
  return asText(job.prompt).trim()
}

function jobDeliver(job: CronJob, a: Translations['agents']): string {
  return asText(job.deliver).trim() || a.deliveryLocal
}

function modelText(job: CronJob, a: Translations['agents']): string {
  return [asText(job.provider).trim(), asText(job.model).trim()].filter(Boolean).join('/') || a.defaultModel
}

type CronJobViewKind =
  | 'attention'
  | 'driver'
  | 'inbox'
  | 'revenue'
  | 'sentinel'
  | 'trading'
  | 'watchdog'
  | 'default'

const CUSTOM_JOB_VIEW_ACCENT: Record<Exclude<CronJobViewKind, 'default'>, string> = {
  attention: 'border-sky-500/35 bg-sky-500/10 text-sky-700 dark:text-sky-300',
  driver: 'border-violet-500/35 bg-violet-500/10 text-violet-700 dark:text-violet-300',
  inbox: 'border-cyan-500/35 bg-cyan-500/10 text-cyan-700 dark:text-cyan-300',
  revenue: 'border-emerald-500/35 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300',
  sentinel: 'border-amber-500/35 bg-amber-500/10 text-amber-700 dark:text-amber-300',
  trading: 'border-orange-500/35 bg-orange-500/10 text-orange-700 dark:text-orange-300',
  watchdog: 'border-rose-500/35 bg-rose-500/10 text-rose-700 dark:text-rose-300'
}

function jobViewKind(job: CronJob): CronJobViewKind {
  const text = [job.name, job.prompt, job.script, job.skill, ...(job.skills ?? [])].filter(Boolean).join(' ').toLowerCase()

  if (text.includes('driver loop') || text.includes('active project driver') || text.includes('durable goal loop')) {
    return 'driver'
  }

  if (text.includes('github') && (text.includes('attention') || text.includes('review') || text.includes('pr '))) {
    return 'attention'
  }

  if (text.includes('proton') || text.includes('inbox') || text.includes('email')) {
    return 'inbox'
  }

  if (text.includes('first-dollar') || text.includes('revenue') || text.includes('customer acquisition')) {
    return 'revenue'
  }

  if (text.includes('sentinel') || text.includes('health guard') || text.includes('monitor.sh')) {
    return 'sentinel'
  }

  if (text.includes('polymarket') || text.includes('paper-only') || text.includes('trader')) {
    return 'trading'
  }

  if (text.includes('watchdog') || text.includes('self-heal')) {
    return 'watchdog'
  }

  return 'default'
}

function repeatText(job: CronJob, a: Translations['agents']): string {
  const completed = job.repeat?.completed
  const times = job.repeat?.times

  if (completed === undefined && times === undefined) {
    return ''
  }

  return a.repeatProgress(completed ?? 0, times ?? '∞')
}

function LocalAgentsBoard() {
  const { t } = useI18n()
  const a = t.agents
  const [profiles, setProfiles] = useState<ProfileInfo[]>([])
  const [selected, setSelected] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<null | string>(null)

  useEffect(() => {
    let cancelled = false

    setLoading(true)
    setError(null)
    void getProfiles()
      .then(result => {
        if (cancelled) {
          return
        }

        const rows = [...(result.profiles ?? [])].sort((a, b) =>
          a.is_default === b.is_default ? a.name.localeCompare(b.name) : a.is_default ? -1 : 1
        )

        setProfiles(rows)
        setSelected(prev => prev ?? rows[0]?.name ?? null)
      })
      .catch(err => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
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
  }, [])

  const selectedProfile = profiles.find(profile => profile.name === selected) ?? profiles[0] ?? null

  return (
    <section className="grid min-h-[18rem] shrink-0 gap-4 lg:grid-cols-[minmax(15rem,0.7fr)_minmax(0,1.3fr)]">
      <div className="flex min-h-0 min-w-0 flex-col rounded-xl border border-border/70 bg-muted/10 p-3">
        <div className="mb-3 flex items-center justify-between gap-2">
          <div>
            <h3 className="text-sm font-semibold text-foreground/90">{a.localAgentsTitle}</h3>
            <p className="text-[0.7rem] text-muted-foreground/70">{a.localAgentsDesc}</p>
          </div>
          {loading ? <GlyphSpinner ariaLabel={a.loadingLocalAgents} className="size-3.5" spinner="breathe" /> : null}
        </div>

        {error ? <p className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">{error}</p> : null}

        <div className="grid min-h-0 gap-2 overflow-y-auto pr-1">
          {profiles.map(profile => (
            <button
              className={cn(
                'grid min-w-0 gap-1 rounded-lg border p-3 text-left transition-colors',
                selectedProfile?.name === profile.name
                  ? 'border-primary/45 bg-primary/10 text-foreground'
                  : 'border-border/55 bg-background/35 text-foreground/85 hover:border-border hover:bg-muted/25'
              )}
              key={profile.name}
              onClick={() => setSelected(profile.name)}
              type="button"
            >
              <span className="flex min-w-0 items-center gap-2">
                <Codicon className="text-muted-foreground/75" name={profile.is_default ? 'home' : 'account'} size="0.9rem" />
                <span className="truncate text-sm font-medium">{profile.name}</span>
                {profile.is_default ? (
                  <span className="rounded-full bg-muted px-1.5 py-0.5 text-[0.58rem] uppercase tracking-wide text-muted-foreground">
                    {a.defaultBadge}
                  </span>
                ) : null}
              </span>
              <span className="truncate text-[0.68rem] text-muted-foreground/75">
                {[profile.provider, profile.model].filter(Boolean).join('/') || a.modelInheritsDefault}
              </span>
              <span className="text-[0.64rem] text-muted-foreground/65">{a.skillsAndPath(profile.skill_count, profile.path)}</span>
            </button>
          ))}

          {!loading && profiles.length === 0 ? (
            <p className="rounded-md border border-border/60 p-3 text-xs text-muted-foreground/75">{a.noLocalProfiles}</p>
          ) : null}
        </div>
      </div>

      <AgentDetail profile={selectedProfile} />
    </section>
  )
}

function AgentDetail({ profile }: { profile: ProfileInfo | null }) {
  const { t } = useI18n()
  const a = t.agents
  const [jobs, setJobs] = useState<CronJob[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<null | string>(null)

  useEffect(() => {
    if (!profile) {
      setJobs([])

      return
    }

    let cancelled = false

    setLoading(true)
    setError(null)
    void getCronJobs(profile.name)
      .then(rows => {
        if (!cancelled) {
          setJobs(rows ?? [])
        }
      })
      .catch(err => {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : String(err))
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
  }, [profile])

  if (!profile) {
    return (
      <div className="grid place-items-center rounded-xl border border-border/70 bg-muted/10 p-6 text-center text-sm text-muted-foreground/75">
        {a.selectAgent}
      </div>
    )
  }

  const sorted = [...jobs].sort((a, b) => jobTitle(a).localeCompare(jobTitle(b)))

  return (
    <div className="flex min-h-0 min-w-0 flex-col rounded-xl border border-border/70 bg-background/35 p-4">
      <div className="mb-4 flex shrink-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <p className="text-[0.66rem] font-medium uppercase tracking-wider text-muted-foreground/65">{a.agentDetail}</p>
          <h3 className="truncate text-lg font-semibold text-foreground">{profile.name}</h3>
          <p className="truncate text-xs text-muted-foreground/75">{profile.path}</p>
        </div>
        {loading ? <GlyphSpinner ariaLabel={a.loadingAgentJobs} className="size-4" spinner="breathe" /> : null}
      </div>

      <div className="mb-4 grid gap-2 sm:grid-cols-3">
        <DetailStat label={a.statModel} value={[profile.provider, profile.model].filter(Boolean).join('/') || a.defaultModel} />
        <DetailStat label={a.statSkills} value={String(profile.skill_count)} />
        <DetailStat label={a.statJobs} value={String(sorted.length)} />
      </div>

      {error ? <p className="mb-3 rounded-md border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">{error}</p> : null}

      <div className="min-h-0 flex-1 overflow-y-auto pr-1">
        {sorted.length > 0 ? (
          <div className="grid gap-3">
            {sorted.map(job => (
              <CronJobDefaultCard job={job} key={job.id} />
            ))}
          </div>
        ) : !loading ? (
          <div className="rounded-lg border border-dashed border-border/70 p-5 text-center">
            <Codicon className="mx-auto mb-2 text-muted-foreground/60" name="watch" size="1.3rem" />
            <p className="text-sm font-medium text-foreground/85">{a.noCronJobsTitle}</p>
            <p className="mt-1 text-xs text-muted-foreground/70">
              {a.noCronJobsDesc(profile.name)}
            </p>
          </div>
        ) : null}
      </div>
    </div>
  )
}

function DetailStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-lg border border-border/55 bg-muted/10 p-2">
      <p className="text-[0.6rem] uppercase tracking-wide text-muted-foreground/60">{label}</p>
      <p className="truncate text-xs font-medium text-foreground/85">{value}</p>
    </div>
  )
}

function CronJobDefaultCard({ job }: { job: CronJob }) {
  const { t } = useI18n()
  const a = t.agents
  const state = jobState(job)
  const prompt = jobPrompt(job)
  const flags = [job.no_agent ? a.modeScriptOnly : '', job.script ? a.modeScript : ''].filter(Boolean)
  const custom = jobViewKind(job)
  const customView = custom === 'default' ? null : a.jobViews[custom]
  const customAccent = custom === 'default' ? '' : CUSTOM_JOB_VIEW_ACCENT[custom]

  const meta = [
    repeatText(job, a),
    asText(job.last_status) ? a.metaLastStatus(asText(job.last_status)) : '',
    job.enabled_toolsets?.length ? a.metaToolsets(job.enabled_toolsets.join(', ')) : '',
    job.skills?.length ? a.metaSkills(job.skills.join(', ')) : '',
    asText(job.workdir) ? a.metaWorkdir(asText(job.workdir)) : ''
  ].filter(Boolean)

  return (
    <article className="grid min-w-0 gap-3 rounded-lg border border-border/60 bg-muted/10 p-3">
      <div className="flex min-w-0 items-start justify-between gap-3">
        <div className="min-w-0">
          <h4 className="truncate text-sm font-semibold text-foreground/90">{jobTitle(job)}</h4>
          <p className="truncate text-[0.68rem] text-muted-foreground/70">{job.id}</p>
        </div>
        <span className="inline-flex shrink-0 items-center gap-1 rounded-full border border-border/55 bg-background/60 px-2 py-1 text-[0.65rem] text-muted-foreground">
          <span className={cn('size-1.5 rounded-full', STATE_DOT[state] ?? STATE_DOT.enabled)} />
          {state}
        </span>
      </div>

      {customView ? (
        <div className={cn('rounded-md border px-3 py-2 text-xs leading-relaxed', customAccent)}>
          <p className="font-semibold">{customView.label}</p>
          <p className="mt-0.5 opacity-85">{customView.description}</p>
        </div>
      ) : null}

      <div className="grid gap-2 sm:grid-cols-2">
        <DetailStat label={a.statSchedule} value={scheduleText(job)} />
        <DetailStat label={a.statNextRun} value={formatDateTime(job.next_run_at)} />
        <DetailStat label={a.statLastRun} value={formatDateTime(job.last_run_at)} />
        <DetailStat label={a.statDelivery} value={jobDeliver(job, a)} />
        <DetailStat label={a.statModel} value={modelText(job, a)} />
        <DetailStat label={a.statMode} value={flags.join(' · ') || a.modeAgent} />
      </div>

      {meta.length > 0 ? <p className="text-[0.68rem] text-muted-foreground/70">{meta.join(' · ')}</p> : null}
      {job.paused_reason ? (
        <p className="rounded-md border border-amber-500/25 bg-amber-500/10 p-2 text-xs text-amber-700 dark:text-amber-300">
          {job.paused_reason}
        </p>
      ) : null}
      {prompt ? <p className="line-clamp-3 text-xs leading-relaxed text-muted-foreground/80">{prompt}</p> : null}
      {job.last_error ? (
        <p className="rounded-md border border-destructive/30 bg-destructive/10 p-2 text-xs text-destructive">
          {job.last_error}
        </p>
      ) : null}
    </article>
  )
}

const fmtDuration = (seconds: number | undefined, a: Translations['agents']) => {
  if (!seconds || seconds <= 0) {
    return ''
  }

  if (seconds < 60) {
    return a.durationSeconds(seconds.toFixed(1))
  }

  const m = Math.floor(seconds / 60)
  const s = Math.round(seconds % 60)

  return a.durationMinutes(m, s)
}

const fmtTokens = (value: number | undefined, a: Translations['agents']) =>
  value ? a.tokens(compactNumber(value)) : ''

// Distinct contract from coarseElapsed: rounds to the second (this ticks live),
// and hours are unbounded ("25h", never "1d"). Kept local on purpose.
const fmtAge = (updatedAt: number, nowMs: number, a: Translations['agents']) => {
  const s = Math.max(0, Math.round((nowMs - updatedAt) / 1000))

  if (s < 2) {
    return a.ageNow
  }

  if (s < 60) {
    return a.ageSeconds(s)
  }

  const m = Math.floor(s / 60)

  return m < 60 ? a.ageMinutes(m) : a.ageHours(Math.floor(m / 60))
}

const flatten = (nodes: readonly SubagentNode[]): SubagentNode[] =>
  nodes.flatMap(node => [node, ...flatten(node.children)])

interface RootGroup {
  id: string
  delegationIndex: number
  nodes: SubagentNode[]
  taskCount: number
}

function groupDelegations(roots: readonly SubagentNode[]): RootGroup[] {
  const groups: RootGroup[] = []
  let n = 0

  for (const node of roots) {
    const prev = groups.at(-1)
    const prevTail = prev?.nodes.at(-1)
    const closeInTime = prevTail ? Math.abs(node.startedAt - prevTail.startedAt) <= 5_000 : false
    const sameShape = prev && node.taskCount > 1 && prev.taskCount === node.taskCount
    const uniqueStep = prev ? !prev.nodes.some(item => item.taskIndex === node.taskIndex) : false

    if (prev && sameShape && closeInTime && uniqueStep) {
      prev.nodes.push(node)

      continue
    }

    if (node.taskCount > 1) {
      n += 1
      groups.push({ id: `delegation-${n}`, delegationIndex: n, nodes: [node], taskCount: node.taskCount })

      continue
    }

    groups.push({ id: node.id, delegationIndex: 0, nodes: [node], taskCount: node.taskCount })
  }

  return groups
}

function SubagentTree({ tree }: { tree: SubagentNode[] }) {
  const { t } = useI18n()
  const flat = useMemo(() => flatten(tree), [tree])
  const groups = useMemo(() => groupDelegations(tree), [tree])
  const [nowMs, setNowMs] = useState(() => Date.now())

  const active = flat.filter(n => n.status === 'running' || n.status === 'queued').length
  const failed = flat.filter(n => n.status === 'failed' || n.status === 'interrupted').length
  const tools = flat.reduce((sum, n) => sum + (n.toolCount ?? 0), 0)
  const files = flat.reduce((sum, n) => sum + n.filesRead.length + n.filesWritten.length, 0)
  const tokens = flat.reduce((sum, n) => sum + (n.inputTokens ?? 0) + (n.outputTokens ?? 0), 0)
  const cost = flat.reduce((sum, n) => sum + (n.costUsd ?? 0), 0)

  useEffect(() => {
    if (active <= 0 || typeof window === 'undefined') {
      return
    }

    const id = window.setInterval(() => setNowMs(Date.now()), 500)

    return () => window.clearInterval(id)
  }, [active])

  if (tree.length === 0) {
    return (
      <div className="grid place-items-center gap-3 py-12 text-center">
        <Codicon className="text-muted-foreground/60" name="hubot" size="1.5rem" />
        <p className="text-sm font-medium text-foreground/90">{t.agents.emptyTitle}</p>
        <p className="max-w-md text-xs leading-relaxed text-muted-foreground/75">{t.agents.emptyDesc}</p>
      </div>
    )
  }

  const summary = [
    t.agents.agentsCount(flat.length),
    active > 0 ? t.agents.activeCount(active) : '',
    failed > 0 ? t.agents.failedCount(failed) : '',
    tools > 0 ? t.agents.toolsCount(tools) : '',
    files > 0 ? t.agents.filesCount(files) : '',
    tokens > 0 ? fmtTokens(tokens, t.agents) : '',
    cost > 0 ? `$${cost.toFixed(2)}` : ''
  ].filter(Boolean)

  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col gap-4 overflow-hidden">
      <p className="shrink-0 text-[0.7rem] text-muted-foreground/70">{summary.join(' · ')}</p>
      <div className="min-h-0 min-w-0 flex-1 overflow-x-hidden overflow-y-auto overscroll-contain pr-1">
        <div className="flex min-w-0 flex-col gap-6">
          {groups.map(group => (
            <DelegationGroup group={group} key={group.id} nowMs={nowMs} />
          ))}
        </div>
      </div>
    </div>
  )
}

function DelegationGroup({ group, nowMs }: { group: RootGroup; nowMs: number }) {
  const { t } = useI18n()

  if (group.nodes.length === 1 && group.taskCount <= 1) {
    return <SubagentRow node={group.nodes[0]!} nowMs={nowMs} />
  }

  const activeWorkers = group.nodes.filter(n => n.status === 'running' || n.status === 'queued').length

  return (
    <section className="grid min-w-0 gap-3">
      <p className="text-[0.66rem] font-medium uppercase tracking-wider text-muted-foreground/70">
        {group.delegationIndex > 0 ? t.agents.delegation(group.delegationIndex) : ''}{' '}
        <span className="text-muted-foreground/50">·</span> {t.agents.workers(group.nodes.length)}
        {activeWorkers > 0 ? <span className="text-primary/85"> · {t.agents.workersActive(activeWorkers)}</span> : null}
      </p>
      <div className="grid min-w-0 gap-4">
        {group.nodes.map(node => (
          <SubagentRow key={node.id} node={node} nowMs={nowMs} />
        ))}
      </div>
    </section>
  )
}

function StreamLine({
  active,
  entry,
  parentRunning,
  rowKey
}: {
  active: boolean
  entry: SubagentStreamEntry
  parentRunning: boolean
  rowKey: string
}) {
  const { t } = useI18n()
  const enterRef = useEnterAnimation(parentRunning, `subagent-stream:${rowKey}`)
  const isMono = entry.kind === 'tool'
  const tone = entry.isError ? 'text-destructive' : STREAM_TONE[entry.kind]

  return (
    <div className="flex min-w-0 items-baseline gap-2 text-[0.72rem] leading-relaxed" ref={enterRef}>
      <span className="flex h-[0.95rem] shrink-0 items-center">{streamGlyph(entry)}</span>
      <span className={cn('min-w-0 flex-1 wrap-anywhere', tone, isMono && 'font-mono text-[0.69rem]')}>
        {entry.text}
        {active ? (
          <GlyphSpinner
            ariaLabel={t.agents.streaming}
            className="ml-1 inline-block size-2.5 align-middle text-muted-foreground/70"
            spinner="breathe"
          />
        ) : null}
      </span>
    </div>
  )
}

function SubagentRow({ node, depth = 0, nowMs }: { node: SubagentNode; depth?: number; nowMs: number }) {
  const { t } = useI18n()
  const running = node.status === 'running' || node.status === 'queued'
  const elapsed = useElapsedSeconds(running, `subagent:${node.id}`)

  const durationSeconds =
    typeof node.durationSeconds === 'number' ? Math.max(0, Math.round(node.durationSeconds)) : elapsed

  const [open, setOpen] = useState(() => running || depth < 2)
  const enterRef = useEnterAnimation(true, `subagent-row:${node.id}`)

  useEffect(() => {
    if (running) {
      setOpen(true)
    }
  }, [running])

  const visibleRows = open ? node.stream.slice(-10) : node.stream.slice(-2)
  const fileLines = [...node.filesWritten.map(p => `+ ${p}`), ...node.filesRead.map(p => `· ${p}`)]

  const subtitle = [
    node.model,
    fmtDuration(durationSeconds, t.agents),
    node.toolCount ? t.agents.toolsCount(node.toolCount) : '',
    fmtTokens((node.inputTokens ?? 0) + (node.outputTokens ?? 0), t.agents),
    t.agents.updatedAgo(fmtAge(node.updatedAt, nowMs, t.agents))
  ].filter(Boolean)

  return (
    <div className={cn('grid min-w-0 max-w-full gap-2', depth > 0 && 'pl-4')} data-slot="tool-block" ref={enterRef}>
      <button
        aria-expanded={open}
        className="group flex w-full min-w-0 items-start gap-2.5 text-left"
        onClick={() => setOpen(v => !v)}
        type="button"
      >
        <span className="mt-0.5 flex h-[1.1rem] shrink-0 items-center">{statusGlyph(node.status, t.agents)}</span>
        <span className="flex min-w-0 flex-1 flex-col gap-0.5">
          <span
            className={cn(
              'wrap-anywhere text-[0.82rem] font-medium leading-[1.1rem] text-foreground/90 transition-colors group-hover:text-foreground',
              running && 'shimmer text-foreground/65'
            )}
          >
            {node.goal}
          </span>
          {subtitle.length > 0 ? (
            <FadeText className="text-[0.66rem] leading-[1.05rem] text-muted-foreground/65">
              {subtitle.join(' · ')}
            </FadeText>
          ) : null}
        </span>
        {running ? <ActivityTimerText className="mt-1 shrink-0 text-[0.6rem]" seconds={durationSeconds} /> : null}
      </button>

      {visibleRows.length > 0 ? (
        <div className="grid min-w-0 gap-1 pl-6" data-selectable-text="true">
          {visibleRows.map((entry, i) => (
            <StreamLine
              active={running && i === visibleRows.length - 1}
              entry={entry}
              key={`${entry.kind}:${entry.at}:${i}`}
              parentRunning={running}
              rowKey={`${node.id}:${entry.kind}:${entry.at}`}
            />
          ))}
        </div>
      ) : null}

      {open && fileLines.length > 0 ? (
        <div className="grid min-w-0 gap-0.5 pl-6" data-selectable-text="true">
          <p className="text-[0.58rem] font-medium tracking-wider text-muted-foreground/60 uppercase">
            {t.agents.files}
          </p>
          {fileLines.slice(0, 8).map(line => (
            <p className="wrap-break-word font-mono text-[0.67rem] leading-relaxed text-muted-foreground/80" key={line}>
              {line}
            </p>
          ))}
          {fileLines.length > 8 ? (
            <p className="font-mono text-[0.67rem] leading-relaxed text-muted-foreground/65">
              {t.agents.moreFiles(fileLines.length - 8)}
            </p>
          ) : null}
        </div>
      ) : null}

      {node.children.length > 0 ? (
        <div className="grid min-w-0 gap-3 pl-6">
          {node.children.map(child => (
            <SubagentRow depth={depth + 1} key={child.id} node={child} nowMs={nowMs} />
          ))}
        </div>
      ) : null}
    </div>
  )
}
