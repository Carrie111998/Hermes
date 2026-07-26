import { atom } from 'nanostores'

import { getSession, listAllProfileSessions } from '@/hermes'
import { normalizeProfileKey } from '@/store/profile'
import { sessionMatchesStoredId } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import type { ComposerAttachment } from './composer'

export interface QueuedPromptEntry {
  id: string
  text: string
  attachments: ComposerAttachment[]
  queuedAt: number
  /** Owning profile for a background (profile-targeted) queue. Absent for
   *  foreground/legacy entries, which drain through the active gateway. */
  profile?: string
}

// In-memory the queue stays a flat map so the many consumers (composer panel,
// background drain, session actions) keep reading it unchanged. The KEY is a
// bare storedSessionId for foreground/legacy entries, or a composite
// `${profile}::${storedSessionId}` for background entries (see profileQueueKey).
type QueueState = Record<string, QueuedPromptEntry[]>

const PROFILE_KEY_SEP = '::'

/** Composite in-memory/storage key for a background profile's queue bucket. */
export function profileQueueKey(profile: string, storedSessionId: string): string {
  return `${normalizeProfileKey(profile)}${PROFILE_KEY_SEP}${storedSessionId}`
}

// Persisted shape (v2): profile-nested. `byProfile[profile][storedSessionId]`
// holds profile-owned buckets; `legacy[storedSessionId]` holds profile-less v1
// buckets pending ownership resolution. v1 was a flat Record<storedSessionId,
// entries[]> under a separate key.
interface QueueStorageV2 {
  version: 2
  byProfile: Record<string, Record<string, QueuedPromptEntry[]>>
  legacy: Record<string, QueuedPromptEntry[]>
}

const STORAGE_KEY_V1 = 'hermes.desktop.composerQueue.v1'
const STORAGE_KEY_V2 = 'hermes.desktop.composerQueue.v2'

function validEntry(value: unknown): QueuedPromptEntry | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const raw = value as Record<string, unknown>

  if (typeof raw.id !== 'string' || typeof raw.text !== 'string' || typeof raw.queuedAt !== 'number') {
    return null
  }

  const entry: QueuedPromptEntry = {
    attachments: Array.isArray(raw.attachments) ? (raw.attachments as ComposerAttachment[]) : [],
    id: raw.id,
    queuedAt: raw.queuedAt,
    text: raw.text
  }

  if (typeof raw.profile === 'string' && raw.profile.trim()) {
    entry.profile = normalizeProfileKey(raw.profile)
  }

  return entry
}

function validBucket(value: unknown): QueuedPromptEntry[] {
  if (!Array.isArray(value)) {
    return []
  }

  return value.map(validEntry).filter((e): e is QueuedPromptEntry => e !== null)
}

// Flatten the nested v2 storage into the in-memory flat map (composite keys for
// profile buckets, bare storedSessionId for legacy).
function flattenV2(parsed: unknown): QueueState {
  const state: QueueState = {}

  if (!parsed || typeof parsed !== 'object') {
    return state
  }

  const raw = parsed as Partial<QueueStorageV2>

  if (raw.byProfile && typeof raw.byProfile === 'object') {
    for (const [profile, buckets] of Object.entries(raw.byProfile)) {
      if (!buckets || typeof buckets !== 'object') {
        continue
      }

      for (const [sid, entries] of Object.entries(buckets)) {
        const valid = validBucket(entries).map(e => ({ ...e, profile: normalizeProfileKey(profile) }))

        if (valid.length) {
          state[profileQueueKey(profile, sid)] = valid
        }
      }
    }
  }

  if (raw.legacy && typeof raw.legacy === 'object') {
    for (const [sid, entries] of Object.entries(raw.legacy)) {
      const valid = validBucket(entries)

      if (valid.length) {
        state[sid] = valid
      }
    }
  }

  return state
}

// Serialize the flat in-memory map back into the nested v2 shape. Profile-stamped
// entries nest under byProfile; everything else stays legacy.
function toStorageV2(state: QueueState): QueueStorageV2 {
  const byProfile: Record<string, Record<string, QueuedPromptEntry[]>> = {}
  const legacy: Record<string, QueuedPromptEntry[]> = {}

  for (const [key, entries] of Object.entries(state)) {
    if (!entries.length) {
      continue
    }

    const profile = entries[0]?.profile

    if (profile) {
      const sid = key.startsWith(`${profile}${PROFILE_KEY_SEP}`) ? key.slice(profile.length + PROFILE_KEY_SEP.length) : key
      ;(byProfile[profile] ??= {})[sid] = entries
    } else {
      legacy[key] = entries
    }
  }

  return { byProfile, legacy, version: 2 }
}

