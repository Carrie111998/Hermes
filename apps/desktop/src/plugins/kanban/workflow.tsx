/**
 * Workflow v1 remote projection. GitHub owns product state; this page renders
 * the remote Hermes controller overlay and exposes only typed controller
 * operations. It intentionally has no generic task/card mutation surface.
 */

import {
  Button,
  cn,
  Codicon,
  ErrorState,
  host,
  Input,
  Loader,
  useMutation,
  useQuery,
  useQueryClient,
  useValue
} from '@hermes/plugin-sdk'
import { useState } from 'react'

import {
  $boardSlug,
  fetchWorkflow,
  pauseWorkflow,
  resumeWorkflow,
  workflowKey
} from './api'
import type {
  WorkflowControllerResponse,
  WorkflowLeafProjection,
  WorkflowProjection
} from './types'
import { errText } from './ui'

function short(value?: null | string, length = 12): string {
  return value ? value.slice(0, length) : '—'
}

function timeLabel(value?: null | number): string {
  return value ? new Date(value * 1000).toLocaleString() : 'never'
}

function StatePill({ label, tone }: { label: string; tone: 'bad' | 'good' | 'neutral' | 'warn' }) {
  return (
    <span
      className={cn(
        'rounded-full border px-2 py-0.5 text-[0.625rem] font-medium uppercase tracking-wide',
        tone === 'good' && 'border-emerald-500/30 bg-emerald-500/10 text-emerald-400',
        tone === 'warn' && 'border-amber-500/30 bg-amber-500/10 text-amber-300',
        tone === 'bad' && 'border-red-500/30 bg-red-500/10 text-red-400',
        tone === 'neutral' && 'border-(--ui-border) bg-(--ui-control-background) text-(--ui-text-secondary)'
      )}
    >
      {label}
    </span>
  )
}

function LeafCard({ leaf }: { leaf: WorkflowLeafProjection }) {
  const issue = leaf.canonical.campaign_issue
  const repo = leaf.canonical.repository

  return (
    <article className="rounded-md border border-(--ui-border) bg-(--ui-panel-background) p-3">
      <div className="flex flex-wrap items-start gap-2">
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-xs font-medium text-foreground">{leaf.title}</h2>
          <p className="mt-0.5 truncate font-mono text-[0.625rem] text-(--ui-text-tertiary)">{leaf.leaf_key}</p>
        </div>
        <StatePill label={leaf.status} tone={leaf.status === 'blocked' ? 'bad' : leaf.status === 'running' ? 'good' : 'neutral'} />
      </div>

      <div className="mt-3 grid gap-2 text-[0.6875rem] text-(--ui-text-secondary) sm:grid-cols-2 xl:grid-cols-4">
        <div>
          <div className="text-(--ui-text-quaternary)">GitHub authority</div>
          <div className="truncate font-mono">{repo && issue ? `${repo}#${issue}` : 'unresolved'}</div>
        </div>
        <div>
          <div className="text-(--ui-text-quaternary)">Specification</div>
          <div className="font-mono">{leaf.specification_version ?? '—'} · {short(leaf.spec_hash)}</div>
        </div>
        <div>
          <div className="text-(--ui-text-quaternary)">Pinned commit</div>
          <div className="font-mono">{short(leaf.pin_sha)}</div>
        </div>
        <div>
          <div className="text-(--ui-text-quaternary)">Dependencies</div>
          <div>{leaf.dependencies.length ? leaf.dependencies.map(dep => `${dep.id}:${dep.status}`).join(', ') : 'none'}</div>
        </div>
      </div>

      {leaf.current_run && (
        <div className="mt-3 flex flex-wrap gap-x-4 gap-y-1 border-t border-(--ui-border) pt-2 font-mono text-[0.625rem] text-(--ui-text-tertiary)">
          <span>run {leaf.current_run.id}</span>
          <span>fence {leaf.current_run.fence_digest}</span>
          <span>lease {timeLabel(leaf.current_run.claim_expires_at)}</span>
          <span>evidence {timeLabel(leaf.current_run.last_evidence_at)}</span>
        </div>
      )}
    </article>
  )
}

