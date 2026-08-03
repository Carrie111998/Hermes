import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { type ChatMessage, chatMessageText } from '@/lib/chat-messages'
import {
  clearInFlightTurnJournal,
  JOURNAL_MAX_AGE_MS,
  JOURNAL_PERSIST_THROTTLE_MS,
  JOURNAL_STORAGE_KEY,
  type JournalableSessionState,
  mergeInFlightMessages,
  persistInFlightTurnState,
  purgeInFlightTurnJournals,
  readInFlightTurnJournal,
  recoverInFlightTurnJournal,
  sweepExpiredInFlightTurnJournals
} from '@/lib/inflight-turn-journal'

function storedEntries(): Record<string, { runtimeSessionId?: null | string; updatedAt: number }> {
  const raw = window.localStorage.getItem(JOURNAL_STORAGE_KEY)

  return raw ? JSON.parse(raw).entries : {}
}

function storedKeys(): string[] {
  return Object.keys(storedEntries())
}

function user(id: string, text: string): ChatMessage {
  return { id, role: 'user', parts: [{ type: 'text', text }] }
}

function assistant(id: string, text: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return { id, role: 'assistant', parts: [{ type: 'text', text }], ...extra }
}

function assistantWithTool(id: string, text: string, extra: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id,
    role: 'assistant',
    parts: [
      { type: 'tool-call', toolCallId: 'tc-1', toolName: 'terminal', args: { command: 'ls' } },
      { type: 'text', text }
    ],
    ...extra
  }
}

function journalState(overrides: Partial<JournalableSessionState> = {}): JournalableSessionState {
  return {
    awaitingResponse: false,
    busy: true,
    messages: [user('u1', 'do the thing'), assistant('assistant-stream-1', 'partial answer', { pending: true })],
    storedSessionId: 'stored-1',
    streamId: 'assistant-stream-1',
    turnStartedAt: 1000,
    ...overrides
  }
}

beforeEach(() => {
  vi.useFakeTimers()
  window.localStorage.clear()
})

afterEach(() => {
  clearInFlightTurnJournal('stored-1')
  vi.useRealTimers()
})

describe('persistInFlightTurnState', () => {
  it('journals the running turn tail after the throttle window', () => {
    persistInFlightTurnState(journalState())

    expect(readInFlightTurnJournal('stored-1')).toBeNull()

    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    const entry = readInFlightTurnJournal('stored-1')
    expect(entry).not.toBeNull()
    expect(entry?.streamId).toBe('assistant-stream-1')
    expect(entry?.turnStartedAt).toBe(1000)
    expect(entry?.messages.map(m => m.role)).toEqual(['user', 'assistant'])
  })

  it('coalesces rapid updates into one write carrying the latest state', () => {
    persistInFlightTurnState(journalState())
    persistInFlightTurnState(
      journalState({
        messages: [
          user('u1', 'do the thing'),
          assistant('assistant-stream-1', 'partial answer grew', { pending: true })
        ]
      })
    )

    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    const entry = readInFlightTurnJournal('stored-1')
    const tail = entry?.messages.find(m => m.role === 'assistant')
    expect(tail?.parts).toEqual([{ type: 'text', text: 'partial answer grew' }])
  })

  it('clears the entry the moment the turn settles, cancelling pending writes', () => {
    persistInFlightTurnState(journalState())
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
    expect(readInFlightTurnJournal('stored-1')).not.toBeNull()

    persistInFlightTurnState(journalState({ messages: [] }))
    persistInFlightTurnState(journalState({ busy: false, awaitingResponse: false, streamId: null }))

    expect(readInFlightTurnJournal('stored-1')).toBeNull()

    vi.advanceTimersByTime(1000)
    expect(readInFlightTurnJournal('stored-1')).toBeNull()
  })

  it('does not journal a turn with no recoverable assistant content yet', () => {
    persistInFlightTurnState(journalState({ messages: [user('u1', 'do the thing')], streamId: null }))

    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
    expect(readInFlightTurnJournal('stored-1')).toBeNull()
  })

  it('expires entries older than the max age', () => {
    persistInFlightTurnState(journalState())
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    const raw = JSON.parse(window.localStorage.getItem(JOURNAL_STORAGE_KEY)!)
    raw.entries['stored-1'].updatedAt = Date.now() - (JOURNAL_MAX_AGE_MS + 60_000)
    window.localStorage.setItem(JOURNAL_STORAGE_KEY, JSON.stringify(raw))

    expect(readInFlightTurnJournal('stored-1')).toBeNull()
  })
})

