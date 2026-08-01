import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { ChatMessage, SessionMessage } from '@/lib/chat-messages'
import { toChatMessages } from '@/lib/chat-messages'

import { dedupeMessagesById, journalDuplicateMessages } from './dedupe-messages'

/** A minimal raw backend row as the gateway/resume path would deliver it. */
function rawRow(row_id: number, text: string, role: SessionMessage['role'] = 'assistant'): SessionMessage {
  return { row_id, role, content: text, timestamp: 1754000000 + row_id }
}

/**
 * The real failing mechanism the maintainer review called out: a backend that
 * emits the SAME persisted row twice. `toChatMessages` stamps each with a
 * DISTINCT ephemeral renderer id (timestamp+index+role), so a guard keyed on the
 * renderer id would never fire. The durable identity is `row_id` — and that is
 * what must be deduped. This test exercises the actual
 * `SessionMessage -> toChatMessages -> dedupe` pipeline, not hand-faked ids.
 */
describe('dedupe via the real reconciliation pipeline', () => {
  it('collapses two backend rows that share a row_id (distinct renderer ids)', () => {
    const raw: SessionMessage[] = [
      rawRow(10, 'first copy of message 10'),
      rawRow(10, 'second copy of message 10'), // same persisted row, replayed
      rawRow(11, 'message 11'),
    ]

    const chatMessages = toChatMessages(raw)
    // Sanity: the two row_id=10 rows got DIFFERENT ephemeral renderer ids.
    const rendererIds = chatMessages.map(m => m.id)
    expect(new Set(rendererIds).size).toBe(rendererIds.length)

    const result = dedupeMessagesById(chatMessages)

    expect(result.removedAny).toBe(true)
    expect(result.messages).toHaveLength(2)
    expect(result.messages.map(m => m.rowId)).toEqual([10, 11])
    // First-seen wins: the original content of row 10 is kept.
    expect(chatMessageText(result.messages[0])).toContain('first copy')
  })

  it('keeps distinct row_ids and does not touch live rows without a row_id', () => {
    const raw: SessionMessage[] = [
      rawRow(20, 'a'),
      rawRow(21, 'b'),
      // A live/optimistic row has no row_id yet — must survive untouched.
      { role: 'user', content: 'c', timestamp: 1754000099 },
    ]

    const result = dedupeMessagesById(toChatMessages(raw))

    expect(result.removedAny).toBe(false)
    expect(result.messages).toHaveLength(3)
    expect(result.messages.map(m => m.rowId)).toEqual([20, 21, undefined])
  })
})

describe('dedupeMessagesById (unit)', () => {
  it('first-seen wins when two ChatMessages share a rowId', () => {
    const input: ChatMessage[] = [
      { id: 'r-1', rowId: 1, role: 'assistant', parts: [{ type: 'text', text: 'first' }] },
      { id: 'r-2', rowId: 1, role: 'assistant', parts: [{ type: 'text', text: 'second' }] },
      { id: 'r-3', rowId: 2, role: 'assistant', parts: [{ type: 'text', text: 'third' }] },
    ]
    const result = dedupeMessagesById(input)

    expect(result.messages).toHaveLength(2)
    expect(result.messages.map(m => m.rowId)).toEqual([1, 2])
    expect(chatMessageText(result.messages[0])).toBe('first')
  })

  it('exempts rows without a durable key (live/optimistic)', () => {
    const input: ChatMessage[] = [
      { id: 'live-1', role: 'user', parts: [{ type: 'text', text: 'x' }] },
      { id: 'live-2', role: 'user', parts: [{ type: 'text', text: 'y' }] },
    ]
    const result = dedupeMessagesById(input)

    expect(result.removedAny).toBe(false)
    expect(result.messages).toHaveLength(2)
  })
})

describe('journalDuplicateMessages', () => {
  beforeEach(() => {
    vi.spyOn(console, 'warn').mockImplementation(() => {})
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('emits a structured warning carrying the durable key + renderer id', () => {
    dedupeMessagesById([
      { id: 'r-1', rowId: 1, role: 'assistant', parts: [{ type: 'text', text: 'alpha' }] },
      { id: 'r-2', rowId: 1, role: 'assistant', parts: [{ type: 'text', text: 'beta' }] },
    ])

    // journal is called by the caller; replicate the call shape here.
    journalDuplicateMessages(
      [{ durableKey: 1, rendererId: 'r-2', role: 'assistant', charLength: 4 }],
      { source: 'reconcileAuthoritativeMessages', sessionId: 'sess-1' }
    )

    expect(console.warn).toHaveBeenCalledTimes(1)
    const [label, payload] = vi.mocked(console.warn).mock.calls[0]
    expect(label).toBe('[transcript-dedup] duplicate durable message key(s) collapsed during merge')
    expect(payload).toMatchObject({
      source: 'reconcileAuthoritativeMessages',
      sessionId: 'sess-1',
      count: 1,
      duplicates: [{ durableKey: 1, rendererId: 'r-2', role: 'assistant', charLength: 4 }],
    })
  })

  it('is a no-op when there are no duplicates', () => {
    journalDuplicateMessages([], { source: 'reconcileAuthoritativeMessages' })
    expect(console.warn).not.toHaveBeenCalled()
  })
})

/** Local helper mirroring chatMessageText's plain-text extraction for assertions. */
function chatMessageText(message: ChatMessage): string {
  return message.parts
    .map(part => (typeof part === 'object' && part !== null && 'text' in part ? (part as { text?: string }).text ?? '' : ''))
    .join('')
}
