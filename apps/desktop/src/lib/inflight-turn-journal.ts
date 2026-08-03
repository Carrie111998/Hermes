import { type ChatMessage, type ChatMessagePart, chatMessageText } from '@/lib/chat-messages'

/**
 * Crash-survivable in-flight turn journal.
 *
 * While a session is busy, the visible tail of the running turn (user prompt +
 * streamed assistant rows, tool calls included) is persisted to localStorage.
 * If the renderer or the whole app dies mid-turn, session resume folds the
 * journaled tail back onto the restored transcript, so streamed progress is
 * not silently lost. The backend's own `inflight` snapshot (merged by
 * `appendLiveSessionProjection`) covers reconnects while the backend is alive;
 * this journal covers the cases where the backend died too — and it is richer,
 * because the backend snapshot carries text only while the journal keeps the
 * full part structure.
 *
 * Best-effort by design: storage failures must never break chat streaming.
 *
 * ## Why the journaled tail is NOT redacted
 *
 * This tail is a REPLAY path, not a display log, so masking its content would
 * corrupt the thing it exists to restore:
 *
 * - The recovered user row is what `planReload` / `planRestore` read back out
 *   of the transcript and resubmit verbatim as `prompt.submit { text }`
 *   (`use-prompt-actions/rewind.ts`). A journaled `***` would be sent to the
 *   model as the user's actual prompt on the next Retry.
 * - The recovered tool-call parts are the structure the backend's text-only
 *   `inflight` snapshot cannot carry — the whole reason this journal exists.
 *
 * This is the desktop instance of the rule established by #43083
 * (`tests/agent/test_tool_call_arg_no_redaction.py`): redacting a value inside
 * a path that is replayed poisons the replay. Confidentiality here is bounded
 * by RETENTION, not by rewriting content. Four mechanisms carry that bargain,
 * and every one of them is load-bearing:
 *
 * 1. Expiry sweeps EVERY entry on any load, and fails closed — an entry that
 *    cannot be positively dated inside the window is dropped (`isExpired`).
 * 2. The entry is cleared the moment the turn settles or recovery reports
 *    `caughtUp`.
 * 3. Deleting a session purges it (`purgeInFlightTurnJournals`).
 * 4. A stored-id rotation mid-turn self-cleans the key it supersedes, so a
 *    rotation cannot strand a tail under a key nobody can name.
 *
 * Do not add masking here. Do weigh a change that weakens any of the four as a
 * security change.
 */

export const JOURNAL_STORAGE_KEY = 'hermes.desktop.inflightTurnJournal.v1'
const STORE_VERSION = 1
const MAX_ENTRIES = 24
/** A journaled tail is only worth replaying across an app restart: once the
 *  backend settles the turn, its committed reply is authoritative and recovery
 *  reports `caughtUp`. A day covers "it died last night, I reopened it this
 *  morning" while keeping unredacted prompts/tool args at rest for hours
 *  rather than a week.
 *
 *  DELIBERATELY NOT CONFIGURABLE. This is a renderer-local security floor, not
 *  a behavioral preference: retention is the whole confidentiality argument for
 *  storing an unredacted prompt at rest, so a `config.yaml` knob here would
 *  mostly give users a way to weaken their own protection. Tightening it is
 *  free (the test asserts a ceiling, not the literal); loosening it should
 *  require a deliberate review.
 *
 *  Accepted tradeoff: a turn parked over a weekend (~63h) now ages out where
 *  the old 7-day window would still have recovered it. A tail that old is
 *  almost always already superseded by the backend's committed reply, and an
 *  unredacted prompt sitting on disk for a week to serve that case is the
 *  worse half of the bargain. */
export const JOURNAL_MAX_AGE_MS = 24 * 60 * 60 * 1000
/** Streaming repaints arrive every ~33ms; localStorage writes are synchronous.
 *  Trailing-edge throttle keeps the journal off the hot path — a crash costs at
 *  most this much of the newest tail. */