describe('recoverInFlightTurnJournal', () => {
  function journalEntry(messages: ChatMessage[]) {
    persistInFlightTurnState(journalState({ messages, streamId: messages.at(-1)?.id ?? null }))
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
  }

  it('is a reference-preserving no-op when nothing is journaled', () => {
    const base = [user('u1', 'do the thing')]
    const result = recoverInFlightTurnJournal('stored-1', base)

    expect(result.applied).toBe(false)
    expect(result.messages).toBe(base)
  })

  it('appends the full tail when the base transcript never saw the turn', () => {
    journalEntry([
      user('u1', 'do the thing'),
      assistantWithTool('assistant-stream-1', 'working on it', { pending: true })
    ])

    const base = [user('u0', 'earlier turn'), assistant('a0', 'earlier reply')]
    const result = recoverInFlightTurnJournal('stored-1', base)

    expect(result.applied).toBe(true)
    expect(result.messages.map(m => m.id)).toEqual(['u0', 'a0', 'u1', 'assistant-stream-1'])
    expect(result.streamId).toBe('assistant-stream-1')
  })

  it('appends only the assistant tail when the user row was persisted', () => {
    journalEntry([
      user('u1', 'do the thing'),
      assistantWithTool('assistant-stream-1', 'working on it', { pending: true })
    ])

    const base = [user('db-u1', 'do the thing')]
    const result = recoverInFlightTurnJournal('stored-1', base, { keepPending: false })

    expect(result.applied).toBe(true)
    expect(result.messages.map(m => m.id)).toEqual(['db-u1', 'assistant-stream-1'])
    const tail = result.messages.at(-1)!
    expect(tail.pending).toBe(false)
    expect(tail.parts[0]).toMatchObject({ type: 'tool-call' })
  })

  it('detects a committed reply as caught up and clears the entry', () => {
    journalEntry([user('u1', 'do the thing'), assistant('assistant-stream-1', 'partial', { pending: true })])

    const base = [user('db-u1', 'do the thing'), assistant('db-a1', 'full committed reply')]
    const result = recoverInFlightTurnJournal('stored-1', base)

    expect(result.applied).toBe(false)
    expect(result.caughtUp).toBe(true)
    expect(result.messages).toBe(base)
    expect(readInFlightTurnJournal('stored-1')).toBeNull()
  })

  it('overlays the backend text-only projection instead of dropping local tool progress', () => {
    // Sweeper regression on #44339: a backend `inflight` assistant snapshot
    // (text only) used to mark the richer local tail "caught up" and delete
    // locally recorded tool calls.
    journalEntry([
      user('u1', 'do the thing'),
      assistantWithTool('assistant-stream-old', 'local part', { pending: true })
    ])

    const base = [
      user('db-u1', 'do the thing'),
      assistant('assistant-stream-rt9', 'longer partial text from the backend snapshot', { pending: true })
    ]

    const result = recoverInFlightTurnJournal('stored-1', base, { keepPending: true })

    expect(result.applied).toBe(true)
    expect(result.caughtUp).toBe(false)
    expect(result.messages).toHaveLength(2)

    const merged = result.messages.at(-1)!
    // Keeps the BASE projection row id so live deltas keep landing on it.
    expect(merged.id).toBe('assistant-stream-rt9')
    expect(result.streamId).toBe('assistant-stream-rt9')
    // Journal structure survives; the longer backend text wins.
    expect(merged.parts[0]).toMatchObject({ type: 'tool-call', toolName: 'terminal' })
    expect(merged.parts[1]).toMatchObject({ type: 'text', text: 'longer partial text from the backend snapshot' })
    // Still in flight — the journal must NOT be cleared.
    expect(readInFlightTurnJournal('stored-1')).not.toBeNull()
  })

  it('keeps the journal text when it is longer than the projection text', () => {
    journalEntry([
      user('u1', 'do the thing'),
      assistantWithTool('assistant-stream-old', 'a much longer locally journaled partial answer', { pending: true })
    ])

    const base = [user('db-u1', 'do the thing'), assistant('assistant-stream-rt9', 'thin', { pending: true })]
    const result = recoverInFlightTurnJournal('stored-1', base, { keepPending: true })

    const merged = result.messages.at(-1)!
    expect(merged.id).toBe('assistant-stream-rt9')
    expect(merged.parts[1]).toMatchObject({ type: 'text', text: 'a much longer locally journaled partial answer' })
  })
})

