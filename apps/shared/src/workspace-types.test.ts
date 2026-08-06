import { describe, expect, it } from 'vitest'

import {
  isPushApprovalCurrent,
  isWorkspaceRunTransitionAllowed,
  type PushApprovalDecision,
  type PushRequest,
  reduceWorkspaceRunEvent,
  WORKSPACE_SCHEMA_VERSION,
  type WorkspaceRunEvent,
  type WorkspaceRunProjection
} from './workspace-types'

const run = (overrides: Partial<WorkspaceRunProjection> = {}): WorkspaceRunProjection => ({
  lastEventId: undefined,
  runId: 'run-1',
  state: 'queued',
  lastSequence: 0,
  syncStatus: 'current',
  ...overrides
})

const event = (
  sequence: number,
  state: WorkspaceRunProjection['state']
): WorkspaceRunEvent => ({
  schemaVersion: WORKSPACE_SCHEMA_VERSION,
  eventId: `event-${sequence}`,
  projectId: 'project-1',
  runId: 'run-1',
  attemptId: 'attempt-1',
  sequence,
  occurredAt: '2026-08-05T00:00:00.000Z',
  type: 'run.state_changed',
  payload: { state }
})

describe('workspace run contract', () => {
  it('allows only declared run transitions', () => {
    expect(isWorkspaceRunTransitionAllowed('queued', 'offered')).toBe(true)
    expect(isWorkspaceRunTransitionAllowed('running', 'uncertain')).toBe(true)
    expect(isWorkspaceRunTransitionAllowed('queued', 'completed')).toBe(false)
    expect(isWorkspaceRunTransitionAllowed('completed', 'running')).toBe(false)
  })

  it('applies ordered events and preserves reference identity for duplicates', () => {
    const offered = reduceWorkspaceRunEvent(run(), event(1, 'offered'))
    const duplicate = reduceWorkspaceRunEvent(offered, event(1, 'offered'))

    expect(offered).toEqual({
      lastEventId: 'event-1',
      runId: 'run-1',
      state: 'offered',
      lastSequence: 1,
      syncStatus: 'current'
    })
    expect(duplicate).toBe(offered)
  })

  it('marks a projection as needing replay when an event sequence has a gap', () => {
    const projection = reduceWorkspaceRunEvent(run(), event(2, 'offered'))

    expect(projection).toEqual({
      lastEventId: undefined,
      runId: 'run-1',
      state: 'queued',
      lastSequence: 0,
      syncStatus: 'needs_replay'
    })
  })

  it('rejects a state event for a different run', () => {
    const wrongRun = { ...event(1, 'offered'), runId: 'run-2' }

    expect(() => reduceWorkspaceRunEvent(run(), wrongRun)).toThrow(/run-2/)
  })

  it('marks a conflicting event at an applied sequence for replay', () => {
    const current = run({ lastEventId: 'event-1', lastSequence: 1, state: 'offered' })
    const conflict = { ...event(1, 'accepted'), eventId: 'event-conflict' }

    expect(reduceWorkspaceRunEvent(current, conflict)).toEqual({
      ...current,
      syncStatus: 'needs_replay'
    })
  })
})

describe('push approval contract', () => {
  const request: PushRequest = {
    id: 'push-1',
    runId: 'run-1',
    commitSha: 'abc123',
    diffDigest: 'sha256:diff',
    remote: 'origin',
    remoteUrl: 'https://github.com/example/project.git',
    remoteUrlDigest: 'url-digest',
    destinationRef: 'refs/heads/hermes/task-1',
    expiresAt: '2026-08-06T00:00:00.000Z'
  }

  const approval: PushApprovalDecision = {
    requestId: request.id,
    approved: true,
    commitSha: request.commitSha,
    diffDigest: request.diffDigest,
    remote: request.remote,
    remoteUrl: request.remoteUrl,
    remoteUrlDigest: request.remoteUrlDigest,
    destinationRef: request.destinationRef,
    decidedAt: '2026-08-05T12:00:00.000Z'
  }

  it('accepts an unexpired approval only for the exact current push snapshot', () => {
    expect(isPushApprovalCurrent({
      approval,
      currentCommitSha: request.commitSha,
      currentDiffDigest: request.diffDigest,
      now: '2026-08-05T13:00:00.000Z',
      request
    })).toBe(true)
  })

  it('invalidates approval after the diff changes or the request expires', () => {
    expect(isPushApprovalCurrent({
      approval,
      currentCommitSha: request.commitSha,
      currentDiffDigest: 'sha256:changed',
      now: '2026-08-05T13:00:00.000Z',
      request
    })).toBe(false)

    expect(isPushApprovalCurrent({
      approval,
      currentCommitSha: request.commitSha,
      currentDiffDigest: request.diffDigest,
      now: request.expiresAt,
      request
    })).toBe(false)
  })
})