export const JOURNAL_PERSIST_THROTTLE_MS = 400
/** How far a journal timestamp may sit in the future before the entry is
 *  treated as unusable rather than fresh. A wall clock can step BACKWARD at
 *  runtime (an NTP correction, a VM snapshot restore, a dual-boot RTC write),
 *  which makes an already-written entry look future-dated. A small tolerance
 *  keeps an ordinary sub-second correction from expiring the turn that is
 *  streaming right now; anything beyond it is a clock we cannot reason about,
 *  so the entry is dropped rather than pinned to disk (see `isExpired`). */
const CLOCK_SKEW_TOLERANCE_MS = 5 * 60 * 1000

export interface InFlightTurnSnapshot {
  messages: ChatMessage[]
  /** The RUNTIME session id that journaled this entry, when the writer knew it.
   *
   *  The store keys on the STORED session id because that is the only identity
   *  a fresh process has on resume — but the stored id ROTATES mid-turn (auto
   *  compression forks a continuation, `use-session-state-cache.ts`
   *  `ensureSessionState`), so one live turn can touch more than one key. The
   *  runtime id does not rotate, so recording it here is what makes the
   *  superseded key reachable: from any surviving entry of the turn we can
   *  enumerate its siblings and drop them (see `linkedKeys`). Without it an
   *  intermediate tip is in neither `$sessions` nor a delete's `removedIds`,
   *  and NO call site can name it.
   *
   *  Optional: entries written before this field existed (or by a caller that
   *  has no runtime id) simply carry no link and behave as they did before.
   *
   *  Known residual gap, stated rather than papered over: if the app dies inside
   *  the throttle window right after a SECOND-or-later rotation — so the new tip
   *  was never written — the old tip is on disk under a runtime id that no
   *  longer exists, and the next process has no entry to adopt it from. That one
   *  orphan is reachable only by its own key (a delete names the first rotation's
   *  old tip, since that IS the lineage root) or by expiry. It is bounded by
   *  `JOURNAL_MAX_AGE_MS`, which is why retention is the floor and this link is
   *  the optimization on top of it — not the other way round. */
  runtimeSessionId?: null | string
  streamId: null | string
  turnStartedAt: null | number
  updatedAt: number
}

export interface JournalableSessionState {
  awaitingResponse: boolean
  busy: boolean
  messages: ChatMessage[]
  storedSessionId: null | string
  streamId: null | string
  turnStartedAt: null | number
}

interface JournalStore {
  entries: Record<string, InFlightTurnSnapshot>
  version: typeof STORE_VERSION
}

export interface InFlightRecoveryResult {
  applied: boolean
  /** The base transcript already contains the journaled turn's completed
   *  reply — the journal entry is stale and has been cleared. */
  caughtUp: boolean
  messages: ChatMessage[]
  streamId: null | string
  turnStartedAt: null | number
}

function storage(): Storage | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage
  } catch {
    return null
  }
}

function emptyStore(): JournalStore {
  return { entries: {}, version: STORE_VERSION }
}

/** Drop every entry past `JOURNAL_MAX_AGE_MS`, not just the one being read.
 *
 * Expiry used to be checked only for the key a caller asked for (plus whatever
 * `saveStore` happened to filter on the next write), so a session that crashed
 * mid-turn and was never reopened kept its unredacted tail on disk
 * indefinitely — no read, no write, no prune. Sweeping on load makes the
 * retention window a real bound instead of an opportunistic one. */
function pruneExpiredEntries(entries: Record<string, InFlightTurnSnapshot>): {
  entries: Record<string, InFlightTurnSnapshot>
  expired: boolean
} {
  const kept = Object.entries(entries).filter(([, entry]) => !isExpired(entry))

  return {
    entries: kept.length === Object.keys(entries).length ? entries : Object.fromEntries(kept),
    expired: kept.length !== Object.keys(entries).length
  }
}

/** Sweep expired entries off disk without needing a session id.
 *
 * The sweep inside `loadStore` only runs when something else already calls into
 * this module (a resume, a state commit, a delete). A user who launches into a
 * fresh draft and never opens a session would never sweep, which would leave
 * the retention bound conditional on unrelated activity. The session-state
 * cache calls this once on mount so the bound holds unconditionally. */
