import { atom } from 'nanostores'

import type { ComposerAttachment } from './composer'

export interface QueuedPromptEntry {
  id: string
  text: string
  attachments: ComposerAttachment[]
  queuedAt: number
}

type QueueState = Record<string, QueuedPromptEntry[]>

export const QUEUE_STORAGE_KEY = 'hermes.desktop.composerQueue.v1'

/** See the tombstone block below for why this lives under its own key. */
export const QUEUE_TOMBSTONES_STORAGE_KEY = 'hermes.desktop.composerQueue.sent.v1'

// True after a persistence attempt failed; cleared by the next success. While
// set, mutations and drain picks base themselves on the in-memory atom instead
// of storage — entries that never reached storage would otherwise vanish from
// every subsequent operation. Storage that cannot be written cannot sync
// windows anyway, so degrading to single-window in-memory semantics is the
// correct fallback, and recovery is automatic on the first save that succeeds.
let persistFailed = false

// Cache of the last parsed queue map, keyed by the exact raw string. Repeated
// reads (every mutation + drain pick + storage event re-reads the whole map)
// then parse once per distinct content, and — critically — return the SAME
// per-session array references, preserving the referential stability that
// useSessionSlice's re-render bail-out depends on.
let cachedQueueRaw: null | string = null
let cachedQueueState: null | QueueState = null

// null = storage unreadable (disabled/corrupt access), distinct from empty.
// Storage content is attacker-adjacent input (another app version, manual
// edits, corruption): sanitize per session and per entry, or a non-array
// session value would make the very first filterState throw during module
// evaluation and brick every window.
const sanitizeState = (parsed: unknown): QueueState => {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {}
  }

  const state: QueueState = {}

  for (const [sid, queue] of Object.entries(parsed as Record<string, unknown>)) {
    if (!Array.isArray(queue)) {
      continue
    }

    const entries = queue.filter(
      (e): e is QueuedPromptEntry =>
        !!e &&
        typeof e === 'object' &&
        typeof (e as { id?: unknown }).id === 'string' &&
        typeof (e as { text?: unknown }).text === 'string' &&
        Array.isArray((e as { attachments?: unknown }).attachments)
    )

    if (entries.length > 0) {
      state[sid] = entries
    }
  }

  return state
}

const readStorage = (): null | QueueState => {
  if (typeof window === 'undefined') {
    return null
  }

  try {
    const raw = window.localStorage.getItem(QUEUE_STORAGE_KEY)

    if (raw === null) {
      return {}
    }

    if (cachedQueueRaw === raw && cachedQueueState) {
      return cachedQueueState
    }

    const state = sanitizeState(JSON.parse(raw) as unknown)

    cachedQueueRaw = raw
    cachedQueueState = state

    return state
  } catch {
    return null
  }
}

const save = (state: QueueState) => {
  if (typeof window === 'undefined') {
    return
  }

  try {
    if (Object.keys(state).length === 0) {
      window.localStorage.removeItem(QUEUE_STORAGE_KEY)
      cachedQueueRaw = null
      cachedQueueState = null
    } else {
      const raw = JSON.stringify(state)
      window.localStorage.setItem(QUEUE_STORAGE_KEY, raw)
      cachedQueueRaw = raw
      cachedQueueState = state
    }

    persistFailed = false
  } catch {
    // Best-effort: the queue keeps working in-memory — freshState() serves the
    // atom while this flag is set, so nothing is lost from the UI or drains.
    persistFailed = true
  }
}

