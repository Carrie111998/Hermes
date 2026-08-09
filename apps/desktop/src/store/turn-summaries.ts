import { atom } from 'nanostores'

import { TurnSummaryCollector } from '@/lib/turn-summary'

// Per-turn tool summary lines keyed by assistant message id (CLI
// display.turn_summary parity). Computed once when the turn settles in
// use-message-stream; read by the assistant message row to render the dim
// summary under the bubble. Keyed by message id like $toolDiffs — a stale
// entry is inert because it only renders when its message id is live.
const $turnSummaries = atom<Record<string, string>>({})

export { $turnSummaries }

// Per-session collectors for the ACTIVE turn. Fed from `tool.complete` events
// as they arrive — NOT derived from the settled message's parts — so
// multi-segment turns (an interim-sealed tool bubble followed by a distinct
// text-only final bubble) still tally every tool. Reset at `message.start`.
const collectors = new Map<string, TurnSummaryCollector>()

export function beginTurnSummary(sessionId: string) {
  collectors.set(sessionId, new TurnSummaryCollector())
}

export function recordTurnTool(
  sessionId: string,
  toolName: string | null | undefined,
  isError: boolean,
  result?: unknown,
) {
  collectors.get(sessionId)?.recordTool(toolName, { isError, result })
}

export function renderTurnSummary(sessionId: string, elapsedSeconds: number): string {
  const collector = collectors.get(sessionId)

  if (!collector) {
    return ''
  }

  // One render per turn: drop the collector so a closed session can't leak a
  // tally and a second completion for the same turn renders nothing new
  // (recordTurnSummary dedupes identical values anyway).
  collectors.delete(sessionId)

  return collector.render(elapsedSeconds)
}

export function recordTurnSummary(messageId: string, summary: string) {
  if (!messageId) {
    return
  }

  const current = $turnSummaries.get()

  if (current[messageId] === summary) {
    return
  }

  $turnSummaries.set({ ...current, [messageId]: summary })
}

export function getTurnSummary(messageId: string): string {
  return messageId ? $turnSummaries.get()[messageId] || '' : ''
}
