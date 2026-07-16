import { atom } from 'nanostores'

import type { ComposerAttachment } from './composer'

export interface QueuedPromptEntry {
  id: string
  text: string
  attachments: ComposerAttachment[]
  queuedAt: number
}

type QueueState = Record<string, QueuedPromptEntry[]>

const STORAGE_KEY = 'hermes.desktop.composerQueue.v1'

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

// Cross-window sync: every desktop window boots this store from the same
// localStorage key, but without this listener each window keeps a private,
// diverging snapshot forever. A window that enqueues/drains would then clobber
// the others' entries on its next save (#46732: prompts resurrecting, vanishing,
// or draining from a window the user never typed in). `storage` fires only in
// the windows that did NOT write, so there is no self-echo to guard against.
if (typeof window !== 'undefined') {
  window.addEventListener('storage', event => {
    if (event.key === STORAGE_KEY || event.key === null) {
      $queuedPromptsBySession.set(load())
    }
  })
}

// Operation-based cross-window mutation: reload the persisted map and apply
// `op` to the freshest version of THIS session's queue — never to an array the
// caller derived from the in-memory atom. Reloading the map alone only
// protected other sessions' keys: two windows mutating the same session would
// still replace its array with competing stale versions (window B's append
// built on a queue that predates window A's), silently dropping entries.
// `op` receives the fresh queue and returns the next one, or null for "nothing
// to do" (no write, no atom churn). Returns whether a write happened.
//
// The load→op→save sequence is synchronous, so the only remaining race is two
// renderer processes interleaving inside it at the exact same instant —
// microseconds, versus the seconds-wide stale-snapshot window this closes.
// Fully eliminating it would need cross-window mutual exclusion (Web Locks)
// around every mutation, which forces all these APIs async; the drain path,
// where a race double-submits a prompt into the model, does exactly that via
// withSessionDrainClaim below.
const mutateSession = (sid: string, op: (fresh: QueuedPromptEntry[]) => null | QueuedPromptEntry[]): boolean => {
  const next = { ...load() }
  const queue = op(next[sid] ?? [])

  if (queue === null) {
    return false
  }

  if (queue.length === 0) {
    delete next[sid]
  } else {
    next[sid] = queue
  }

  $queuedPromptsBySession.set(next)
  save(next)

  return true
}

const sidOf = (key: string | null | undefined): null | string => {
  const trimmed = key?.trim()

  return trimmed ? trimmed : null
}

const queueFor = (sid: string) => $queuedPromptsBySession.get()[sid] ?? []

const nextId = () => `queued-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`

const cloneAttachments = (attachments: ComposerAttachment[]) => attachments.map(a => ({ ...a }))

export const getQueuedPrompts = (key: string | null | undefined): QueuedPromptEntry[] => {
  const sid = sidOf(key)

  return sid ? queueFor(sid) : []
}

/**
 * Read a session's queue straight from persisted storage, bypassing the atom.
 * The `storage` event that syncs the atom is asynchronous, so around a drain
 * the atom can still list an entry another window already removed. Drain paths
 * must pick from this — inside {@link withSessionDrainClaim} — so they never
 * act on that stale echo.
 */
export const readPersistedQueuedPrompts = (key: string | null | undefined): QueuedPromptEntry[] => {
  const sid = sidOf(key)

  return sid ? (load()[sid] ?? []) : []
}

/**
 * Run `task` while holding the exclusive cross-window drain claim for this
 * session, resolving null WITHOUT running it when another window already holds
 * the claim. A renderer-local lock cannot stop two windows from picking the
 * same queued entry and double-submitting it — every idle window schedules
 * auto-drain — so the mutex must live outside the renderer. Web Locks are
 * arbitrated by the browser process across all windows of the origin, and the
 * claim is released automatically if the holding window closes or crashes,
 * unlike a localStorage claim flag which would leak and jam the queue forever.
 * `ifAvailable` keeps losers non-blocking: they skip this attempt and try
 * again when the winner's removal lands as a storage event.
 *
 * Environments without Web Locks (non-Chromium test DOMs) run `task` directly:
 * there is no second window to exclude there, and the caller's renderer-local
 * lock still serializes attempts within this one.
 */
export const withSessionDrainClaim = async <T>(
  key: string | null | undefined,
  task: () => Promise<T>
): Promise<null | T> => {
  const sid = sidOf(key)

  if (!sid) {
    return null
  }

  const locks = typeof navigator === 'undefined' ? undefined : navigator.locks

  if (!locks) {
    return task()
  }

  return (await locks.request(`${STORAGE_KEY}.drain.${sid}`, { ifAvailable: true }, grant =>
    grant ? task() : Promise.resolve(null)
  )) as null | T
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

  let head: null | QueuedPromptEntry = null

  mutateSession(sid, fresh => {
    if (fresh.length === 0) {
      return null
    }

    head = fresh[0]!

    return fresh.slice(1)
  })

  return head
}

export const removeQueuedPrompt = (key: string | null | undefined, id: string): boolean => {
  const sid = sidOf(key)

  if (!sid) {
    return false
  }

  return mutateSession(sid, fresh => {
    const next = fresh.filter(e => e.id !== id)

    return next.length === fresh.length ? null : next
  })
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

      if (entry.text === update.text && !update.attachments) {
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

  mutateSession(sid, fresh => (fresh.length === 0 ? null : []))
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

  // Same operation-based rule as mutateSession, spanning two keys: both the
  // migrated entries and the target queue come from the fresh persisted map,
  // not the atom, so entries another window just enqueued are not dropped.
  const next = { ...load() }
  const pending = next[from] ?? []

  if (pending.length === 0) {
    return false
  }

  delete next[from]
  next[to] = [...(next[to] ?? []), ...pending]

  $queuedPromptsBySession.set(next)
  save(next)

  return true
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