// ---------------------------------------------------------------------------
// Removal tombstones.
//
// The queue map alone cannot survive one interleaving: a mutation in window B
// that loaded the map BEFORE window A removed entry X and saved AFTER puts X
// back into storage. If X was just drained, the next auto-drain would submit
// it a second time; if the user deleted X, it would send something they
// explicitly discarded. localStorage has no compare-and-swap and synchronous
// mutators cannot take async Web Locks, so instead every removal records the
// entry id here FIRST, and every queue read/write filters tombstoned ids — a
// resurrected entry can reappear in the raw map, but never in a drainable or
// visible state; the storage listener writes the purged map back so the ghost
// leaves storage within one event round trip. The sidecar lives under its own
// key so queue-map writes can never clobber it wholesale; concurrent sidecar
// writers can at worst lose one id to each other (falling back to today's
// behavior, never worse). The cap bounds its size; the TTL exists only to shed
// abandoned ids and is deliberately long — a resurrected ghost must never
// outlive the tombstone that hides it, and ghosts are purged within one
// storage event or the next write, both far inside the TTL.
// ---------------------------------------------------------------------------

type RemovalTombstones = Record<string, number>

const TOMBSTONE_TTL_MS = 24 * 60 * 60_000
const TOMBSTONE_CAP = 64

let cachedTombstonesRaw: null | string = null
let cachedTombstones: null | RemovalTombstones = null

// Removals recorded while the sidecar was unwritable (quota). Merged into
// every read so THIS window keeps filtering its own removals even when it
// cannot tell the other windows about them; bounded by the same prune+cap.
let unpersistedTombstones: RemovalTombstones = {}

const sanitizeTombstones = (parsed: unknown): RemovalTombstones => {
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return {}
  }

  const tombstones: RemovalTombstones = {}

  for (const [id, at] of Object.entries(parsed as Record<string, unknown>)) {
    if (typeof at === 'number' && Number.isFinite(at)) {
      tombstones[id] = at
    }
  }

  return tombstones
}

const readTombstones = (): RemovalTombstones => {
  if (typeof window === 'undefined') {
    return {}
  }

  const overlay = unpersistedTombstones

  try {
    const raw = window.localStorage.getItem(QUEUE_TOMBSTONES_STORAGE_KEY)

    if (raw === null) {
      return overlay
    }

    if (cachedTombstonesRaw !== raw || !cachedTombstones) {
      cachedTombstonesRaw = raw
      cachedTombstones = sanitizeTombstones(JSON.parse(raw) as unknown)
    }

    return Object.keys(overlay).length === 0 ? cachedTombstones : { ...cachedTombstones, ...overlay }
  } catch {
    return overlay
  }
}

const pruneTombstones = (tombstones: RemovalTombstones): RemovalTombstones => {
  const cutoff = Date.now() - TOMBSTONE_TTL_MS
  const alive = Object.entries(tombstones).filter(([, at]) => at > cutoff)
  alive.sort((a, b) => a[1] - b[1])

  return Object.fromEntries(alive.slice(-TOMBSTONE_CAP))
}

const recordTombstones = (ids: string[]) => {
  if (typeof window === 'undefined' || ids.length === 0) {
    return
  }

  const next = pruneTombstones(readTombstones())
  const now = Date.now()

  for (const id of ids) {
    next[id] = now
  }

  try {
    const raw = JSON.stringify(next)
    window.localStorage.setItem(QUEUE_TOMBSTONES_STORAGE_KEY, raw)
    cachedTombstonesRaw = raw
    cachedTombstones = next
    unpersistedTombstones = {}
  } catch {
    // Sidecar unwritable: keep the merged set in memory so this window still
    // filters its own removals; other windows degrade to pre-tombstone behavior.
    unpersistedTombstones = next
  }
}

/** Preserves the input reference when nothing is filtered. */
const filterTombstoned = (queue: QueuedPromptEntry[], tombstones = readTombstones()): QueuedPromptEntry[] => {
  const cutoff = Date.now() - TOMBSTONE_TTL_MS
  const next = queue.filter(e => !(tombstones[e.id] !== undefined && tombstones[e.id]! > cutoff))

  return next.length === queue.length ? queue : next
}