describe('mergeInFlightMessages', () => {
  it('treats an error-bearing assistant row as recoverable content', () => {
    const tail = [user('u1', 'do the thing'), assistant('a-err', '', { error: 'provider exploded' })]
    const result = mergeInFlightMessages([user('db-u1', 'do the thing')], tail)

    expect(result.applied).toBe(true)
    expect(result.messages.at(-1)?.error).toBe('provider exploded')
  })

  it('ignores hidden rows when extracting nothing to recover', () => {
    const result = mergeInFlightMessages([], [user('u1', 'x')])

    expect(result.applied).toBe(false)
    expect(result.caughtUp).toBe(false)
  })
})

describe('mid-turn redirect corrections', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  // A redirect inserts its correction as a second user row directly before the
  // live reply, so the turn opens with a RUN of user rows. Journaling only back
  // to the nearest one lost the prompt that actually started the turn — the
  // vanishing user bubble.
  it('journals the whole user run, not just the correction', () => {
    persistInFlightTurnState({
      awaitingResponse: false,
      busy: true,
      messages: [
        user('user-1', 'remove the session counts'),
        user('user-2', 'hurry up'),
        assistant('assistant-stream-1', 'Moving.', { pending: true })
      ],
      storedSessionId: 'stored-redirect',
      streamId: 'assistant-stream-1',
      turnStartedAt: Date.now()
    })
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    const journaled = readInFlightTurnJournal('stored-redirect')?.messages ?? []

    expect(journaled.map(message => message.parts.map(part => (part as { text: string }).text).join(''))).toEqual([
      'remove the session counts',
      'hurry up',
      'Moving.'
    ])
  })

  it('still stops at an assistant boundary so prior turns are not journaled', () => {
    persistInFlightTurnState({
      awaitingResponse: false,
      busy: true,
      messages: [
        user('user-old', 'an earlier turn'),
        assistant('assistant-old', 'an earlier answer'),
        user('user-1', 'the live prompt'),
        assistant('assistant-stream-1', 'Moving.', { pending: true })
      ],
      storedSessionId: 'stored-boundary',
      streamId: 'assistant-stream-1',
      turnStartedAt: Date.now()
    })
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    const journaled = readInFlightTurnJournal('stored-boundary')?.messages ?? []

    expect(journaled.map(message => message.id)).toEqual(['user-1', 'assistant-stream-1'])
  })
})