// Persist v2; reports failure (so the v1 migration only removes the v1 key AFTER
// a confirmed write). Best-effort otherwise: storage may be unavailable, the
// queue still works in-memory.
function saveV2(state: QueueState): boolean {
  if (typeof window === 'undefined') {
    return false
  }

  try {
    const serialized = toStorageV2(state)

    if (Object.keys(serialized.byProfile).length === 0 && Object.keys(serialized.legacy).length === 0) {
      window.localStorage.removeItem(STORAGE_KEY_V2)
    } else {
      window.localStorage.setItem(STORAGE_KEY_V2, JSON.stringify(serialized))
    }

    return true
  } catch {
    return false
  }
}

const load = (): QueueState => {
  if (typeof window === 'undefined') {
    return {}
  }

  try {
    const rawV2 = window.localStorage.getItem(STORAGE_KEY_V2)

    if (rawV2) {
      return flattenV2(JSON.parse(rawV2))
    }

    // v1 migration: a flat Record<storedSessionId, entries[]>. Entries keep no
    // profile, so they land in `legacy` and drain through the active gateway
    // (backwards compat) until ownership resolution re-attributes them.
    const rawV1 = window.localStorage.getItem(STORAGE_KEY_V1)

    if (rawV1) {
      const parsed = JSON.parse(rawV1)
      const state: QueueState = {}

      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        for (const [sid, entries] of Object.entries(parsed as Record<string, unknown>)) {
          const valid = validBucket(entries)

          if (valid.length) {
            state[sid] = valid
          }
        }
      }

      // Remove the v1 key ONLY after the v2 write succeeds.
      if (saveV2(state)) {
        window.localStorage.removeItem(STORAGE_KEY_V1)
      }

      return state
    }
  } catch {
    // fall through to empty
  }

  return {}
}

const save = (state: QueueState) => {
  saveV2(state)
}

export const $queuedPromptsBySession = atom<QueueState>(load())

/**
 * Sessions whose queue the user explicitly halted (Stop button / Esc). A parked
 * queue is skipped by both auto-drain paths until the user acts on it again —
 * resume, send-now, a manual drain, queueing a fresh prompt, or emptying the
 * queue all unpark. Deliberately in-memory only: a fresh app process starts
 * unparked, so restored-entry semantics stay a separate concern.
 */
export const $parkedQueueSessions = atom<Record<string, true>>({})

const setParked = (sid: string, parked: boolean) => {
  const current = $parkedQueueSessions.get()

  if (Boolean(current[sid]) === parked) {
    return
  }

  const next = { ...current }

  if (parked) {
    next[sid] = true
  } else {
    delete next[sid]
  }

  $parkedQueueSessions.set(next)
}

const writeSession = (sid: string, queue: QueuedPromptEntry[]) => {
  const current = $queuedPromptsBySession.get()
  const next = { ...current }

  if (queue.length === 0) {
    delete next[sid]
    // An empty queue has nothing to hold back — drop the park so it can't
    // linger as stale state and silently gate entries queued much later.
    setParked(sid, false)
  } else {
    next[sid] = queue
  }

  $queuedPromptsBySession.set(next)
  save(next)
}

const sidOf = (key: string | null | undefined): null | string => {
  const trimmed = key?.trim()

  return trimmed ? trimmed : null
}

/** A queue address: a bare storedSessionId string (foreground/legacy) or an
 *  explicit `{ profile, storedSessionId }` (background profile-targeted). */
export type QueueKey = string | { profile?: string; storedSessionId: string } | null | undefined

const keyOf = (key: QueueKey): null | string => {
  if (key && typeof key === 'object') {
    const sid = key.storedSessionId?.trim()

    if (!sid) {
      return null
    }

    return key.profile ? profileQueueKey(key.profile, sid) : sid
  }

  return sidOf(key)
}

const queueFor = (sid: string) => $queuedPromptsBySession.get()[sid] ?? []

