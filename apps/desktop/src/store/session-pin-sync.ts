/**
 * Reconcile the sidebar's pins with the backend "keep" flag, both directions.
 *
 * Pins drive the sidebar UI out of `$pinnedSessionIds` (localStorage), but the
 * durable record is `sessions.pinned` in each profile's state.db. Two things
 * depend on the backend copy: the `sessions.auto_archive` sweep runs
 * server-side and would otherwise hide a pinned chat, and a second Desktop app
 * pointed at the same gateway has its own, separate localStorage.
 *
 * Push: PATCH `pinned` whenever the local set changes, and re-assert the whole
 * set at boot — which transparently migrates pre-existing pins with no user
 * action.
 *
 * Pull: session rows now carry `pinned`, and the list endpoints back-fill
 * pinned conversations past their LIMIT, so a row's absence from a page no
 * longer says anything about its pin state. That makes the server row
 * authoritative: adopt pins this app hasn't seen, and drop local pins the
 * server says are gone. Only rows actually present in the payload are
 * consulted, so a backend predating the flag (`pinned === undefined`) leaves
 * the local set untouched.
 */

import { setSessionPinnedRemote } from '@/hermes'
import { $pinnedSessionIds, pinSession, unpinSession } from '@/store/layout'
import { $sessions, sessionMatchesStoredId, sessionPinId } from '@/store/session'

// pin ids we've successfully PATCHed pinned=true this session.
const mirrored = new Set<string>()
// pin ids awaiting their row so we can resolve the owning profile before PATCH.
const pending = new Set<string>()
interface PendingPinWrite {
  generation: number
  pinned: boolean
}

// Latest in-flight write per pin id. Generation identity prevents an older
// request from clearing or retrying a newer click's guard.
const unconfirmed = new Map<string, PendingPinWrite>()
let nextWriteGeneration = 0

function profileFor(pinId: string): null | string | undefined {
  return $sessions.get().find(row => sessionMatchesStoredId(row, pinId))?.profile
}

/** PATCH the flag, guarding reads against pages that predate the write. */
function writePin(id: string, pinned: boolean, profile?: null | string): Promise<void> {
  const generation = ++nextWriteGeneration
  unconfirmed.set(id, { generation, pinned })

  return setSessionPinnedRemote(id, pinned, profile).then(
    () => {
      const current = unconfirmed.get(id)

      if (current?.generation === generation) {
        unconfirmed.delete(id)
      }
    },
    (err: unknown) => {
      const current = unconfirmed.get(id)

      if (current?.generation === generation) {
        unconfirmed.delete(id)
        throw err
      }
    }
  )
}

/**
 * Adopt the server's pin state for every row in the current page.
 *
 * Runs before the push pass so a remote pin is already in the local set by the
 * time we reconcile — it gets marked as mirrored rather than echoed straight
 * back as a redundant PATCH.
 */
function pullRemotePins(): void {
  const local = new Set($pinnedSessionIds.get())

  for (const row of $sessions.get()) {
    // A backend without the flag has no opinion; never act on `undefined`.
    if (typeof row.pinned !== 'boolean') {
      continue
    }

    // Pins are keyed on the durable lineage root so they survive compression
    // tip rotation; the row may surface under either identity.
    const pinId = sessionPinId(row)
    const heldLocally = local.has(pinId) || local.has(row.id)

    // A write of ours the page hasn't caught up to yet is newer than the page.
    const awaitedId = unconfirmed.has(pinId) ? pinId : row.id
    const awaited = unconfirmed.get(awaitedId)

    if (awaited && awaited.pinned !== row.pinned) {
      continue
    }

    if (row.pinned && !heldLocally) {
      pinSession(pinId)
      // Already true server-side; record it so the push pass doesn't re-PATCH.
      mirrored.add(pinId)
    } else if (!row.pinned && heldLocally) {
      unpinSession(local.has(pinId) ? pinId : row.id)
      mirrored.delete(pinId)
      mirrored.delete(row.id)
    }
  }
}

function reconcile(): void {
  // Config/session REST is only reachable through the Electron bridge.
  if (!window.hermesDesktop) {
    return
  }

  const current = new Set($pinnedSessionIds.get())

  // Push local intent before consulting the current session page. The page may
  // still carry the value from before the click; writePin records that newer
  // intent in `unconfirmed` so pullRemotePins cannot undo it.
  //
  // Unpinned: anything we were tracking that's no longer in the set.
  for (const id of [...mirrored, ...pending]) {
    if (!current.has(id)) {
      mirrored.delete(id)
      pending.delete(id)
      void writePin(id, false, profileFor(id)).catch(() => {
        // Preserve the unpin intent for the next reconcile. writePin suppresses
        // failures from superseded generations, so only the latest failed
        // unpin can restore this retry marker.
        mirrored.add(id)
      })
    }
  }

  // Newly pinned: hold until we can resolve the row (for its profile).
  for (const id of current) {
    if (!mirrored.has(id)) {
      pending.add(id)
    }
  }

  // Flush whatever we can resolve now; unresolved ids (row not loaded yet)
  // retry on the next $sessions change.
  for (const id of [...pending]) {
    const row = $sessions.get().find(entry => sessionMatchesStoredId(entry, id))

    if (!row) {
      continue
    }

    pending.delete(id)
    mirrored.add(id)
    void writePin(id, true, row.profile).catch(() => {
      // Let a later reconcile retry the mirror.
      mirrored.delete(id)
      pending.add(id)
    })
  }

  // With local writes now guarded, adopt authoritative changes made by other
  // clients without allowing an older page to clobber this app's latest click.
  pullRemotePins()
}

// Sync once, then re-sync on pin-set and session-list changes. Call once per app.
export function watchSessionPins(): void {
  reconcile()
  $pinnedSessionIds.listen(reconcile)
  $sessions.listen(reconcile)
}