// #77486 proposed redacting the journal. It must NOT be redacted: this tail is
// replayed, so masking it poisons the replay exactly as #43083 documents for
// tool-call args (tests/agent/test_tool_call_arg_no_redaction.py). The
// confidentiality axis is RETENTION. These tests pin both halves of that
// bargain — content survives verbatim, exposure is bounded.
describe('journal replay fidelity (no redaction)', () => {
  const SECRET_PROMPT = "run PGPASSWORD='honchorulez' psql -h 127.0.0.1"

  beforeEach(() => {
    window.localStorage.clear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    purgeInFlightTurnJournals(['stored-replay'])
    vi.useRealTimers()
  })

  function journalSecretTurn(): void {
    persistInFlightTurnState({
      awaitingResponse: false,
      busy: true,
      messages: [
        user('user-1', SECRET_PROMPT),
        {
          id: 'assistant-stream-1',
          role: 'assistant',
          parts: [
            {
              type: 'tool-call',
              toolCallId: 'tc-1',
              toolName: 'terminal',
              args: { command: SECRET_PROMPT }
            }
          ],
          pending: true
        }
      ],
      storedSessionId: 'stored-replay',
      streamId: 'assistant-stream-1',
      turnStartedAt: 1000
    })
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
  }

  /** The extraction `planReload` / `planRestore` perform on the recovered
   *  transcript before handing text to `prompt.submit`
   *  (use-prompt-actions/rewind.ts:125 — `chatMessageText(userMessage).trim()`).
   *  Uses the REAL extractor. A mirrored copy here would buy nothing and could
   *  drift: `inflight-turn-journal.ts` already imports `chatMessageText` from
   *  `chat-messages.ts` at runtime, so the module is in this suite's graph
   *  either way, and `chat-messages.ts` touches no `localStorage`. */
  function replayedPromptText(messages: ChatMessage[]): string {
    const userMessage = [...messages].reverse().find(message => message.role === 'user')

    return userMessage ? chatMessageText(userMessage).trim() : ''
  }

  // The load-bearing contract: a recovered transcript is what Retry/Regenerate
  // resubmits. The reload planner reads the user row back out and hands its text to
  // `prompt.submit`, so a redacted journal would send `***` to the model as the
  // user's real prompt on the next turn.
  it('round-trips a recovered prompt back to the submit text byte-for-byte', () => {
    journalSecretTurn()

    const recovered = recoverInFlightTurnJournal('stored-replay', [], { keepPending: true })

    expect(recovered.applied).toBe(true)
    expect(replayedPromptText(recovered.messages)).toBe(SECRET_PROMPT)
    expect(replayedPromptText(recovered.messages)).not.toContain('***')
  })

  it('recovers tool-call args verbatim so replayed structure stays executable', () => {
    journalSecretTurn()

    const recovered = recoverInFlightTurnJournal('stored-replay', [], { keepPending: true })

    const toolPart = recovered.messages
      .flatMap(message => message.parts)
      .find((part): part is Extract<typeof part, { type: 'tool-call' }> => part.type === 'tool-call')

    expect(toolPart?.args).toEqual({ command: SECRET_PROMPT })
  })
})