export function sweepExpiredInFlightTurnJournals(): void {
  loadStore()
}

function loadStore(): JournalStore {
  const store = storage()

  if (!store) {
    return emptyStore()
  }

  try {
    const raw = store.getItem(JOURNAL_STORAGE_KEY)

    if (!raw) {
      return emptyStore()
    }

    const parsed = JSON.parse(raw)

    if (
      !parsed ||
      parsed.version !== STORE_VERSION ||
      typeof parsed.entries !== 'object' ||
      Array.isArray(parsed.entries)
    ) {
      return emptyStore()
    }

    const { entries, expired } = pruneExpiredEntries(parsed.entries as Record<string, InFlightTurnSnapshot>)
    const journal: JournalStore = { entries, version: STORE_VERSION }

    // Flush the sweep so the expired tail leaves disk on this load, even if the
    // caller never writes. `saveStore` is already failure-tolerant.
    if (expired) {
      saveStore(journal)
    }

    return journal
  } catch {
    return emptyStore()
  }
}

function saveStore(journal: JournalStore): void {
  const store = storage()

  if (!store) {
    return
  }

  try {
    const entries = Object.fromEntries(
      Object.entries(journal.entries)
        .filter(([, entry]) => !isExpired(entry))
        .sort((a, b) => b[1].updatedAt - a[1].updatedAt)
        .slice(0, MAX_ENTRIES)
    )

    if (Object.keys(entries).length === 0) {
      store.removeItem(JOURNAL_STORAGE_KEY)

      return
    }

    store.setItem(JOURNAL_STORAGE_KEY, JSON.stringify({ entries, version: STORE_VERSION }))
  } catch {
    // Quota/private-mode failures: the journal is a recovery aid, not truth.
  }
}

/** Fail CLOSED: anything we cannot positively date within the window is
 *  expired.
 *
 *  `now - entry.updatedAt > MAX_AGE_MS` alone fails OPEN in three ways, because
 *  every comparison against `NaN` — and every negative delta — is `false`:
 *
 *  - `updatedAt` missing or non-numeric (a hand-edited or truncated store, a
 *    future schema change) → `NaN > MAX_AGE_MS` is `false` → retained forever.
 *  - `updatedAt` in the future (the wall clock stepped backward) → negative
 *    delta → retained until the clock catches up, which may be never.
 *
 *  `loadStore` validates the store envelope but not individual entries, so one
 *  such entry pinned its unredacted tail to disk permanently and the retention
 *  sweep walked straight past it. Retention is the entire confidentiality
 *  argument for this file, so an entry that cannot be aged must not be kept.
 *
 *  Structurally unusable entries are folded into the same predicate: an entry
 *  whose `messages` is not an array cannot be replayed (and would throw in the
 *  merge), so keeping it buys nothing and costs disk. */
function isExpired(entry: InFlightTurnSnapshot, now = Date.now()): boolean {
  if (!entry || typeof entry !== 'object' || !Array.isArray(entry.messages)) {
    return true
  }

  const age = now - entry.updatedAt

  return !Number.isFinite(age) || age < -CLOCK_SKEW_TOLERANCE_MS || age > JOURNAL_MAX_AGE_MS
}

function cloneMessages(messages: ChatMessage[]): ChatMessage[] {
  try {
    return JSON.parse(JSON.stringify(messages)) as ChatMessage[]
  } catch {
    return []
  }
}

function normalizedText(value: string): string {
  return value.replace(/\s+/g, ' ').trim()
}

function attachmentSignature(message: ChatMessage): string {
  return (message.attachmentRefs ?? []).join('\n')
}

function userMessagesMatch(left: ChatMessage, right: ChatMessage): boolean {
  return (
    left.role === 'user' &&
    right.role === 'user' &&
    normalizedText(chatMessageText(left)) === normalizedText(chatMessageText(right)) &&
    attachmentSignature(left) === attachmentSignature(right)
  )
}

