import { describe, expect, it } from 'vitest'

import { chatMessageText, type ChatMessage } from '@/lib/chat-messages'

import { reconcileResumeMessages } from './utils'

const user = (id: string, text: string): ChatMessage => ({
  id,
  parts: [{ type: 'text', text }],
  role: 'user'
})

/**
 * Switching away from a running session and back re-hydrates it from
 * `session.activate`, whose transcript is TEXT-ONLY for the live turn (the
 * gateway's `inflight` projection carries `user`/`assistant` strings, nothing
 * structural). The renderer's cached state is the only carrier of the turn's
 * tool calls and reasoning, so reconcile must not drop them.
 */
describe('reconcileResumeMessages — structural parts on a mid-turn switch', () => {
  it('keeps tool-call parts the authoritative text-only row cannot carry', () => {
    const cached: ChatMessage[] = [
      user('u1', 'read the config'),
      {
        id: 'a1',
        parts: [
          { type: 'reasoning', text: 'I should read the file first.' },
          { type: 'tool-call', toolCallId: 'call-1', toolName: 'read_file', result: 'contents' },
          { type: 'text', text: 'Reading it now' }
        ],
        role: 'assistant'
      }
    ]

    // What activate returns mid-turn: the same rows, but flattened to text and
    // one delta further along, so the text no longer matches the cached copy.
    const authoritative: ChatMessage[] = [
      user('u1', 'read the config'),
      { id: 'a1', parts: [{ type: 'text', text: 'Reading it now — found the key' }], role: 'assistant' }
    ]

    const [, assistant] = reconcileResumeMessages(authoritative, cached)

    expect(assistant.parts.filter(p => p.type === 'tool-call')).toHaveLength(1)
    expect(assistant.parts.filter(p => p.type === 'reasoning')).toHaveLength(1)
    // The newer authoritative text still wins.
    expect(assistant.parts.filter(p => p.type === 'text').at(-1)).toMatchObject({
      text: 'Reading it now — found the key'
    })
  })

  it('does not duplicate tool calls the authoritative row already carries', () => {
    const cached: ChatMessage[] = [
      {
        id: 'a1',
        parts: [{ type: 'tool-call', toolCallId: 'call-1', toolName: 'read_file', result: 'contents' }],
        role: 'assistant'
      }
    ]

    const authoritative: ChatMessage[] = [
      {
        id: 'a1',
        parts: [
          { type: 'tool-call', toolCallId: 'call-1', toolName: 'read_file', result: 'contents' },
          { type: 'text', text: 'done' }
        ],
        role: 'assistant'
      }
    ]

    const [assistant] = reconcileResumeMessages(authoritative, cached)

    expect(assistant.parts.filter(p => p.type === 'tool-call')).toHaveLength(1)
  })

  it('keeps live-tail structure when the flat dump is not a strict text extension', () => {
    // Mid-turn sandwich path: cache holds reasoning/tools; resume returns a
    // longer non-extending dump. Structure source must be live-tail.
    const cached: ChatMessage[] = [
      {
        id: 'assistant-stream-1',
        pending: true,
        parts: [
          { type: 'reasoning', text: 'thinking about tools' },
          { type: 'tool-call', toolCallId: 'c1', toolName: 'terminal', args: {} },
          { type: 'text', text: 'partial' }
        ],
        role: 'assistant'
      }
    ]

    const authoritative: ChatMessage[] = [
      {
        id: 'assistant-stream-1',
        pending: true,
        parts: [{ type: 'text', text: 'thinking about tools\nRan terminal\npartial and more dump' }],
        role: 'assistant'
      }
    ]

    const [assistant] = reconcileResumeMessages(authoritative, cached)

    expect(assistant.parts.some(part => part.type === 'reasoning')).toBe(true)
    expect(assistant.parts.some(part => part.type === 'tool-call')).toBe(true)
    expect(assistant.parts.filter(part => part.type === 'text').map(part => ('text' in part ? part.text : ''))).toEqual(
      ['partial']
    )
  })

  it('does not graft historical structure onto a live text-only row after compression rewrote ordinals', () => {
    // Previous cache still has a completed structured assistant at ordinal 0.
    // Resume after compression returns a new live text-only assistant at the
    // same role ordinal for an unrelated turn — must not inherit foreign parts.
    const cached: ChatMessage[] = [
      {
        id: 'old-assistant',
        parts: [
          { type: 'reasoning', text: 'old thinking' },
          { type: 'tool-call', toolCallId: 'old-call', toolName: 'terminal', args: {} },
          { type: 'text', text: 'old answer' }
        ],
        role: 'assistant'
      }
    ]

    const authoritative: ChatMessage[] = [
      {
        id: 'assistant-stream-runtime-1',
        pending: true,
        parts: [{ type: 'text', text: 'brand new partial' }],
        role: 'assistant'
      }
    ]

    const [assistant] = reconcileResumeMessages(authoritative, cached)

    expect(assistant.parts.some(part => part.type === 'reasoning')).toBe(false)
    expect(assistant.parts.some(part => part.type === 'tool-call')).toBe(false)
    expect(assistant.parts).toEqual([{ type: 'text', text: 'brand new partial' }])
  })
})