describe('journal retention (the confidentiality axis)', () => {
  const HOUR_MS = 60 * 60 * 1000

  beforeEach(() => {
    window.localStorage.clear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    purgeInFlightTurnJournals(['stored-a', 'stored-b', 'stored-keep'])
    vi.useRealTimers()
  })

  function seed(storedSessionId: string): void {
    persistInFlightTurnState(journalState({ storedSessionId }))
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
  }

  /** Backdate entries in ONE direct write. Ageing them via the public API would
   *  re-enter `loadStore`, which now sweeps — the later seed would evict the
   *  earlier aged entry before the test could establish its precondition. */
  function backdate(ids: string[], ageMs: number): void {
    const raw = JSON.parse(window.localStorage.getItem(JOURNAL_STORAGE_KEY)!)

    for (const id of ids) {
      raw.entries[id].updatedAt = Date.now() - ageMs
    }

    window.localStorage.setItem(JOURNAL_STORAGE_KEY, JSON.stringify(raw))
  }

  /** Overwrite one entry field directly, bypassing the writer — this is how a
   *  truncated store, a hand edit, or a backward clock step presents on load. */
  function corrupt(id: string, patch: Record<string, unknown>): void {
    const raw = JSON.parse(window.localStorage.getItem(JOURNAL_STORAGE_KEY)!)

    Object.assign(raw.entries[id], patch)
    window.localStorage.setItem(JOURNAL_STORAGE_KEY, JSON.stringify(raw))
  }

  // Asserted RELATIVE to the constant, not against 20h/30h literals: a literal
  // pins the window from both sides, so any legitimate retune breaks a test that
  // has no opinion about the exact value. What the feature promises is
  // "recoverable inside the window, gone outside it".
  it('recovers a tail inside the retention window', () => {
    seed('stored-a')
    backdate(['stored-a'], JOURNAL_MAX_AGE_MS - HOUR_MS)

    expect(readInFlightTurnJournal('stored-a')).not.toBeNull()
  })

  it('drops a tail past the retention window', () => {
    seed('stored-a')
    backdate(['stored-a'], JOURNAL_MAX_AGE_MS + HOUR_MS)

    expect(readInFlightTurnJournal('stored-a')).toBeNull()
  })

  // The one directional assertion: tightening the window stays free, loosening
  // it past a day trips a deliberate review. An unredacted prompt at rest is
  // what is being bounded.
  it('keeps the retention ceiling at or under a day', () => {
    expect(JOURNAL_MAX_AGE_MS).toBeLessThanOrEqual(24 * HOUR_MS)
  })

  // `now - updatedAt > MAX_AGE_MS` fails OPEN on all three of these: every
  // comparison against NaN is false, and so is every negative delta. The store
  // envelope is validated on load but individual entries were not, so ONE bad
  // entry pinned its unredacted tail to disk permanently and the sweep walked
  // straight past it. Retention is the whole confidentiality argument here, so
  // an entry that cannot be dated inside the window must not be kept.
  it('drops an entry with no updatedAt instead of retaining it forever', () => {
    seed('stored-a')
    corrupt('stored-a', { updatedAt: undefined })

    expect(readInFlightTurnJournal('stored-a')).toBeNull()
    expect(storedKeys()).toEqual([])
  })

  it('drops an entry with a non-numeric updatedAt', () => {
    seed('stored-a')
    corrupt('stored-a', { updatedAt: '2026-05-01T00:00:00Z' })

    expect(readInFlightTurnJournal('stored-a')).toBeNull()
    expect(storedKeys()).toEqual([])
  })

  // A wall clock steps backward for real reasons: an NTP correction, a VM
  // snapshot restore, a dual-boot RTC write. A far-future stamp is a clock we
  // cannot reason about, so the tail goes rather than waiting for the clock to
  // catch up — which may be never.
  it('drops a future-dated entry left by a backward clock step', () => {
    seed('stored-a')
    corrupt('stored-a', { updatedAt: Date.now() + 3 * JOURNAL_MAX_AGE_MS })

    expect(readInFlightTurnJournal('stored-a')).toBeNull()
    expect(storedKeys()).toEqual([])
  })

  // The other half of that call: a sub-second correction during a LIVE turn must
  // not delete the turn the user is watching. Small skew stays recoverable.
  it('keeps an entry a small clock correction pushed slightly into the future', () => {
    seed('stored-a')
    corrupt('stored-a', { updatedAt: Date.now() + 2_000 })

    expect(readInFlightTurnJournal('stored-a')).not.toBeNull()
  })

  // A structurally unusable entry cannot be replayed (and throws in the merge),
  // so keeping it buys nothing and costs disk.
  it('drops a structurally invalid entry', () => {
    seed('stored-a')
    corrupt('stored-a', { messages: 'not-an-array' })

    expect(readInFlightTurnJournal('stored-a')).toBeNull()
    expect(storedKeys()).toEqual([])
  })

  // Nothing imports this module at boot, so the load-time sweep only runs once
  // some other call reaches it. A user who launches into a fresh draft and never
  // opens a session would never sweep — the bound has to hold without depending
  // on unrelated activity.
  it('sweeps without a session id so the bound does not need a session to be opened', () => {
    seed('stored-a')
    backdate(['stored-a'], JOURNAL_MAX_AGE_MS + HOUR_MS)

    sweepExpiredInFlightTurnJournals()

    expect(storedKeys()).toEqual([])
  })

  // The bug this fixes: expiry was only evaluated for the key being read, so a
  // session that crashed mid-turn and was never reopened kept its unredacted
  // tail on disk forever. Any load must sweep every expired entry off disk.
  it('sweeps expired entries for OTHER sessions off disk on any load', () => {
    seed('stored-keep')
    seed('stored-a')
    seed('stored-b')
    backdate(['stored-a', 'stored-b'], JOURNAL_MAX_AGE_MS + HOUR_MS)

    expect(storedKeys().sort()).toEqual(['stored-a', 'stored-b', 'stored-keep'])

    // Read an unrelated key — the stale siblings must still be evicted.
    readInFlightTurnJournal('stored-keep')

    expect(storedKeys()).toEqual(['stored-keep'])
  })
})

