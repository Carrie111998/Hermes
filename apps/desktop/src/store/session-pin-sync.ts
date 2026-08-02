/**
 * Reconcile the sidebar's local pin order with each profile's durable backend
 * `sessions.pinned` flag.
 *
 * Persisted keys are profile-qualified (`profile\0lineage-root-id`). Older
 * Desktop builds stored only the unqualified id. Legacy keys migrate once the
 * owning row is known: a unique match keeps the user's local intent, while
 * cloned ids with conflicting server state adopt only the profile rows whose
 * durable flag is true. Ambiguous all-false clones stay legacy until a concrete
 * profile page makes ownership unambiguous; we never guess and pin a sibling.
 */

import { setSessionPinnedRemote } from '@/hermes'
import { $pinnedSessionIds, pinSession, unpinSession } from '@/store/layout'
import { $cronSessions, $messagingSessions, $sessions } from '@/store/session'

import { parseSessionPinKey, sessionMatchesPinKey, sessionPinKey } from './session-pin-key'

// Qualified keys confirmed by this app or adopted from a server row.
const mirrored = new Set<string>()
// Writes issued but not yet acknowledged. A session page already in flight may
// carry the old value, so the local write must win until its request settles.
const unconfirmed = new Map<string, boolean>()

const loadedRows = () => [...$sessions.get(), ...$cronSessions.get(), ...$messagingSessions.get()]

function writePin(key: string, pinned: boolean): Promise<void> {
  const parsed = parseSessionPinKey(key)

  if (!parsed) {
    return Promise.resolve()
  }

  unconfirmed.set(key, pinned)

  return setSessionPinnedRemote(parsed.id, pinned, parsed.profile).then(
    () => {
      if (unconfirmed.get(key) === pinned) {
        unconfirmed.delete(key)
      }
    },
    (err: unknown) => {
      if (unconfirmed.get(key) === pinned) {
        unconfirmed.delete(key)
      }

      throw err
    }
  )
}

/** Resolve old id-only entries without letting a cloned sibling claim them. */
function migrateLegacyKeys(keys: readonly string[]): string[] {
  const rows = loadedRows()
  const next: string[] = []

  const add = (key: string) => {
    if (!next.includes(key)) {
      next.push(key)
    }
  }

  for (const key of keys) {
    if (parseSessionPinKey(key)) {
      add(key)

      continue
    }

    const candidates = new Map<string, (typeof rows)[number]>()

    for (const row of rows) {
      if (sessionMatchesPinKey(row, key)) {
        candidates.set(sessionPinKey(row), row)
      }
    }

    if (candidates.size === 1) {
      add([...candidates.keys()][0])

      continue
    }

    if (candidates.size > 1) {
      const durablePins = [...candidates].filter(([, row]) => row.pinned === true)

      if (durablePins.length > 0) {
        for (const [qualified] of durablePins) {
          add(qualified)
        }

        continue
      }
    }

    // No row yet, or several all-false/flagless clones: preserve the old key
    // until a concrete profile page can resolve it without guessing.
    add(key)
  }

  return next
}

function pullRemotePins(): void {
  const local = new Set($pinnedSessionIds.get())

  for (const row of loadedRows()) {
    if (typeof row.pinned !== 'boolean') {
      continue
    }

    const key = sessionPinKey(row)
    const heldLocally = local.has(key)
    const awaited = unconfirmed.get(key)

    if (awaited !== undefined && awaited !== row.pinned) {
      continue
    }

    if (row.pinned && !heldLocally) {
      // Fence the synchronous pin listener before mutating the atom so an
      // adopted server pin is not echoed back as a redundant PATCH.
      mirrored.add(key)
      pinSession(key)
    } else if (!row.pinned && heldLocally) {
      // Likewise, forget the mirror first so the nested reconcile does not
      // PATCH false for a change the server already reported.
      mirrored.delete(key)
      unpinSession(key)
    }
  }
}

function reconcile(): void {
  if (!window.hermesDesktop) {
    return
  }

  const stored = $pinnedSessionIds.get()
  const migrated = migrateLegacyKeys(stored)

  if (migrated.length !== stored.length || migrated.some((key, index) => key !== stored[index])) {
    $pinnedSessionIds.set(migrated)

    return
  }

  const current = new Set(stored.filter(key => parseSessionPinKey(key)))

  for (const key of [...mirrored]) {
    if (!current.has(key)) {
      mirrored.delete(key)
      void writePin(key, false).catch(() => {})
    }
  }

  for (const key of current) {
    if (!mirrored.has(key)) {
      mirrored.add(key)
      void writePin(key, true).catch(() => {
        mirrored.delete(key)
      })
    }
  }

  pullRemotePins()
}

// Sync once, then follow every session slice that can own a sidebar pin.
export function watchSessionPins(): void {
  reconcile()
  $pinnedSessionIds.listen(reconcile)
  $sessions.listen(reconcile)
  $cronSessions.listen(reconcile)
  $messagingSessions.listen(reconcile)
}