const nextId = () => `queued-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

const cloneAttachments = (attachments: ComposerAttachment[]) => attachments.map(a => ({ ...a }))

export const getQueuedPrompts = (key: QueueKey): QueuedPromptEntry[] => {
  const sid = keyOf(key)

  return sid ? queueFor(sid) : []
}

export const enqueueQueuedPrompt = (
  key: QueueKey,
  payload: { text: string; attachments: ComposerAttachment[]; profile?: string }
): null | QueuedPromptEntry => {
  const sid = keyOf(key)

  if (!sid) {
    return null
  }

  const entry: QueuedPromptEntry = {
    id: nextId(),
    text: payload.text,
    attachments: cloneAttachments(payload.attachments),
    queuedAt: Date.now()
  }

  if (payload.profile) {
    entry.profile = normalizeProfileKey(payload.profile)
  }

  writeSession(sid, [...queueFor(sid), entry])
  // Queueing a new prompt is fresh intent to keep the conversation moving —
  // a park from an earlier Stop must not hold this (or the entries ahead of
  // it) back.
  setParked(sid, false)

  return entry
}

export const dequeueQueuedPrompt = (key: QueueKey): null | QueuedPromptEntry => {
  const sid = keyOf(key)

  if (!sid) {
    return null
  }

  const [head, ...rest] = queueFor(sid)

  if (!head) {
    return null
  }

  writeSession(sid, rest)

  return head
}

export const removeQueuedPrompt = (key: QueueKey, id: string): boolean => {
  const sid = keyOf(key)

  if (!sid) {
    return false
  }

  const queue = queueFor(sid)
  const next = queue.filter(e => e.id !== id)

  if (next.length === queue.length) {
    return false
  }

  writeSession(sid, next)

  return true
}

export const promoteQueuedPrompt = (key: string | null | undefined, id: string): boolean => {
  const sid = sidOf(key)

  if (!sid) {
    return false
  }

  const queue = queueFor(sid)
  const index = queue.findIndex(e => e.id === id)

  if (index <= 0) {
    return false
  }

  const entry = queue[index]!
  writeSession(sid, [entry, ...queue.slice(0, index), ...queue.slice(index + 1)])

  return true
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

  const queue = queueFor(sid)
  let changed = false

  const next = queue.map(entry => {
    if (entry.id !== id) {
      return entry
    }

    const attachments = update.attachments ? cloneAttachments(update.attachments) : entry.attachments

    if (entry.text === update.text && !update.attachments) {
      return entry
    }

    changed = true

    return { ...entry, text: update.text, attachments }
  })

  if (!changed) {
    return false
  }

  writeSession(sid, next)

  return true
}

export const updateQueuedPromptText = (key: string | null | undefined, id: string, text: string): boolean =>
  updateQueuedPrompt(key, id, { text })

export const clearQueuedPrompts = (key: string | null | undefined) => {
  const sid = sidOf(key)

  if (!sid || !(sid in $queuedPromptsBySession.get())) {
    return
  }

  writeSession(sid, [])
}

/**
 * Move pending entries from a dead session key onto a live one, preserving FIFO
 * (existing target entries first, migrated entries appended). A backend bounce /
 * resume can mint a fresh runtime session id for the *same* conversation; the
 * entries enqueued under the old id would otherwise be stranded under a key
 * nothing reads anymore. No-op unless both keys resolve and differ.
 */
export const migrateQueuedPrompts = (fromKey: string | null | undefined, toKey: string | null | undefined): boolean => {
  const from = sidOf(fromKey)
  const to = sidOf(toKey)

  if (!from || !to || from === to) {
    return false
  }

  const pending = queueFor(from)

  if (pending.length === 0) {
    return false
  }

  const next = { ...$queuedPromptsBySession.get() }
  delete next[from]
  next[to] = [...queueFor(to), ...pending]

  $queuedPromptsBySession.set(next)
  save(next)

  // The park is a property of the entries the user halted — it re-homes with
  // them. Without this, a backend bounce right after Stop would shed the park
  // and auto-send the exact prompts the user just held back.
  if ($parkedQueueSessions.get()[from]) {
    setParked(from, false)
    setParked(to, true)
  }

  return true
}

/**
 * Park a session's queue after an explicit user halt (Stop / Esc): entries stay
 * visible in the panel but neither auto-drain path sends them. No-op for a
 * session with nothing queued — parking exists to hold back queued turns, and
 * a park with no queue would only linger as a stale gate.
 */
export const parkQueuedPrompts = (key: string | null | undefined): boolean => {
  const sid = sidOf(key)

  if (!sid || queueFor(sid).length === 0) {
    return false
  }

  setParked(sid, true)

  return true
}

/** Lift a park (user resumed the queue). Safe to call for any session. */
export const unparkQueuedPrompts = (key: string | null | undefined): void => {
  const sid = sidOf(key)

  if (sid) {
    setParked(sid, false)
  }
}

export const isQueueParked = (key: string | null | undefined): boolean => {
  const sid = sidOf(key)

  return sid ? Boolean($parkedQueueSessions.get()[sid]) : false
}

/** Inputs to {@link shouldAutoDrain}. */
export interface AutoDrainInput {
  isBusy: boolean
  /** The user explicitly halted this session's queue (Stop / Esc). */
  parked?: boolean
  queueLength: number
}

/**
 * Decide whether the composer should auto-drain the next queued prompt.
 *
 * Edge-independent on purpose: the queue must advance whenever the session is
 * idle and has pending entries, NOT only on an observed busy true → false edge.
 * A backend bounce / websocket reconnect remounts the composer and resets the
 * busy ref to the current value, swallowing the settle edge — an edge-gated
 * drain would then strand the entry forever. The caller's drain lock
 * (`drainingQueueRef`) serializes sends so being edge-free can't double-submit.
 *
 * `parked` is the one deliberate exception: an explicit Stop/Esc is the user
 * saying HALT, and immediately firing the next queued prompt contradicts the
 * instruction they just gave. Parked entries stay in the panel until the user
 * resumes, sends, edits, or deletes them. Interrupts that exist to reach the
 * queue faster (send-now-while-busy) never park, so they keep draining through
 * this same gate.
 */
export const shouldAutoDrain = ({ isBusy, parked, queueLength }: AutoDrainInput): boolean =>
  !isBusy && !parked && queueLength > 0

/** Auto-drain attempts for one entry before we stop retrying and toast. The
 * entry stays queued for a manual send; a remount/reconnect resets the count. */
export const MAX_AUTO_DRAIN_ATTEMPTS = 4

// ── v1 → v2 ownership resolution ───────────────────────────────────────────

export type SessionOwnership =
  | { kind: 'ambiguous' }
  | { kind: 'indeterminate' }
  | { kind: 'none' }
  | { kind: 'one'; profile: string }

/**
 * Resolve the single owning profile of a stored session so a profile-less (v1 /
 * legacy) queue bucket can be re-attributed. Aggregate-FIRST: one
 * /api/profiles/sessions request — `archived: 'include'` is mandatory so an
 * archived queued session is not misclassified as absent — collapses matches
 * (id or _lineage_root_id, via sessionMatchesStoredId) by normalized owning
 * profile.
 *
 * Never guesses: an aggregate request FAILURE is `indeterminate` (not permission
 * to probe blindly or submit through the active gateway); zero/one/many matches
 * are `none`/`one`/`ambiguous`. Only when the bucket is absent from a SUCCESSFUL
 * aggregate does it fall back to profile-scoped getSession reads over the loaded
 * catalog — a confirmed not-found there is still `none`.
 */
export async function resolveUniqueSessionProfile(storedSessionId: string): Promise<SessionOwnership> {
  let catalog: SessionInfo[]

  try {
    ;({ sessions: catalog } = await listAllProfileSessions(500, 0, 'include', 'recent', 'all'))
  } catch {
    return { kind: 'indeterminate' }
  }

  const owners = new Set<string>()

  for (const row of catalog) {
    if (sessionMatchesStoredId(row, storedSessionId)) {
      owners.add(normalizeProfileKey(row.profile))
    }
  }

  if (owners.size === 1) {
    return { kind: 'one', profile: [...owners][0]! }
  }

  if (owners.size > 1) {
    return { kind: 'ambiguous' }
  }

  // Absent from a successful aggregate: confirm via profile-scoped reads over the
  // catalog's profiles. A match anywhere is `one`; with the aggregate already
  // reachable, per-profile not-founds confirm absence (`none`).
  const profiles = new Set(catalog.map(row => normalizeProfileKey(row.profile)))

  for (const profile of profiles) {
    try {
      const session = await getSession(storedSessionId, profile)

      if (session && sessionMatchesStoredId(session, storedSessionId)) {
        return { kind: 'one', profile: normalizeProfileKey(session.profile ?? profile) }
      }
    } catch {
      // Not on this profile; keep checking.
    }
  }

  return { kind: 'none' }
}

/**
 * Resolve a legacy (profile-less) bucket's owner and, on exactly one owner,
 * atomically move it into that profile's bucket and stamp every entry. Zero,
 * ambiguous, or indeterminate ownership leaves it under its bare key (legacy) and
 * returns the outcome for the caller to notify — never guessing, never routing
 * through the active gateway. No-op when the bucket is already empty.
 */
export async function resolveLegacyQueueBucket(storedSessionId: string): Promise<SessionOwnership> {
  const ownership = await resolveUniqueSessionProfile(storedSessionId)

  if (ownership.kind !== 'one') {
    return ownership
  }

  const current = $queuedPromptsBySession.get()
  const entries = current[storedSessionId]

  if (!entries?.length) {
    return ownership
  }

  const next = { ...current }
  delete next[storedSessionId]
  next[profileQueueKey(ownership.profile, storedSessionId)] = entries.map(entry => ({
    ...entry,
    profile: ownership.profile
  }))

  $queuedPromptsBySession.set(next)
  save(next)

  return ownership
}