// Entry-level expiry made every INDIVIDUAL entry ageable, but the store
// ENVELOPE had the same fail-open shape one level up: an unparseable or
// foreign-version blob was answered with an empty store and left on disk, so
// `pruneExpiredEntries({})` swept 0 of 0, the `if (expired)` flush never fired,
// and the plaintext prompt + tool args stayed forever — beyond the reach of
// expiry AND of an explicit delete, since the purge loads through the same
// function and sees an empty store. Retention is the whole confidentiality
// argument for this file, so a state retention cannot reach is a hole in it.
//
// No corruption is needed to get here: a future STORE_VERSION bump, or a user
// downgrading the app after a newer version wrote entries, lands on the exact
// same branch.
describe('journal store envelope (retention must reach it too)', () => {
  const SECRET = 'PGPASSWORD=hunter2 psql -h prod'

  beforeEach(() => {
    window.localStorage.clear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    window.localStorage.clear()
    vi.useRealTimers()
  })

  /** A real journal blob written by the real writer, so the fixture carries the
   *  same plaintext an actual crashed turn would leave behind. */
  function seededRaw(): string {
    persistInFlightTurnState(
      journalState({
        messages: [user('u1', SECRET), assistant('assistant-stream-1', 'working', { pending: true })],
        storedSessionId: 'stored-a'
      })
    )
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    const raw = window.localStorage.getItem(JOURNAL_STORAGE_KEY)!

    expect(raw).toContain(SECRET)

    return raw
  }

  it('discards an unparseable store instead of leaving the plaintext on disk', () => {
    const raw = seededRaw()
    // Truncated blob: an interrupted write, a quota kill mid-`setItem`, a
    // half-synced leveldb file.
    window.localStorage.setItem(JOURNAL_STORAGE_KEY, raw.slice(0, Math.floor(raw.length / 2)))

    sweepExpiredInFlightTurnJournals()

    expect(window.localStorage.getItem(JOURNAL_STORAGE_KEY)).toBeNull()
  })

  it('discards a store written by a FUTURE version rather than stranding it unreadable', () => {
    const raw = seededRaw()
    const parsed = JSON.parse(raw)

    // What a downgrade sees: v2 wrote this, v1 is now reading it. v1 cannot
    // replay it and its very next write overwrites the key wholesale anyway, so
    // "keeping" it only means keeping unreadable plaintext at rest.
    window.localStorage.setItem(JOURNAL_STORAGE_KEY, JSON.stringify({ ...parsed, version: 99 }))

    sweepExpiredInFlightTurnJournals()

    expect(window.localStorage.getItem(JOURNAL_STORAGE_KEY)).toBeNull()
  })

  // `typeof null === 'object'` and `Array.isArray(null) === false`, so a null
  // `entries` passed the envelope guard and threw inside the sweep — landing in
  // the same silent fail-open path.
  it('discards a store whose entries map is null', () => {
    seededRaw()
    window.localStorage.setItem(JOURNAL_STORAGE_KEY, JSON.stringify({ entries: null, version: 1 }))

    sweepExpiredInFlightTurnJournals()

    expect(window.localStorage.getItem(JOURNAL_STORAGE_KEY)).toBeNull()
  })

  // The delete gesture is the user's explicit "remove this now". It must not be
  // silently defeated by a blob shape the reader happens not to understand.
  it('lets an explicit purge reach a corrupt store it cannot enumerate', () => {
    const raw = seededRaw()
    window.localStorage.setItem(JOURNAL_STORAGE_KEY, raw.slice(0, Math.floor(raw.length / 2)))

    purgeInFlightTurnJournals(['stored-a'])

    expect(window.localStorage.getItem(JOURNAL_STORAGE_KEY)).toBeNull()
  })

  it('leaves a well-formed current-version store alone', () => {
    seededRaw()

    sweepExpiredInFlightTurnJournals()

    expect(storedKeys()).toEqual(['stored-a'])
    expect(readInFlightTurnJournal('stored-a')).not.toBeNull()
  })
})