function partHasRecoverableContent(part: ChatMessagePart): boolean {
  if (part.type === 'text' || part.type === 'reasoning') {
    return typeof part.text === 'string' && part.text.trim().length > 0
  }

  return part.type === 'tool-call'
}

function assistantHasRecoverableContent(message: ChatMessage): boolean {
  return message.role === 'assistant' && (Boolean(message.error) || message.parts.some(partHasRecoverableContent))
}

/** A live-turn projection row (backend `inflight` via appendLiveSessionProjection,
 *  or a still-streaming local bubble) — as opposed to a completed transcript row. */
function isLiveProjectionRow(message: ChatMessage): boolean {
  return (
    Boolean(message.pending) ||
    message.id.startsWith('assistant-stream-') ||
    message.id.startsWith('inflight-assistant-')
  )
}

/** Visible tail of the running turn: the streaming assistant row (plus any
 *  interim rows sealed after it) back to the user prompt that started it. */
function recoverableTail(messages: ChatMessage[], streamId: null | string): ChatMessage[] {
  const visible = messages.filter(message => !message.hidden)
  let assistantIndex = -1

  if (streamId) {
    assistantIndex = visible.findIndex(message => message.id === streamId && assistantHasRecoverableContent(message))
  }

  if (assistantIndex < 0) {
    for (let index = visible.length - 1; index >= 0; index -= 1) {
      const message = visible[index]

      if (message.role === 'user') {
        break
      }

      if (assistantHasRecoverableContent(message)) {
        assistantIndex = index

        break
      }
    }
  }

  if (assistantIndex < 0) {
    return []
  }

  let start = assistantIndex

  for (let index = assistantIndex - 1; index >= 0; index -= 1) {
    if (visible[index].role === 'user') {
      start = index

      // A mid-turn redirect inserts its correction as another user row right
      // before the live reply, so the turn can open with a RUN of user rows.
      // Keep walking back over them: stopping at the nearest one journals the
      // correction alone and loses the prompt that actually started the turn.
      while (start > 0 && visible[start - 1].role === 'user') {
        start -= 1
      }

      break
    }
  }

  return cloneMessages(visible.slice(start))
}

function normalizeRecoveredTail(tail: ChatMessage[], keepPending: boolean): ChatMessage[] {
  return cloneMessages(tail).map(message =>
    message.role === 'assistant'
      ? {
          ...message,
          pending: keepPending ? (message.pending ?? true) : false
        }
      : { ...message, pending: false }
  )
}

function assistantTextLength(message: ChatMessage): number {
  return chatMessageText(message).length
}

/** Merge the journal's last assistant row into the base's live projection row.
 *
 * The journal carries structure (tool calls, reasoning) the backend snapshot
 * lacks; the backend text may be newer than the journal's last throttled
 * write. Keep the journal's parts, but let the longer text win — and keep the
 * BASE row's id so live deltas keep appending to the row the stream handler
 * already targets.
 */
function overlayProjectionRow(projection: ChatMessage, journalRow: ChatMessage): ChatMessage {
  // A projected error (retained failed turn) must survive the overlay.
  const error = journalRow.error ?? projection.error

  const merged: ChatMessage = {
    ...journalRow,
    id: projection.id,
    pending: projection.pending,
    ...(error ? { error } : {})
  }

  if (assistantTextLength(projection) <= assistantTextLength(journalRow)) {
    return merged
  }

  // Backend text is newer than the journal's last throttled write — swap it
  // into the journal's first text part, keeping tool calls and reasoning.
  const projectionText = chatMessageText(projection)
  const parts: ChatMessagePart[] = []
  let textReplaced = false

  for (const part of journalRow.parts) {
    if (part.type !== 'text') {
      parts.push(part)
    } else if (!textReplaced) {
      parts.push({ ...part, text: projectionText })
      textReplaced = true
    }
  }

  if (!textReplaced) {
    parts.push({ type: 'text', text: projectionText })
  }

  return { ...merged, parts }
}

