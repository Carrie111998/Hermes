import { atom } from 'nanostores'

import { SLASH_COMMAND_RE } from '@/lib/chat-runtime'

import type { ComposerAttachment } from './composer'
import {
  type ComposerStorageOwner,
  decodeComposerStorageScopeKey,
  normalizeComposerStorageOwner,
  resolveComposerStorageScopeKey
} from './composer-storage-scope'

export interface QueuedPromptEntry {
  id: string
  text: string
  /** What the queue panel and the sent bubble show, when it differs from the
   *  text the agent receives. A queued `/skill` invocation carries the whole
   *  expanded skill body as `text` — the UI shows the invocation instead. */
  displayText?: string
  attachments: ComposerAttachment[]
  queuedAt: number
}

/** Whether a queued entry can ride a mid-turn redirect: text-only, non-empty,
 *  not a slash command — the same gate `steerDraft` applies to the live draft
 *  (attachments can't ride a redirect; slash commands execute, not steer). */
export const isSteerableEntry = (entry: Pick<QueuedPromptEntry, 'attachments' | 'text'>): boolean => {
  const text = entry.text.trim()

  return Boolean(text) && entry.attachments.length === 0 && !SLASH_COMMAND_RE.test(text)
}

type QueueState = Record<string, QueuedPromptEntry[]>

const STORAGE_KEY = 'hermes.desktop.composerQueue.v1'
const PARK_STORAGE_KEY = 'hermes.desktop.composerQueueParks.v1'

const load = (): QueueState => {
  if (typeof window === 'undefined') {
    return {}
  }

  try {
    const raw = window.localStorage.getItem(STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : null

    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? (parsed as QueueState) : {}
  } catch {
    return {}
  }
}

const save = (state: QueueState) => {
  if (typeof window === 'undefined') {
    return
  }

  try {
    if (Object.keys(state).length === 0) {
      window.localStorage.removeItem(STORAGE_KEY)
    } else {
      window.localStorage.setItem(STORAGE_KEY, JSON.stringify(state))
    }
  } catch {
    // best-effort: storage may be unavailable, queue still works in-memory
  }
}

export const $queuedPromptsBySession = atom<QueueState>(load())

const loadParks = (): Record<string, true> => {
  if (typeof window === 'undefined') {
    return {}
  }

  try {
    const raw = window.localStorage.getItem(PARK_STORAGE_KEY)
    const parsed = raw ? JSON.parse(raw) : null

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return {}
    }

    return Object.fromEntries(
      Object.keys(parsed)
        .filter(key => parsed[key] === true)
        .map(key => [key, true])
    )
  } catch {
    return {}
  }
}

const saveParks = (state: Record<string, true>): void => {
  if (typeof window === 'undefined') {
    return
  }

  try {
    if (Object.keys(state).length === 0) {
      window.localStorage.removeItem(PARK_STORAGE_KEY)
    } else {
      window.localStorage.setItem(PARK_STORAGE_KEY, JSON.stringify(state))
    }
  } catch {
    // best-effort: storage may be unavailable, parking still works in-memory
  }
}

/**
 * Sessions whose queue the user explicitly halted (Stop button / Esc). A parked
 * queue is skipped by both auto-drain paths until the user acts on it again —
 * resume, send-now, a manual drain, queueing a fresh prompt, or emptying the
 * queue all unpark. Persisted and synchronized across renderer windows so a
 * Stop in one surface remains a drain boundary everywhere.
 */
export const $parkedQueueSessions = atom<Record<string, true>>(loadParks())

export function reloadPersistedComposerQueue(): void {
  $queuedPromptsBySession.set(load())
  $parkedQueueSessions.set(loadParks())
}

const setParked = (sid: string, parked: boolean) => {
  const current = loadParks()

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
  saveParks(next)
}

if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (event.key === STORAGE_KEY) {
      $queuedPromptsBySession.set(load())
    } else if (event.key === PARK_STORAGE_KEY) {
      $parkedQueueSessions.set(loadParks())
    }
  })
}