/** Filter every session's queue; preserves references (and the map) when clean. */
const filterState = (state: QueueState): QueueState => {
  const tombstones = readTombstones()
  let changed = false
  const next: QueueState = {}

  for (const [sid, queue] of Object.entries(state)) {
    const filtered = filterTombstoned(queue, tombstones)

    if (filtered.length > 0) {
      next[sid] = filtered
    }

    if (filtered !== queue || filtered.length === 0) {
      changed = true
    }
  }

  return changed ? next : state
}

export const $queuedPromptsBySession = atom<QueueState>(filterState(readStorage() ?? {}))

// Cross-window sync: every desktop window boots this store from the same
// localStorage key, but without this listener each window keeps a private,
// diverging snapshot forever. A window that enqueues/drains would then clobber
// the others' entries on its next save (#46732: prompts resurrecting, vanishing,
// or draining from a window the user never typed in). `storage` fires only in
// the windows that did NOT write, so there is no self-echo to guard against.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (
      event.key !== QUEUE_STORAGE_KEY &&
      event.key !== QUEUE_TOMBSTONES_STORAGE_KEY &&
      event.key !== null
    ) {
      return
    }

    // While persistence is broken, this window's atom is its only truth:
    // adopting another window's map would delete every in-memory-only entry.
    if (persistFailed) {
      return
    }

    const stored = readStorage()

    if (stored === null) {
      return
    }

    const filtered = filterState(stored)
    $queuedPromptsBySession.set(filtered)

    // A stale save resurrected a tombstoned entry into the raw map: write the
    // purged map back so the ghost leaves storage now, not "on the next
    // write" — an idle queue might not see one before the tombstone expires.
    // Idempotent across windows: whoever writes last writes the same content.
    if (filtered !== stored) {
      save(filtered)
    }
  })
}

// The freshest queue map a mutation or drain pick may act on: persisted
// storage — the only synchronously cross-window source — unless persistence is
// broken or storage is unreadable, in which case the in-memory atom is the
// best (and only) truth this window has.
const freshState = (): QueueState =>
  persistFailed ? $queuedPromptsBySession.get() : (readStorage() ?? $queuedPromptsBySession.get())

// Map-level operation-based mutation: `op` receives a copy of the freshest
// visible state and returns the next state, or null for "nothing to do".
// Basing every write on freshState() — never on an array a caller computed
// from the atom — is what stops two windows from replacing the same session's
// queue with competing stale versions (#57516 review). The remaining race is
// two renderer processes interleaving inside one another's load→op→save; its
// worst case is a briefly resurrected or lost entry in the raw map, and the
// tombstone filter above guarantees a resurrected entry is never drained or
// shown again. Cross-window double-SUBMIT protection does not rest on this
// path at all — that is withSessionDrainClaim's job.
const mutateState = (op: (fresh: QueueState) => null | QueueState): boolean => {
  const next = op({ ...freshState() })

  if (next === null) {
    return false
  }

  const filtered = filterState(next)
  $queuedPromptsBySession.set(filtered)
  save(filtered)

  return true
}

// Returns whether `op` applied (its null = "didn't apply", which is what the
// mutators' booleans report). The write itself can additionally happen for a
// non-applying op when tombstoned entries got purged from the stored queue:
// the remove/clear path tombstones FIRST, so its op sees the entry already
// filtered out — but the atom and storage still hold it and must be rewritten
// without it.
const mutateSession = (sid: string, op: (fresh: QueuedPromptEntry[]) => null | QueuedPromptEntry[]): boolean => {
  let applied = false

  mutateState(state => {
    const stored = state[sid] ?? []
    const filtered = filterTombstoned(stored)
    const queue = op(filtered)
    applied = queue !== null

    if (queue === null && filtered === stored) {
      return null
    }

    const next = queue ?? filtered

    if (next.length === 0) {
      delete state[sid]
    } else {
      state[sid] = next
    }

    return state
  })

  return applied
}

const sidOf = (key: string | null | undefined): null | string => {
  const trimmed = key?.trim()

  return trimmed ? trimmed : null
}

