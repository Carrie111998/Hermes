export const WORKSPACE_SCHEMA_VERSION = 1 as const

export type WorkspaceSchemaVersion = typeof WORKSPACE_SCHEMA_VERSION
export type WorkspaceId = string
export type ProjectId = string
export type RunnerId = string
export type BindingId = string
export type ConversationId = string
export type WorktreeId = string
export type TaskId = string
export type RunId = string
export type AttemptId = string
export type AssetId = string

export interface LogicalWorkspaceProject {
  id: ProjectId
  workspaceId: WorkspaceId
  name: string
  repositoryIdentity?: {
    normalizedRemoteHash?: string
    fingerprint?: string
  }
  createdAt: string
  archivedAt?: string
}

/** Shared metadata for a device-local binding. Absolute paths stay runner-local. */
export interface WorkspaceBinding {
  id: BindingId
  projectId: ProjectId
  runnerId: RunnerId
  displayLabel: string
  repositoryFingerprint?: string
  capabilities: Array<'read' | 'write' | 'execute' | 'git_mutate' | 'publish'>
  availability: 'online' | 'offline' | 'revoked'
  lastSeenAt?: string
}

export interface ConversationLane {
  id: ConversationId
  projectId: ProjectId
  runnerId: RunnerId
  bindingId: BindingId
  worktreeId?: WorktreeId
  baseRef: string
  baseSha: string
  branch: string
  state: 'ready' | 'provisioning' | 'active' | 'review' | 'blocked' | 'archived'
  writerLease?: {
    holderRunId: RunId
    fencingToken: number
    expiresAt: string
  }
}

export type WorkspaceRunState =
  | 'queued'
  | 'waiting_for_device'
  | 'offered'
  | 'accepted'
  | 'resolving_context'
  | 'preparing_workspace'
  | 'running'
  | 'awaiting_approval'
  | 'awaiting_review'
  | 'uncertain'
  | 'reconciling'
  | 'completed'
  | 'failed'
  | 'canceled'

export type WorkspaceRunSyncStatus = 'current' | 'needs_replay'

export interface WorkspaceRunProjection {
  lastEventId?: string
  runId: RunId
  state: WorkspaceRunState
  lastSequence: number
  syncStatus: WorkspaceRunSyncStatus
}

export interface WorkspaceRunSpec {
  schemaVersion: WorkspaceSchemaVersion
  workspaceId: WorkspaceId
  projectId: ProjectId
  taskId: TaskId
  runId: RunId
  attemptId: AttemptId
  conversationId: ConversationId
  runnerId: RunnerId
  bindingId: BindingId
  bindingRelativePath: string
  baseSha: string
  deadline: string
  idempotencyKey: string
  cancellationToken: string
  fencingToken: number
}

export interface WorkspaceRunEvent {
  schemaVersion: WorkspaceSchemaVersion
  eventId: string
  projectId: ProjectId
  runId: RunId
  attemptId: AttemptId
  sequence: number
  occurredAt: string
  type: 'run.state_changed'
  payload: {
    state: WorkspaceRunState
  }
}

const RUN_TRANSITIONS: Readonly<Record<WorkspaceRunState, ReadonlySet<WorkspaceRunState>>> = {
  queued: new Set(['waiting_for_device', 'offered', 'canceled']),
  waiting_for_device: new Set(['offered', 'canceled']),
  offered: new Set(['queued', 'waiting_for_device', 'accepted', 'failed', 'canceled']),
  accepted: new Set(['resolving_context', 'preparing_workspace', 'failed', 'canceled']),
  resolving_context: new Set(['preparing_workspace', 'awaiting_approval', 'failed', 'canceled']),
  preparing_workspace: new Set(['running', 'awaiting_approval', 'failed', 'canceled']),
  running: new Set(['awaiting_approval', 'awaiting_review', 'completed', 'failed', 'canceled', 'uncertain']),
  awaiting_approval: new Set(['running', 'awaiting_review', 'failed', 'canceled', 'uncertain']),
  awaiting_review: new Set(['completed', 'failed', 'canceled']),
  uncertain: new Set(['reconciling', 'failed', 'canceled']),
  reconciling: new Set(['running', 'awaiting_approval', 'awaiting_review', 'completed', 'failed', 'canceled', 'uncertain']),
  completed: new Set(),
  failed: new Set(),
  canceled: new Set()
}

export function isWorkspaceRunTransitionAllowed(
  from: WorkspaceRunState,
  to: WorkspaceRunState
): boolean {
  return RUN_TRANSITIONS[from].has(to)
}