const writeSessionSnapshot = (current: QueueState, sid: string, queue: QueuedPromptEntry[]) => {
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

const mutateSession = <T>(
  sid: string,
  mutation: (queue: QueuedPromptEntry[]) => { next: QueuedPromptEntry[]; result: T }
): T => {
  const current = load()
  const { next, result } = mutation(current[sid] ?? [])

  writeSessionSnapshot(current, sid, next)

  return result
}

const sidOf = (key: string | null | undefined): null | string => {
  const trimmed = key?.trim()

  return trimmed ? resolveComposerStorageScopeKey(trimmed) : null
}

const queueFor = (sid: string) => $queuedPromptsBySession.get()[sid] ?? []

const nextId = () => `queued-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

const cloneAttachments = (attachments: ComposerAttachment[]) => attachments.map(a => ({ ...a }))

export const getQueuedPrompts = (key: string | null | undefined): QueuedPromptEntry[] => {
  const sid = sidOf(key)

  return sid ? queueFor(sid) : []
}

export const getLatestQueuedPrompts = (key: string | null | undefined): QueuedPromptEntry[] => {
  const sid = sidOf(key)

  return sid ? (load()[sid] ?? []).map(entry => ({ ...entry, attachments: cloneAttachments(entry.attachments) })) : []
}

export const enqueueQueuedPrompt = (
  key: string | null | undefined,
  payload: { text: string; attachments: ComposerAttachment[]; displayText?: string }
): null | QueuedPromptEntry => {
  const sid = sidOf(key)

  if (!sid) {
    return null
  }

  const entry: QueuedPromptEntry = {
    id: nextId(),
    text: payload.text,
    ...(payload.displayText ? { displayText: payload.displayText } : {}),
    attachments: cloneAttachments(payload.attachments),
    queuedAt: Date.now()
  }

  mutateSession(sid, queue => ({ next: [...queue, entry], result: undefined }))
  // Queueing a new prompt is fresh intent to keep the conversation moving —
  // a park from an earlier Stop must not hold this (or the entries ahead of
  // it) back.
  setParked(sid, false)

  return entry
}

export const dequeueQueuedPrompt = (key: string | null | undefined): null | QueuedPromptEntry => {
  const sid = sidOf(key)

  if (!sid) {
    return null
  }

  return mutateSession(sid, queue => {
    const [head, ...rest] = queue

    return { next: head ? rest : queue, result: head ?? null }
  })
}

export const removeQueuedPrompt = (key: string | null | undefined, id: string): boolean => {
  const sid = sidOf(key)

  if (!sid) {
    return false
  }

  return mutateSession(sid, queue => {
    const next = queue.filter(entry => entry.id !== id)

    return { next, result: next.length !== queue.length }
  })
}

export const promoteQueuedPrompt = (key: string | null | undefined, id: string): boolean => {
  const sid = sidOf(key)

  if (!sid) {
    return false
  }

  return mutateSession(sid, queue => {
    const index = queue.findIndex(entry => entry.id === id)

    if (index <= 0) {
      return { next: queue, result: false }
    }

    const entry = queue[index]!

    return { next: [entry, ...queue.slice(0, index), ...queue.slice(index + 1)], result: true }
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

  return mutateSession(sid, queue => {
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

      // The user rewrote the text, so any display projection it carried (a
      // `/skill` invocation standing in for the expanded body) no longer
      // describes it — what they typed is now what sends.
      const { displayText: _dropped, ...rest } = entry

      return { ...rest, text: update.text, attachments }
    })

    return { next, result: changed }
  })
}

export const updateQueuedPromptText = (key: string | null | undefined, id: string, text: string): boolean =>
  updateQueuedPrompt(key, id, { text })

export const clearQueuedPrompts = (key: string | null | undefined) => {
  const sid = sidOf(key)

  if (!sid) {
    return
  }

  mutateSession(sid, () => ({ next: [], result: undefined }))
}

export function clearQueuedPromptsForOwnerLineage(
  owner: ComposerStorageOwner,
  storedSessionIds: readonly (null | string | undefined)[]
): void {
  const normalizedOwner = normalizeComposerStorageOwner(owner)
  const ids = new Set(storedSessionIds.flatMap(id => (id?.trim() ? [id.trim()] : [])))

  for (const key of Object.keys($queuedPromptsBySession.get())) {
    const decoded = decodeComposerStorageScopeKey(key)

    if (
      decoded?.format === 'canonical' &&
      decoded.storedSessionId &&
      ids.has(decoded.storedSessionId) &&
      decoded.owner.connectionId === normalizedOwner.connectionId &&
      decoded.owner.profile === normalizedOwner.profile
    ) {
      clearQueuedPrompts(key)
    }
  }
}

/**
 * Move pending entries from a dead session key onto a live one, preserving FIFO
 * (existing target entries first, migrated entries appended). A backend bounce /
 * resume can mint a fresh runtime session id for the *same* conversation; the
 * entries enqueued under the old id would otherwise be stranded under a key
 * nothing reads anymore. No-op unless both keys resolve and differ.
 */
export function migrateQueuedPromptsExact(from: string, to: string): boolean {
  if (!from || !to || from === to) {
    return false
  }

  const current = load()
  const pending = current[from] ?? []

  if (pending.length === 0) {
    $queuedPromptsBySession.set(current)

    return false
  }

  const next = { ...current }
  delete next[from]
  next[to] = [...(current[to] ?? []), ...pending]

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

export const migrateQueuedPrompts = (fromKey: string | null | undefined, toKey: string | null | undefined): boolean => {
  const from = sidOf(fromKey)
  const to = sidOf(toKey)

  return from && to ? migrateQueuedPromptsExact(from, to) : false
}

/**
 * Park a session's queue after an explicit user halt (Stop / Esc): entries stay
 * visible in the panel but neither auto-drain path sends them. No-op for a
 * session with nothing queued — parking exists to hold back queued turns, and
 * a park with no queue would only linger as a stale gate.
 */
export const parkQueuedPrompts = (key: string | null | undefined): boolean => {
  const sid = sidOf(key)

  const current = sid ? load() : null

  if (!sid || !current || (current[sid] ?? []).length === 0) {
    return false
  }

  $queuedPromptsBySession.set(current)
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
