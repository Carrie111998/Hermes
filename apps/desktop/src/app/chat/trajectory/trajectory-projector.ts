import type { ChatMessage, ChatMessagePart } from '@/lib/chat-messages'

export type TrajectoryRecordKind = 'assistant' | 'reasoning' | 'tool' | 'user'
export type TrajectoryRecordStatus = 'complete' | 'error' | 'running'

export interface TrajectoryRecord {
  id: string
  kind: TrajectoryRecordKind
  turn: number
  step: number | null
  text: string
  startedAt: number | null
  completedAt: number | null
  durationMs: number | null
  status: TrajectoryRecordStatus
  callId?: string
  name?: string
  payload?: unknown
  result?: unknown
}

export interface TrajectoryProjection {
  model: string
  provider: string
  summary: {
    turns: number
    steps: number
    toolCalls: number
    errors: number
    durationMs: number
  }
  records: TrajectoryRecord[]
}

function partText(part: ChatMessagePart): string {
  if (part.type === 'text' || part.type === 'reasoning') {
    return part.text
  }

  return ''
}

function durationMs(startedAt: number | undefined, completedAt: number | undefined): number | null {
  if (startedAt === undefined || completedAt === undefined || completedAt < startedAt) {
    return null
  }

  return Math.round((completedAt - startedAt) * 1000)
}

function recordStatus(message: ChatMessage, part: ChatMessagePart): TrajectoryRecordStatus {
  if (part.type === 'tool-call' && part.isError) {
    return 'error'
  }

  if (message.error) {
    return 'error'
  }

  if (part.type === 'tool-call' && part.result === undefined) {
    return 'running'
  }

  return message.pending ? 'running' : 'complete'
}

function messageBounds(message: ChatMessage): { completedAt: number | null; startedAt: number | null } {
  const timestamps = message.parts
    .map(part => part.timestamp)
    .filter((value): value is number => value !== undefined)

  const completions = message.parts
    .map(part => part.completedAt)
    .filter((value): value is number => value !== undefined)

  return {
    startedAt: message.timestamp ?? (timestamps.length ? Math.min(...timestamps) : null),
    completedAt: message.completedAt ?? (completions.length ? Math.max(...completions) : null)
  }
}

export function projectTrajectory(
  messages: readonly ChatMessage[],
  runtime: { model: string; provider: string }
): TrajectoryProjection {
  const records: TrajectoryRecord[] = []
  let turn = 0
  let step = 0

  for (const message of messages) {
    if (message.hidden) {
      continue
    }

    if (message.role === 'user') {
      turn += 1
      step = 0
      const bounds = messageBounds(message)
      const text = message.parts.map(partText).join('').trim()

      records.push({
        id: message.id,
        kind: 'user',
        turn,
        step: null,
        text,
        startedAt: bounds.startedAt,
        completedAt: bounds.completedAt,
        durationMs:
          message.durationS !== undefined
            ? Math.round(message.durationS * 1000)
            : durationMs(bounds.startedAt ?? undefined, bounds.completedAt ?? undefined),
        status: message.error ? 'error' : message.pending ? 'running' : 'complete'
      })

      continue
    }

    if (message.role !== 'assistant') {
      continue
    }

    if (turn === 0) {
      turn = 1
    }

    step += 1

    for (const [partIndex, part] of message.parts.entries()) {
      const startedAt = part.timestamp ?? message.timestamp ?? null
      const completedAt = part.completedAt ?? message.completedAt ?? null

      const common = {
        id: `${message.id}:${partIndex}`,
        turn,
        step,
        startedAt,
        completedAt,
        durationMs: durationMs(startedAt ?? undefined, completedAt ?? undefined),
        status: recordStatus(message, part)
      }

      if (part.type === 'tool-call') {
        records.push({
          ...common,
          kind: 'tool',
          text: part.toolName,
          callId: part.toolCallId,
          name: part.toolName,
          payload: part.args,
          result: part.result
        })
      } else if (part.type === 'reasoning') {
        records.push({ ...common, kind: 'reasoning', text: part.text })
      } else if (part.type === 'text' && part.text.trim()) {
        records.push({ ...common, kind: 'assistant', text: part.text })
      }
    }
  }

  const starts = records.map(record => record.startedAt).filter((value): value is number => value !== null)
  const endings = records.map(record => record.completedAt).filter((value): value is number => value !== null)

  return {
    model: runtime.model,
    provider: runtime.provider,
    summary: {
      turns: messages.filter(message => !message.hidden && message.role === 'user').length || (records.length ? 1 : 0),
      steps: new Set(records.filter(record => record.step !== null).map(record => `${record.turn}:${record.step}`)).size,
      toolCalls: records.filter(record => record.kind === 'tool').length,
      errors: records.filter(record => record.status === 'error').length,
      durationMs: starts.length && endings.length ? Math.max(0, Math.round((Math.max(...endings) - Math.min(...starts)) * 1000)) : 0
    },
    records
  }
}
