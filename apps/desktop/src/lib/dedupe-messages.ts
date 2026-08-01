import type { ChatMessage } from '@/lib/chat-messages'

/**
 * Transcript reconciliation must never inject a duplicate *persisted* message
 * into the renderer cache. A backend that emits the same stored row twice
 * (same `row_id` / numeric `id`) would otherwise render that message twice —
 * the "message shows up doubled but the DB has it once" symptom reported
 * 2026-07-31. The store has no dedup today, so this collapses any repeated
 * durable identity defensively, then journals what it removed so a recurrence
 * is observable instead of a phantom.
 *
 * KEY LAYER — read this before "fixing" the guard:
 * The renderer's own `ChatMessage.id` is EPHEMERAL (derived from
 * `timestamp + index + role` in `toChatMessages`). Two repeats of the same
 * backend row get DIFFERENT renderer ids, so deduping on `ChatMessage.id`
 * would NEVER fire. The durable identity is `row_id` (gateway resume) or the
 * numeric `id` (REST transcript) — see `types/hermes.ts` and
 * `chat-messages.ts:toChatMessages`. We dedupe on THAT.
 *
 * Rows with no durable identity (live/optimistic, still mid-stream) are
 * exempt: they legitimately lack `rowId` and must never be collapsed.
 */

/** The durable backend identity for a row, or undefined if not yet persisted. */
export function durableMessageKey(message: ChatMessage): number | undefined {
  return message.rowId
}

export interface DedupeResult {
  /** Messages with duplicate durable keys collapsed (first-seen wins). */
  messages: ChatMessage[]
  /** True when one or more duplicate durable keys were removed. */
  removedAny: boolean
  /** Per-duplicate detail, for journaling. */
  removed: Array<{ durableKey: number; rendererId: string; role: string; charLength: number }>
}

/**
 * Return a copy of `messages` with any repeated durable message key
 * (`rowId`) collapsed to its FIRST occurrence. Stable order (first-seen
 * position kept, later dup removed). First-seen wins: the repeat is the
 * suspect, so it is discarded entirely (position + content).
 *
 * Rows without a durable key (live/optimistic) are never collapsed — they
 * are unique per construction and must survive.
 */
export function dedupeMessagesById(messages: ChatMessage[]): DedupeResult {
  const seen = new Set<number>()
  const kept: ChatMessage[] = []
  const removed: DedupeResult['removed'] = []

  for (const message of messages) {
    const key = durableMessageKey(message)
    // No durable key → live/optimistic row; never collapse.
    if (key === undefined) {
      kept.push(message)
      continue
    }
    if (seen.has(key)) {
      removed.push({
        durableKey: key,
        rendererId: message.id,
        role: message.role,
        charLength: charLengthOf(message),
      })
      continue
    }
    seen.add(key)
    kept.push(message)
  }

  return {
    messages: kept,
    removedAny: removed.length > 0,
    removed,
  }
}

/**
 * Emit a structured, dev-only warning when duplicate durable message keys were
 * found during a transcript merge. This is the evidence collector: in a dev
 * build the duplicate is reported with enough context (durable key, renderer
 * id, role, merge source) to file a reproduction-backed issue. No-op in
 * production builds.
 */
export function journalDuplicateMessages(
  removed: DedupeResult['removed'],
  context: { source: string; sessionId?: string | null }
): void {
  if (!import.meta.env.DEV || removed.length === 0) {
    return
  }

  console.warn('[transcript-dedup] duplicate durable message key(s) collapsed during merge', {
    source: context.source,
    sessionId: context.sessionId ?? null,
    count: removed.length,
    duplicates: removed,
  })
}

function charLengthOf(message: ChatMessage): number {
  let total = 0
  for (const part of message.parts ?? []) {
    if (typeof part === 'object' && part !== null && 'text' in part && typeof part.text === 'string') {
      total += part.text.length
    }
  }
  return total
}