export function WorkflowPage() {
  const slug = useValue($boardSlug)
  const key = workflowKey(slug)
  const qc = useQueryClient()
  const [reason, setReason] = useState('')

  // Refetch on mount/focus and every five seconds. No query data is persisted;
  // reopening the laptop reconstructs state from the remote server.
  const query = useQuery({
    queryFn: fetchWorkflow,
    queryKey: key,
    refetchInterval: 5_000,
    refetchOnMount: 'always',
    refetchOnWindowFocus: 'always'
  })

  const control = useMutation({
    mutationFn: ({ operation, version, why }: { operation: 'pause' | 'resume'; version: number; why: string }) =>
      operation === 'pause' ? pauseWorkflow(version, why) : resumeWorkflow(version, why),
    // Never park an operator mutation for replay after reconnect. Attempt once;
    // if the remote server is unreachable, fail visibly and require a fresh read.
    networkMode: 'always',
    retry: false,
    onError: err => host.notify({ kind: 'error', message: errText(err) }),
    onSuccess: (response: WorkflowControllerResponse) => {
      qc.setQueryData<WorkflowProjection>(key, current =>
        current ? { ...current, controller: response.controller } : current
      )
      setReason('')
      void qc.invalidateQueries({ queryKey: key })
    }
  })

  const projection = query.data
  const controller = projection?.controller

  const stale =
    !projection ||
    !controller?.heartbeat_at ||
    projection.server_time - controller.heartbeat_at > 60

  const controlReason = reason.trim()

  const mutate = (operation: 'pause' | 'resume') => {
    if (!controller || !controlReason) {
      return
    }

    control.mutate({ operation, version: controller.version, why: controlReason })
  }

  if (query.error && !projection) {
    return (
      <div className="grid h-full place-items-center">
        <ErrorState title={errText(query.error)} />
      </div>
    )
  }

  if (!projection || !controller) {
    return (
      <div className="grid h-full place-items-center">
        <Loader type="lemniscate-bloom" />
      </div>
    )
  }

  return (
    <div className="flex h-full min-h-0 flex-col overflow-hidden bg-(--ui-background)">
      <header className="border-b border-(--ui-border) px-4 py-3">
        <div className="flex flex-wrap items-start gap-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <Codicon name="server-process" size="0.9rem" />
              <h1 className="text-sm font-semibold">Workflow</h1>
              <StatePill label={stale ? 'remote stale' : controller.status} tone={stale ? 'bad' : controller.status === 'healthy' ? 'good' : 'warn'} />
              <StatePill label={controller.dispatch_enabled ? 'dispatch enabled' : 'dispatch paused'} tone={controller.dispatch_enabled ? 'warn' : 'neutral'} />
              {!controller.broker_ready && <StatePill label="broker gated" tone="neutral" />}
            </div>
            <p className="mt-1 text-[0.6875rem] text-(--ui-text-tertiary)">
              GitHub is canonical. Hermes remote server owns controller runtime. Closing Desktop changes nothing.
            </p>
          </div>
          <div className="text-right font-mono text-[0.625rem] text-(--ui-text-quaternary)">
            <div>board {projection.board}</div>
            <div>epoch {short(controller.controller_epoch)}</div>
            <div>heartbeat {timeLabel(controller.heartbeat_at)}</div>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <Input
            aria-label="Workflow control reason"
            className="min-w-64 flex-1"
            onChange={event => setReason(event.target.value)}
            placeholder="Reason required for audited control"
            value={reason}
          />
          <Button
            disabled={control.isPending || !controlReason || !controller.dispatch_enabled}
            onClick={() => mutate('pause')}
            size="sm"
            variant="outline"
          >
            <Codicon name="debug-pause" size="0.75rem" />
            Pause
          </Button>
          <Button
            disabled={control.isPending || !controlReason || controller.dispatch_enabled || !controller.broker_ready}
            onClick={() => mutate('resume')}
            size="sm"
          >
            <Codicon name="play" size="0.75rem" />
            Resume
          </Button>
        </div>
        {!controller.broker_ready && (
          <p className="mt-1.5 text-[0.625rem] text-(--ui-text-quaternary)">
            Resume is unavailable until the least-privilege worker broker is independently verified.
          </p>
        )}
        {query.error && (
          <p className="mt-1.5 text-[0.625rem] text-red-400">
            Refresh failed; controls use the last version and will be rejected if stale: {errText(query.error)}
          </p>
        )}
      </header>

      <main className="min-h-0 flex-1 overflow-y-auto p-4">
        {projection.leaves.length ? (
          <div className="grid gap-3 xl:grid-cols-2">
            {projection.leaves.map(leaf => <LeafCard key={leaf.leaf_key} leaf={leaf} />)}
          </div>
        ) : (
          <div className="grid h-full place-items-center text-center">
            <div>
              <Codicon className="text-(--ui-text-quaternary)" name="git-pull-request" size="1.25rem" />
              <p className="mt-2 text-xs text-(--ui-text-tertiary)">No protected Workflow leaves on this board.</p>
            </div>
          </div>
        )}
      </main>
    </div>
  )
}