/** Rows the base transcript doesn't already hold by id. The journal and the
 *  base can both carry the same row (a resume that replays a still-journaled
 *  turn), and appending it twice puts a duplicate id in the transcript —
 *  which assistant-ui's MessageRepository rejects by throwing. */
function withoutBaseIds(rows: ChatMessage[], baseMessages: ChatMessage[]): ChatMessage[] {
  const baseIds = new Set(baseMessages.map(message => message.id))

  return rows.filter(row => !baseIds.has(row.id))
}

export function mergeInFlightMessages(
  baseMessages: ChatMessage[],
  tailMessages: ChatMessage[],
  options: { keepPending?: boolean } = {}
): InFlightRecoveryResult {
  const noop: InFlightRecoveryResult = {
    applied: false,
    caughtUp: false,
    messages: baseMessages,
    streamId: null,
    turnStartedAt: null
  }

  const tail = normalizeRecoveredTail(tailMessages, Boolean(options.keepPending))

  if (!tail.some(assistantHasRecoverableContent)) {
    return noop
  }

  const tailUserIndex = tail.findIndex(message => message.role === 'user')
  const tailUser = tailUserIndex >= 0 ? tail[tailUserIndex] : null
  const tailAssistants = tail.slice(tailUserIndex + 1)
  const lastJournalRow = tailAssistants.findLast(assistantHasRecoverableContent) ?? null
  const matchingUserIndex = tailUser ? baseMessages.findLastIndex(message => userMessagesMatch(message, tailUser)) : -1

  if (matchingUserIndex < 0) {
    // Base doesn't know this turn at all (user row was never persisted):
    // append the whole tail.
    const streamId = lastJournalRow?.id ?? null

    return {
      applied: true,
      caughtUp: false,
      messages: [...baseMessages, ...withoutBaseIds(tail, baseMessages)],
      streamId,
      turnStartedAt: null
    }
  }

  const afterUser = baseMessages.slice(matchingUserIndex + 1)

  const completedReply = afterUser.find(
    message => assistantHasRecoverableContent(message) && !isLiveProjectionRow(message)
  )

  if (completedReply) {
    // The transcript already holds this turn's committed reply — the journal
    // entry is stale.
    return { ...noop, caughtUp: true }
  }

  const projectionIndex = baseMessages.findIndex(
    (message, index) => index > matchingUserIndex && message.role === 'assistant' && isLiveProjectionRow(message)
  )

  if (projectionIndex < 0) {
    if (tailAssistants.length === 0) {
      return noop
    }

    const streamId = lastJournalRow?.id ?? null

    return {
      applied: true,
      caughtUp: false,
      messages: [...baseMessages, ...withoutBaseIds(tailAssistants, baseMessages)],
      streamId,
      turnStartedAt: null
    }
  }

  // Backend projection row present (text-only): overlay the journal's
  // structure onto it instead of treating it as "caught up" — that is how
  // locally recorded tool progress used to get dropped.
  const projection = baseMessages[projectionIndex]
  const merged = lastJournalRow ? overlayProjectionRow(projection, lastJournalRow) : projection

  const sealedRows = tailAssistants.filter(
    message => message !== lastJournalRow && assistantHasRecoverableContent(message)
  )

  const messages = [
    ...baseMessages.slice(0, projectionIndex),
    ...sealedRows,
    merged,
    ...baseMessages.slice(projectionIndex + 1)
  ]

  return { applied: true, caughtUp: false, messages, streamId: merged.id, turnStartedAt: null }
}

const persistTimers = new Map<string, ReturnType<typeof setTimeout>>()
const persistLatest = new Map<string, { runtimeSessionId: null | string; state: JournalableSessionState }>()

/** Every key that belongs to the same live turn as `seedIds`.
 *
 *  The store keys on the rotating stored id, so one turn can own several keys
 *  (see `InFlightTurnSnapshot.runtimeSessionId`). Expansion is: the named ids,
 *  plus every entry whose `runtimeSessionId` matches either a named id or the
 *  runtime id recorded on a named entry. That closure is what lets a caller
 *  drain a rotated turn while naming only the id it happens to hold. */