describe('purgeInFlightTurnJournals', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    purgeInFlightTurnJournals(['stored-doomed', 'stored-lineage', 'stored-survivor'])
    vi.useRealTimers()
  })

  // Deleting a session removes its authoritative history; the local tail must
  // go with it instead of waiting to age out.
  it('drops every id a deleted session may be journaled under, keeping others', () => {
    persistInFlightTurnState(journalState({ storedSessionId: 'stored-doomed' }))
    persistInFlightTurnState(journalState({ storedSessionId: 'stored-lineage' }))
    persistInFlightTurnState(journalState({ storedSessionId: 'stored-survivor' }))
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    expect(storedKeys().sort()).toEqual(['stored-doomed', 'stored-lineage', 'stored-survivor'])

    // Mirrors the call site: stored tip + lineage root + a null runtime id.
    purgeInFlightTurnJournals(['stored-doomed', 'stored-lineage', null, undefined])

    expect(storedKeys()).toEqual(['stored-survivor'])
  })

  // A throttled write already in flight must not resurrect the entry after the
  // session is gone.
  it('cancels a pending throttled write so a purged entry cannot come back', () => {
    persistInFlightTurnState(journalState({ storedSessionId: 'stored-doomed' }))

    expect(vi.getTimerCount()).toBe(1)

    purgeInFlightTurnJournals(['stored-doomed'])

    // Assert the TIMER, not just the map. `persistLatest.delete` alone already
    // starves the callback via its `if (latest)` guard, so an entry-only
    // assertion passes even with the `clearTimeout` removed — it would pin map
    // eviction while the docstring sells cancellation.
    expect(vi.getTimerCount()).toBe(0)

    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    expect(readInFlightTurnJournal('stored-doomed')).toBeNull()
    expect(storedKeys()).toEqual([])
  })

  it('is a no-op for unknown ids', () => {
    persistInFlightTurnState(journalState({ storedSessionId: 'stored-survivor' }))
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    purgeInFlightTurnJournals(['never-journaled'])

    expect(storedKeys()).toEqual(['stored-survivor'])
  })
})