/**
 * Resuming onto a session the gateway already owns returns a CACHED snapshot
 * (the desktop's local mirror from a prior session.resume) — that snapshot is
 * stale by definition because the gateway may have appended further turns
 * after it was taken. The persisted REST transcript (getLatestSessionMessages)
 * is what the model will see on the next submit; when it disagrees with the
 * cached snapshot, the persisted wins (#81951).
 *
 * The warm-cache path applies the persisted transcript over the cached snapshot
 * via a second reconcileResumeMessages call — exactly what the test below
 * mirrors: pass the cached snapshot as `previousMessages` (warm path's
 * initial step), then reconcile the persisted over the result.
 */
describe('reconcileResumeMessages — persisted transcript overrides stale snapshot (#81951)', () => {
  it('appends turns the cached snapshot missed', () => {
    // The cached snapshot was taken before the gateway received a third user
    // turn. The persisted transcript carries it. After the merge, the third
    // turn must be present alongside the cached rows.
    const cached: ChatMessage[] = [
      user('u1', 'first ask'),
      { id: 'a1', parts: [{ type: 'text', text: 'first answer' }], role: 'assistant' },
      user('u2', 'second ask'),
      { id: 'a2', parts: [{ type: 'text', text: 'second answer' }], role: 'assistant' }
    ]

    const persisted: ChatMessage[] = [
      user('u1', 'first ask'),
      { id: 'a1', parts: [{ type: 'text', text: 'first answer' }], role: 'assistant' },
      user('u2', 'second ask'),
      { id: 'a2', parts: [{ type: 'text', text: 'second answer' }], role: 'assistant' },
      user('u3', 'third ask (gateway-only)'),
      { id: 'a3', parts: [{ type: 'text', text: 'third answer' }], role: 'assistant' }
    ]

    // Warm-path Step 1: session.activate returns empty messages (omit_messages)
// and no inflight/queued, so activatedMessages === cachedViewState.messages.
    const activatedMessages = reconcileResumeMessages(cached, [])
    expect(activatedMessages.map(m => chatMessageText(m).trim() || m.id)).toEqual([
      'first ask',
      'first answer',
      'second ask',
      'second answer'
    ])

    // Warm-path Step 2 (the fix): persisted is always reconciled on top, even
    // when the session is mid-turn — the cached snapshot must not leak into
    // the model prompt at the expense of the gateway's authoritative history.
    const merged = reconcileResumeMessages(persisted, activatedMessages)
    expect(merged.map(m => chatMessageText(m).trim() || m.id)).toEqual([
      'first ask',
      'first answer',
      'second ask',
      'second answer',
      'third ask (gateway-only)',
      'third answer'
    ])
  })

  it('prefers the persisted transcript text when the cached snapshot is older', () => {
    // The cached snapshot was taken mid-response: assistant is partial. The
    // persisted transcript carries the FULLER answer the gateway now has.
    // Reconciling persisted on top of the snapshot must surface the newer
    // assistant text.
    const cached: ChatMessage[] = [
      user('u1', 'summarize'),
      { id: 'a1', parts: [{ type: 'text', text: 'partial answer' }], role: 'assistant' }
    ]

    const persisted: ChatMessage[] = [
      user('u1', 'summarize'),
      { id: 'a1', parts: [{ type: 'text', text: 'partial answer complete with conclusion' }], role: 'assistant' }
    ]

    const activatedMessages = reconcileResumeMessages(cached, [])
    const merged = reconcileResumeMessages(persisted, activatedMessages)

    expect(merged).toHaveLength(2)
    expect(chatMessageText(merged[1]!).trim()).toBe('partial answer complete with conclusion')
  })

  it('keeps cached reasoning/tool parts even when the persisted text is newer', () => {
    // Same scenario as the warm-cache reasoning carry-over test, but framed
    // as a stale-snapshot merge: the snapshot has structural parts the
    // gateway's text-only persisted dump can't carry, and we still want
    // them on the final transcript the model sees.
    const cached: ChatMessage[] = [
      user('u1', 'read the config'),
      {
        id: 'a1',
        parts: [
          { type: 'reasoning', text: 'I should read the file first.' },
          { type: 'tool-call', toolCallId: 'call-1', toolName: 'read_file', result: 'contents' },
          { type: 'text', text: 'Reading it now' }
        ],
        role: 'assistant'
      }
    ]

    // Persisted from the gateway is text-only — the tool/reasoning live in
    // the cache. The merged transcript must keep them.
    const persisted: ChatMessage[] = [
      user('u1', 'read the config'),
      { id: 'a1', parts: [{ type: 'text', text: 'Reading it now — found the key' }], role: 'assistant' }
    ]

    const activatedMessages = reconcileResumeMessages(cached, [])
    const merged = reconcileResumeMessages(persisted, activatedMessages)

    const assistant = merged[1]!
    expect(assistant.parts.filter(p => p.type === 'tool-call')).toHaveLength(1)
    expect(assistant.parts.filter(p => p.type === 'reasoning')).toHaveLength(1)
    expect(assistant.parts.filter(p => p.type === 'text').at(-1)).toMatchObject({
      text: 'Reading it now — found the key'
    })
  })
})