function linkedKeys(
  entries: Record<string, InFlightTurnSnapshot>,
  seedIds: readonly string[],
  extraRuntimeIds: readonly (null | string | undefined)[] = []
): string[] {
  const named = new Set(seedIds)
  const runtimes = new Set<string>()

  for (const id of [...seedIds, ...extraRuntimeIds]) {
    // A caller-held id may itself BE a runtime id (a delete passes the closing
    // runtime id), which is how a rotation orphan gets reached when the
    // intermediate tip is in neither $sessions nor the delete's removedIds.
    if (id) {
      runtimes.add(id)
    }
  }

  for (const id of named) {
    const runtimeSessionId = entries[id]?.runtimeSessionId

    if (runtimeSessionId) {
      runtimes.add(runtimeSessionId)
    }
  }

  return Object.keys(entries).filter(key => {
    const runtimeSessionId = entries[key]?.runtimeSessionId

    return named.has(key) || (Boolean(runtimeSessionId) && runtimes.has(runtimeSessionId as string))
  })
}

/** Drop a throttled write that is about to be superseded, so it cannot land
 *  after the entry it targets has been removed. */
function dropPendingWrite(storedSessionId: string): void {
  const timer = persistTimers.get(storedSessionId)

  if (timer) {
    clearTimeout(timer)
    persistTimers.delete(storedSessionId)
  }

  persistLatest.delete(storedSessionId)
}

/** Remove `seedIds` and every key linked to the same turn. Returns the keys
 *  actually dropped. */
function purgeLinkedEntries(
  seedIds: readonly (null | string | undefined)[],
  extraRuntimeIds: readonly (null | string | undefined)[] = []
): string[] {
  const ids = [...new Set(seedIds.filter((id): id is string => Boolean(id)))]

  for (const id of ids) {
    dropPendingWrite(id)
  }

  const journal = loadStore()
  const doomed = linkedKeys(journal.entries, ids, extraRuntimeIds)

  if (doomed.length === 0) {
    return []
  }

  for (const key of doomed) {
    dropPendingWrite(key)
    delete journal.entries[key]
  }

  saveStore(journal)

  return doomed
}

function writeSnapshot(storedSessionId: string, state: JournalableSessionState, runtimeSessionId: null | string): void {
  const tail = recoverableTail(state.messages, state.streamId)

  if (tail.length === 0) {
    return
  }

  const journal = loadStore()

  // Self-clean the rotation. The stored id rotates mid-turn (auto compression),
  // so this write may be landing under a NEW key while the turn's previous key
  // still holds the same prompt and tool args. Nothing else would ever clear it:
  // settle only clears the current id, and an intermediate tip is not in
  // $sessions, so no delete can name it. Sweep the turn's other keys here, where
  // the link is still known.
  //
  // `previousRuntimeSessionId` handles the resume case: this key's existing
  // entry was written by the PREVIOUS process under a now-dead runtime id, and
  // adopting it lets the first post-crash write drain orphans that process left.
  const previousRuntimeSessionId = journal.entries[storedSessionId]?.runtimeSessionId

  const superseded = runtimeSessionId
    ? linkedKeys(journal.entries, [storedSessionId], [runtimeSessionId, previousRuntimeSessionId]).filter(
        key => key !== storedSessionId
      )
    : []

  for (const key of superseded) {
    dropPendingWrite(key)
    delete journal.entries[key]
  }

  journal.entries[storedSessionId] = {
    messages: tail,
    ...(runtimeSessionId ? { runtimeSessionId } : {}),
    streamId: state.streamId,
    turnStartedAt: state.turnStartedAt,
    updatedAt: Date.now()
  }
  saveStore(journal)
}

/** Persist the running turn's visible tail (throttled), or clear the entry the
 *  moment the turn settles. Call on every session-state commit.
 *
 *  `runtimeSessionId` is the session's RUNTIME id (the one that does not rotate
 *  when compression forks a continuation). It is optional only so tests and
 *  non-runtime callers stay simple — pass it from the real write path, or a
 *  rotated key has no link and can orphan. */