const queueFor = (sid: string) => $queuedPromptsBySession.get()[sid] ?? []

const nextId = () => `queued-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

const cloneAttachments = (attachments: ComposerAttachment[]) => attachments.map(a => ({ ...a }))

/**
 * Rendered-state reader (the atom): right for display and render-time checks.
 * Drains and mutations must NOT pick from this — the storage event that syncs
 * the atom is asynchronous, so it can trail what another window just did; they
 * use {@link readFreshQueuedPrompts} / the internal fresh-state path instead.
 */
export const getQueuedPrompts = (key: string | null | undefined): QueuedPromptEntry[] => {
  const sid = sidOf(key)

  return sid ? queueFor(sid) : []
}

/**
 * Read a session's queue from the freshest source (persisted storage, or the
 * atom when persistence is broken), with removal tombstones filtered. This is
 * the ONLY read drain paths may pick from, inside {@link withSessionDrainClaim}.
 */
export const readFreshQueuedPrompts = (key: string | null | undefined): QueuedPromptEntry[] => {
  const sid = sidOf(key)

  return sid ? filterTombstoned(freshState()[sid] ?? []) : []
}

export const enqueueQueuedPrompt = (
  key: string | null | undefined,
  payload: { text: string; attachments: ComposerAttachment[] }
): null | QueuedPromptEntry => {
  const sid = sidOf(key)

  if (!sid) {
    return null
  }

  const entry: QueuedPromptEntry = {
    id: nextId(),
    text: payload.text,
    attachments: cloneAttachments(payload.attachments),
    queuedAt: Date.now()
  }

  mutateSession(sid, fresh => [...fresh, entry])

  return entry
}

export const dequeueQueuedPrompt = (key: string | null | undefined): null | QueuedPromptEntry => {
  const sid = sidOf(key)

  if (!sid) {
    return null
  }

  const head = readFreshQueuedPrompts(sid)[0] ?? null

  if (!head) {
    return null
  }

  removeQueuedPrompt(sid, head.id)

  return head
}

export const removeQueuedPrompt = (key: string | null | undefined, id: string): boolean => {
  const sid = sidOf(key)

  if (!sid) {
    return false
  }

  if (!readFreshQueuedPrompts(sid).some(e => e.id === id)) {
    return false
  }

  // Tombstone FIRST: from this instant no stale concurrent save can resurrect
  // the entry into a drainable or visible state, even though the map write
  // below has not happened yet.
  recordTombstones([id])

  mutateSession(sid, fresh => {
    const next = fresh.filter(e => e.id !== id)

    return next.length === fresh.length ? null : next
  })

  return true
}

export const promoteQueuedPrompt = (key: string | null | undefined, id: string): boolean => {
  const sid = sidOf(key)

  if (!sid) {
    return false
  }

  return mutateSession(sid, fresh => {
    const index = fresh.findIndex(e => e.id === id)

    if (index <= 0) {
      return null
    }

    const entry = fresh[index]!

    return [entry, ...fresh.slice(0, index), ...fresh.slice(index + 1)]
  })
}

export const updateQueuedPrompt = (
  key: string | null | undefined,
  id: string,
  update: { text: string; attachments?: ComposerAttachment[] }
): boolean => {
  const sid = sidOf(key)

  if (!sid) {
    return false
  }

  return mutateSession(sid, fresh => {
    let changed = false

    const next = fresh.map(entry => {
      if (entry.id !== id) {
        return entry
      }

      const attachments = update.attachments ? cloneAttachments(update.attachments) : entry.attachments

      // Structural comparison, not presence: edit flows always pass an
      // attachments array (usually an identical one), and treating "passed"
      // as "changed" made every arrow-step through the edit stack a full
      // stringify + storage write + cross-window event for nothing.
      const sameAttachments =
        !update.attachments || JSON.stringify(update.attachments) === JSON.stringify(entry.attachments)

      if (entry.text === update.text && sameAttachments) {
        return entry
      }

      changed = true

      return { ...entry, text: update.text, attachments }
    })

    return changed ? next : null
  })
}

export const updateQueuedPromptText = (key: string | null | undefined, id: string, text: string): boolean =>
  updateQueuedPrompt(key, id, { text })

export const clearQueuedPrompts = (key: string | null | undefined) => {
  const sid = sidOf(key)

  if (!sid) {
    return
  }

  // Cleared entries are removals too: without tombstones a concurrent stale
  // save could resurrect a deleted session's prompts and later send them.
  recordTombstones(readFreshQueuedPrompts(sid).map(e => e.id))

  mutateSession(sid, fresh => (fresh.length === 0 ? null : []))
}

/**
 * Move pending entries from a dead session key onto a live one, preserving FIFO
 * (existing target entries first, migrated entries appended). A backend bounce /
 * resume can mint a fresh runtime session id for the *same* conversation; the
 * entries enqueued under the old id would otherwise be stranded under a key
 * nothing reads anymore. No-op unless both keys resolve and differ.
 *
 * Async on purpose: it WAITS on the source key's drain claim. A drain in
 * flight under the old key must finish — and remove its entry under that key —
 * before the remainder moves, or the moved copy would be picked up under the
 * new key's (different) claim while the original submit is still in flight and
 * be submitted twice.
 *
 * Accepted limit: the write to the TARGET key races concurrent unlocked
 * mutations on it like any same-instant same-session write (milliseconds,
 * lost-update only) — holding the target's drain claim would not help, since
 * mutations never take claims.
 */
export const migrateQueuedPrompts = async (
  fromKey: string | null | undefined,
  toKey: string | null | undefined
): Promise<boolean> => {
  const from = sidOf(fromKey)
  const to = sidOf(toKey)

  if (!from || !to || from === to) {
    return false
  }

  const moved = await withSessionDrainClaim(
    from,
    async () =>
      mutateState(state => {
        const tombstones = readTombstones()
        const pending = filterTombstoned(state[from] ?? [], tombstones)

        if (pending.length === 0) {
          return null
        }

        delete state[from]
        state[to] = [...filterTombstoned(state[to] ?? [], tombstones), ...pending]

        return state
      }),
    { wait: true }
  )

  return moved ?? false
}

export interface DrainClaimOptions {
  /** Wait for the current holder to release instead of skipping. */
  wait?: boolean
  /** Bound the wait (ms); a timed-out wait resolves null. Unbounded when omitted. */
  timeoutMs?: number
}

/**
 * Run `task` while holding the exclusive cross-window drain claim for this
 * session, resolving null WITHOUT running it when the claim is unavailable
 * (held by another window and not waited for, or the wait timed out). A
 * renderer-local lock cannot stop two windows from picking the same queued
 * entry and double-submitting it — every idle window schedules auto-drain —
 * so the mutex must live outside the renderer. Web Locks are arbitrated by
 * the browser process across all windows of the origin, and the claim is
 * released automatically if the holding window closes or crashes, unlike a
 * localStorage claim flag which would leak and jam the queue forever.
 *
 * Environments without Web Locks (non-Chromium test DOMs) run `task` directly:
 * there is no second window to exclude there, and the caller's renderer-local
 * lock still serializes attempts within this one.
 */
export const withSessionDrainClaim = async <T>(
  key: string | null | undefined,
  task: () => Promise<T>,
  options: DrainClaimOptions = {}
): Promise<null | T> => {
  const sid = sidOf(key)

  if (!sid) {
    return null
  }

  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks

  if (!locks) {
    return task()
  }

  const name = `${QUEUE_STORAGE_KEY}.drain.${sid}`

  try {
    if (!options.wait) {
      return (await locks.request(name, { ifAvailable: true }, grant =>
        grant ? task() : Promise.resolve(null)
      )) as null | T
    }

    const signal = options.timeoutMs === undefined ? undefined : AbortSignal.timeout(options.timeoutMs)

    return (await locks.request(name, signal ? { signal } : {}, () => task())) as T
  } catch (error) {
    // An aborted wait (timeout) means the holder outlasted our patience —
    // report contention rather than surfacing a DOMException to drain logic.
    if (error instanceof DOMException && (error.name === 'AbortError' || error.name === 'TimeoutError')) {
      return null
    }

    throw error
  }
}

/**
 * Resolve once this session's drain claim is free (acquiring and instantly
 * releasing it). Losers of an `ifAvailable` attempt use this as their wake-up:
 * a winner whose submit is rejected — or whose window dies mid-submit — never
 * writes storage, so no storage event will re-trigger them, but the browser
 * always releases a dead window's locks.
 *
 * Resolves `true` only after a genuine wait-and-release. `false` means no
 * wait happened (no usable key, no Web Locks, or the request failed) — the
 * caller must NOT treat that as a wake-up, or an environment whose lock
 * requests reject would spin arm→fail→re-arm forever.
 */
export const whenSessionDrainClaimReleased = async (key: string | null | undefined): Promise<boolean> => {
  const sid = sidOf(key)

  if (!sid) {
    return false
  }

  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks

  if (!locks) {
    return false
  }

  try {
    await locks.request(`${QUEUE_STORAGE_KEY}.drain.${sid}`, {}, async () => undefined)

    return true
  } catch {
    return false
  }
}

const sentLockName = (id: string) => `${QUEUE_STORAGE_KEY}.sent.${id}`

/**
 * Hold a browser-arbitrated "already sent" claim on this entry id for the rest
 * of this window's lifetime. This is the storage-independent second layer
 * under the tombstones: when THIS window's removals cannot reach storage
 * (quota exhausted — the persistFailed mode), a healthy sibling window would
 * otherwise re-drain and re-submit every entry this window sends, because
 * storage still lists them. The held lock is visible to every window via
 * {@link isQueuedPromptSentElsewhere} and needs no storage at all. It releases
 * when this window closes, degrading to the accepted crash race — never worse
 * than the storage layer alone.
 */
export const markQueuedPromptSent = (id: string) => {
  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks

  if (!locks) {
    return
  }

  void locks
    .request(sentLockName(id), { ifAvailable: true }, grant => (grant ? new Promise<never>(() => {}) : undefined))
    .catch(() => undefined)
}

/** Whether any window (this one included) holds the sent claim for this id. */
export const isQueuedPromptSentElsewhere = async (id: string): Promise<boolean> => {
  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks

  if (!locks?.query) {
    return false
  }

  try {
    const { held } = await locks.query()
    const name = sentLockName(id)

    return (held ?? []).some(lock => lock?.name === name)
  } catch {
    return false
  }
}

/** Inputs to {@link shouldAutoDrain}. */
export interface AutoDrainInput {
  isBusy: boolean
  queueLength: number
}

/**
 * Decide whether the composer should auto-drain the next queued prompt.
 *
 * Edge-independent on purpose: the queue must advance whenever the session is
 * idle and has pending entries, NOT only on an observed busy true → false edge.
 * A backend bounce / websocket reconnect remounts the composer and resets the
 * busy ref to the current value, swallowing the settle edge — an edge-gated
 * drain would then strand the entry forever. Being edge-free can't
 * double-submit: the caller serializes sends within a window (its drain ref)
 * and across windows ({@link withSessionDrainClaim}).
 */
export const shouldAutoDrain = ({ isBusy, queueLength }: AutoDrainInput): boolean => !isBusy && queueLength > 0

/** Auto-drain attempts for one entry before we stop retrying and toast. The
 * entry stays queued for a manual send; a remount/reconnect resets the count. */
export const MAX_AUTO_DRAIN_ATTEMPTS = 4

/** Base delay between bounded auto-drain retries (attempt N waits N × this). */
export const AUTO_DRAIN_RETRY_BASE_MS = 1_000