// A stored session id ROTATES mid-turn: auto-compression forks a continuation
// and `ensureSessionState` swaps `storedSessionId` on the live state
// (use-session-state-cache.ts). The journal keys on that rotating tip, so one
// turn can own more than one key — and an INTERMEDIATE tip is in neither
// `$sessions` nor a delete's `removedIds`, so no call site can name it. Left
// unlinked, it holds the unredacted prompt and tool args for the full retention
// window and survives the user's delete gesture.
describe('stored-id rotation mid-turn', () => {
  const RUNTIME_ID = 'runtime-7'

  beforeEach(() => {
    window.localStorage.clear()
    vi.useFakeTimers()
  })

  afterEach(() => {
    purgeInFlightTurnJournals(['tip-1', 'tip-2', 'tip-3', 'other-tip', RUNTIME_ID, 'runtime-8', 'runtime-other'])
    vi.useRealTimers()
  })

  /** One session-state commit, as `updateSessionState` performs it: the state
   *  carries the CURRENT stored tip, the runtime id does not rotate. */
  function commit(storedSessionId: string, runtimeSessionId: null | string = RUNTIME_ID): void {
    persistInFlightTurnState(journalState({ storedSessionId }), runtimeSessionId)
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
  }

  function settle(storedSessionId: string, runtimeSessionId: null | string = RUNTIME_ID): void {
    persistInFlightTurnState(
      journalState({ awaitingResponse: false, busy: false, storedSessionId, streamId: null }),
      runtimeSessionId
    )
  }

  it('leaves no orphaned key when the tip rotates mid-turn', () => {
    commit('tip-1')
    expect(storedKeys()).toEqual(['tip-1'])

    // Compression rotates the tip; the same turn keeps streaming.
    commit('tip-2')
    expect(storedKeys()).toEqual(['tip-2'])

    settle('tip-2')
    expect(storedKeys()).toEqual([])
  })

  it('leaves no orphan across repeated rotations in one turn', () => {
    commit('tip-1')
    commit('tip-2')
    commit('tip-3')

    expect(storedKeys()).toEqual(['tip-3'])
  })

  /** Both keys of ONE rotated turn on disk under the same runtime id — the
   *  state a process leaves when it dies after the rotation write but before
   *  the turn settles. Built through the real writer, then the swept key is put
   *  back, because a live rotation self-cleans. */
  function seedCrashedRotation(): void {
    commit('tip-1')
    const orphaned = { ...storedEntries() }

    commit('tip-2')
    window.localStorage.setItem(
      JOURNAL_STORAGE_KEY,
      JSON.stringify({ entries: { ...storedEntries(), ...orphaned }, version: 1 })
    )
  }

  // A resume mints a NEW runtime id, so the first post-crash write cannot reach
  // the previous process's keys by its own id. It ADOPTS the runtime id recorded
  // on the key it is writing, which is what links it to the sibling left behind.
  it('drains a rotation sibling left by a previous process on the next write', () => {
    seedCrashedRotation()

    expect(storedKeys().sort()).toEqual(['tip-1', 'tip-2'])

    commit('tip-2', 'runtime-8')

    expect(storedKeys()).toEqual(['tip-2'])
  })

  // Same reach on the settle path: the turn ending must not leave the sibling.
  it('drains a rotation sibling when the turn settles', () => {
    seedCrashedRotation()

    settle('tip-2')

    expect(storedKeys()).toEqual([])
  })

  it('never touches an unrelated session journaled under its own runtime', () => {
    commit('other-tip', 'runtime-other')
    commit('tip-1')
    commit('tip-2')

    expect(storedKeys().sort()).toEqual(['other-tip', 'tip-2'])

    purgeInFlightTurnJournals(['tip-2', RUNTIME_ID])

    expect(storedKeys()).toEqual(['other-tip'])
  })

  // The reviewer's simulation, as a contract. Before the fix the sequence ended
  // with tip-1 surviving the delete; the delete's id set (removedIds + the
  // closing runtime id) cannot name an intermediate tip, so the runtime link
  // recorded on the entry is what reaches it.
  it('purges a pre-existing rotation orphan via the runtime id a delete holds', () => {
    seedCrashedRotation()

    expect(storedKeys().sort()).toEqual(['tip-1', 'tip-2'])

    // What removeSession passes: removedIds (the tip it knows + lineage root) and
    // the closing runtime id. 'tip-1' is in none of them.
    purgeInFlightTurnJournals(['tip-2', 'root-0', RUNTIME_ID])

    expect(storedKeys()).toEqual([])
  })

  // Recovery is the feature retention protects — a rotated turn must still be
  // recoverable under the tip a resume actually asks for.
  it('keeps the rotated turn recoverable under the live tip', () => {
    commit('tip-1')
    commit('tip-2')

    const recovered = recoverInFlightTurnJournal('tip-2', [], { keepPending: true })

    expect(recovered.applied).toBe(true)
    expect(recovered.messages.map(message => message.role)).toEqual(['user', 'assistant'])
  })

  // A purge cancels the pending writes it can SEE: the ids it was handed, plus
  // whatever `linkedKeys` finds ON DISK. A write that rotated to a brand-new tip
  // and is still inside the 400ms throttle is in neither set — the new key has
  // never been written, and the sidebar row still carries the old tip, so the
  // delete names the pre-rotation ids. The purge emptied the store and then the
  // throttle fired and put the turn straight back, AFTER an explicit delete.
  // `persistLatest` already knows that pending write's runtime id, so the purge
  // expands over it and not only over what happens to be on disk.
  it('cannot be resurrected by a write that rotated to an unwritten tip', () => {
    commit('tip-1')
    expect(storedKeys()).toEqual(['tip-1'])

    // Compression rotates to tip-2 and schedules the write; nothing on disk yet.
    persistInFlightTurnState(journalState({ storedSessionId: 'tip-2' }), RUNTIME_ID)
    expect(storedKeys()).toEqual(['tip-1'])

    // The user deletes. The row still carries the OLD tip, so this is exactly
    // what removeSession passes: removedIds + the closing runtime id.
    purgeInFlightTurnJournals(['tip-1', RUNTIME_ID])

    expect(storedKeys()).toEqual([])

    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    expect(storedKeys()).toEqual([])
    expect(window.localStorage.getItem(JOURNAL_STORAGE_KEY)).toBeNull()
  })

  // Same window, reached from the settle path rather than a delete.
  it('does not let a rotated pending write survive the turn settling', () => {
    commit('tip-1')
    persistInFlightTurnState(journalState({ storedSessionId: 'tip-2' }), RUNTIME_ID)

    clearInFlightTurnJournal('tip-1')
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    expect(storedKeys()).toEqual([])
  })

  // The guard must stay narrow: cancelling by runtime closure must not reach a
  // DIFFERENT session's pending write.
  it('leaves an unrelated session pending write intact', () => {
    commit('tip-1')
    persistInFlightTurnState(journalState({ storedSessionId: 'other-tip' }), 'runtime-other')

    purgeInFlightTurnJournals(['tip-1', RUNTIME_ID])
    vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)

    expect(storedKeys()).toEqual(['other-tip'])
  })
})