export function persistInFlightTurnState(state: JournalableSessionState, runtimeSessionId: null | string = null): void {
  const storedSessionId = state.storedSessionId

  if (!storedSessionId) {
    return
  }

  if (!state.busy && !state.awaitingResponse && !state.streamId) {
    clearInFlightTurnJournal(storedSessionId)

    return
  }

  persistLatest.set(storedSessionId, { runtimeSessionId, state })

  if (persistTimers.has(storedSessionId)) {
    return
  }

  persistTimers.set(
    storedSessionId,
    setTimeout(() => {
      persistTimers.delete(storedSessionId)
      const latest = persistLatest.get(storedSessionId)

      persistLatest.delete(storedSessionId)

      if (latest) {
        writeSnapshot(storedSessionId, latest.state, latest.runtimeSessionId)
      }
    }, JOURNAL_PERSIST_THROTTLE_MS)
  )
}

export function readInFlightTurnJournal(storedSessionId: null | string): InFlightTurnSnapshot | null {
  if (!storedSessionId) {
    return null
  }

  const journal = loadStore()
  const entry = journal.entries[storedSessionId]

  if (!entry) {
    return null
  }

  if (isExpired(entry)) {
    delete journal.entries[storedSessionId]
    saveStore(journal)

    return null
  }

  return entry
}

/** Fold a journaled in-flight tail back onto a restored transcript. A no-op
 *  returns `baseMessages` by reference so callers keep their fast-path ref. */
export function recoverInFlightTurnJournal(
  storedSessionId: null | string,
  baseMessages: ChatMessage[],
  options: { keepPending?: boolean } = {}
): InFlightRecoveryResult {
  const snapshot = readInFlightTurnJournal(storedSessionId)

  if (!snapshot) {
    return {
      applied: false,
      caughtUp: false,
      messages: baseMessages,
      streamId: null,
      turnStartedAt: null
    }
  }

  const recovered = mergeInFlightMessages(baseMessages, snapshot.messages, options)

  if (recovered.caughtUp) {
    clearInFlightTurnJournal(storedSessionId)
  }

  return {
    ...recovered,
    streamId: recovered.applied ? (recovered.streamId ?? snapshot.streamId) : null,
    turnStartedAt: recovered.applied ? snapshot.turnStartedAt : null
  }
}

/** Clear a settled turn's journal entry — and any key the same turn left behind
 *  when the stored id rotated mid-turn, which `storedSessionId` alone does not
 *  name (see `InFlightTurnSnapshot.runtimeSessionId`). */
export function clearInFlightTurnJournal(storedSessionId: null | string): void {
  purgeLinkedEntries([storedSessionId])
}

/** Purge journaled tails for a deleted session.
 *
 *  Deleting a session removes its authoritative history, but the journal used
 *  to keep that turn's prompt and tool calls in localStorage until the entry
 *  aged out — the delete gesture did not reach the local copy (the sibling
 *  `composer-queue` store was already cleared here; this one was missed).
 *
 *  Coverage, stated precisely, because retention is the confidentiality
 *  argument for this file and an overclaiming docstring is worse than none:
 *  this drains every id passed in, PLUS every entry linked to one of them by
 *  `runtimeSessionId`. The link is what covers compression rotation — a turn
 *  whose stored id rotated has a key that is in neither `$sessions` nor a
 *  delete's `removedIds`, so no caller can name it directly. An entry written
 *  with no runtime id (pre-existing store, non-runtime caller) is only reachable
 *  by its own key; pass every id you hold.
 *
 *  Callers pass a list because a session has more than one identity: the stored
 *  tip rotates on compression, the lineage root differs from it, and the runtime
 *  id is a third. All of them drain in a single load/save. */
export function purgeInFlightTurnJournals(sessionIds: readonly (null | string | undefined)[]): void {
  purgeLinkedEntries(sessionIds)
}