export function reduceWorkspaceRunEvent(
  projection: WorkspaceRunProjection,
  event: WorkspaceRunEvent
): WorkspaceRunProjection {
  if (event.schemaVersion !== WORKSPACE_SCHEMA_VERSION) {
    throw new Error(`Unsupported workspace event schema ${event.schemaVersion}`)
  }

  if (event.runId !== projection.runId) {
    throw new Error(`Workspace event for ${event.runId} cannot update ${projection.runId}`)
  }

  if (!Number.isSafeInteger(event.sequence) || event.sequence < 1) {
    throw new Error(`Invalid workspace event sequence ${event.sequence}`)
  }

  const nextState = event.payload.state

  if (event.sequence === projection.lastSequence) {
    if (event.eventId === projection.lastEventId && nextState === projection.state) {
      return projection
    }

    return projection.syncStatus === 'needs_replay'
      ? projection
      : { ...projection, syncStatus: 'needs_replay' }
  }

  if (event.sequence < projection.lastSequence) {
    return projection.syncStatus === 'needs_replay'
      ? projection
      : { ...projection, syncStatus: 'needs_replay' }
  }

  if (event.sequence !== projection.lastSequence + 1) {
    if (projection.syncStatus === 'needs_replay') {
      return projection
    }

    return { ...projection, syncStatus: 'needs_replay' }
  }

  if (!isWorkspaceRunTransitionAllowed(projection.state, nextState)) {
    throw new Error(`Invalid workspace run transition ${projection.state} -> ${nextState}`)
  }

  return {
    lastEventId: event.eventId,
    runId: projection.runId,
    state: nextState,
    lastSequence: event.sequence,
    syncStatus: 'current'
  }
}

export interface PushRequest {
  id: string
  runId: RunId
  commitSha: string
  diffDigest: string
  remote: string
  remoteUrl: string
  remoteUrlDigest: string
  destinationRef: string
  expiresAt: string
}

export interface PushApprovalDecision {
  requestId: string
  approved: boolean
  commitSha: string
  diffDigest: string
  remote: string
  remoteUrl: string
  remoteUrlDigest: string
  destinationRef: string
  decidedAt: string
}

export interface PushApprovalCheck {
  request: PushRequest
  approval: PushApprovalDecision
  currentCommitSha: string
  currentDiffDigest: string
  now: string
}

export function isPushApprovalCurrent({
  request,
  approval,
  currentCommitSha,
  currentDiffDigest,
  now
}: PushApprovalCheck): boolean {
  const expiresAt = Date.parse(request.expiresAt)
  const currentTime = Date.parse(now)

  if (!Number.isFinite(expiresAt) || !Number.isFinite(currentTime) || currentTime >= expiresAt) {
    return false
  }

  return approval.approved &&
    approval.requestId === request.id &&
    approval.commitSha === request.commitSha &&
    approval.diffDigest === request.diffDigest &&
    approval.remote === request.remote &&
    approval.remoteUrl === request.remoteUrl &&
    approval.remoteUrlDigest === request.remoteUrlDigest &&
    approval.destinationRef === request.destinationRef &&
    currentCommitSha === request.commitSha &&
    currentDiffDigest === request.diffDigest
}

export interface ContextReference {
  id: string
  projectId: ProjectId
  runId: RunId
  source: 'notion' | 'slack' | 'repository' | 'web'
  remoteObjectId: string
  sourceUrl?: string
  title: string
  excerpt: string
  contentHash: string
  observedAt: string
}

export interface WorkspaceAsset {
  id: AssetId
  projectId: ProjectId
  contentHash: string
  mediaType: string
  byteSize: number
  storageKey: string
  lifecycle: 'staged' | 'retained' | 'project_saved' | 'deleted'
  createdAt: string
  parentAssetId?: AssetId
  generation?: {
    provider: string
    model: string
    prompt: string
    parameters: Record<string, unknown>
  }
}

export interface LearningCandidate {
  id: string
  projectId: ProjectId
  originatingRunId: RunId
  destination: 'memory' | 'skill' | 'project_knowledge'
  scope: 'project' | 'profile' | 'global'
  evidenceReferenceIds: string[]
  proposal: string
  risk: 'low' | 'medium' | 'high'
  state: 'proposed' | 'sanitized' | 'evaluating' | 'rejected' | 'approved' | 'canary' | 'promoted' | 'rolled_back' | 'quarantined'
  createdAt: string
  expiresAt?: string
}
