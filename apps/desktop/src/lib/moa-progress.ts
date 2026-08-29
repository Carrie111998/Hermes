import type { GatewayEventPayload } from '@/lib/chat-messages'

export const MOA_PROGRESS_PREFIX = '\u001ehermes-moa-v1:'

export type MoaAdvisorStatus = 'complete' | 'failed' | 'interrupted' | 'queued' | 'running' | 'skipped'
export type MoaPhase = 'aggregating' | 'reference' | 'settled'

export interface MoaAdvisorState {
  index: number
  label: string
  output?: string
  status: MoaAdvisorStatus
}

export interface MoaProgressState {
  advisors: MoaAdvisorState[]
  aggregator: string
  concurrency: number
  fanout: string
  guidanceReused: boolean
  phase: MoaPhase
  startedAt: number
  settledAt?: number
}

function advisorStatus(value: unknown): MoaAdvisorStatus {
  return ['complete', 'failed', 'interrupted', 'queued', 'running', 'skipped'].includes(String(value))
    ? (value as MoaAdvisorStatus)
    : 'complete'
}

export function reduceMoaProgress(
  previous: MoaProgressState | undefined,
  eventType: string,
  payload: GatewayEventPayload | undefined,
  occurredAt: number
): MoaProgressState | undefined {
  if (eventType === 'moa.phase' && payload?.phase === 'reference' && Array.isArray(payload.advisors)) {
    const concurrency = Math.max(0, Math.min(Number(payload.concurrency) || 0, payload.advisors.length))
    const reused = payload.guidance_reused === true

    return {
      advisors: payload.advisors.map((label, offset) => ({
        index: offset + 1,
        label: String(label),
        status: reused ? 'complete' : offset < concurrency ? 'running' : 'queued'
      })),
      aggregator: String(payload.aggregator || ''),
      concurrency,
      fanout: String(payload.fanout || 'user_turn'),
      guidanceReused: reused,
      phase: 'reference',
      startedAt: occurredAt
    }
  }

  if (!previous) {
    return undefined
  }

  if (eventType === 'moa.progress') {
    const index = typeof payload?.index === 'number' ? payload.index : 0
    const fallbackIndex = previous.advisors.findIndex(
      advisor => advisor.label === payload?.label && (advisor.status === 'queued' || advisor.status === 'running')
    )
    const targetIndex = index > 0 ? index - 1 : fallbackIndex

    if (targetIndex < 0 || targetIndex >= previous.advisors.length) {
      return previous
    }

    const advisors = previous.advisors.map((advisor, offset) =>
      offset === targetIndex ? { ...advisor, status: advisorStatus(payload?.status) } : advisor
    )
    const running = advisors.filter(advisor => advisor.status === 'running').length
    let available = previous.concurrency - running

    for (let i = 0; i < advisors.length && available > 0; i++) {
      if (advisors[i].status === 'queued') {
        advisors[i] = { ...advisors[i], status: 'running' }
        available -= 1
      }
    }

    return { ...previous, advisors }
  }

  if (eventType === 'moa.reference') {
    const index = typeof payload?.index === 'number' ? payload.index - 1 : -1

    if (index < 0 || index >= previous.advisors.length) {
      return previous
    }

    return {
      ...previous,
      advisors: previous.advisors.map((advisor, offset) =>
        offset === index
          ? {
              ...advisor,
              label: String(payload?.label || advisor.label),
              output: String(payload?.text || ''),
              status: advisor.status === 'running' || advisor.status === 'queued' ? 'complete' : advisor.status
            }
          : advisor
      )
    }
  }

  if (eventType === 'moa.phase' && payload?.phase === 'aggregator') {
    return {
      ...previous,
      aggregator: String(payload.aggregator || previous.aggregator),
      guidanceReused: payload.guidance_reused === true || previous.guidanceReused,
      phase: 'aggregating'
    }
  }

  if (eventType === 'message.complete') {
    return { ...previous, phase: 'settled', settledAt: occurredAt }
  }

  return previous
}

export function serializeMoaProgress(state: MoaProgressState): string {
  return `${MOA_PROGRESS_PREFIX}${JSON.stringify(state)}`
}

export function parseMoaProgress(text: string): MoaProgressState | null {
  if (!text.startsWith(MOA_PROGRESS_PREFIX)) {
    return null
  }

  try {
    const parsed = JSON.parse(text.slice(MOA_PROGRESS_PREFIX.length)) as MoaProgressState

    return Array.isArray(parsed.advisors) && typeof parsed.phase === 'string' ? parsed : null
  } catch {
    return null
  }
}
