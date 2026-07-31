/**
 * Reconcile the sidebar's pins with the backend "keep" flag, both directions.
 *
 * Pins drive the sidebar UI out of `$pinnedSessionIds` (localStorage), but the
 * durable record is `sessions.pinned` in each profile's state.db. Two things
 * depend on the backend copy: the `sessions.auto_archive` sweep runs
 * server-side and would otherwise hide a pinned chat, and a second Desktop app
 * pointed at the same gateway has its own, separate localStorage.
 *
 * Push: PATCH `pinned` whenever the local set changes. At boot the restored
 * set is first reconciled against the server — a present server row is
 * authoritative, so pins another surface removed while this app was stopped
 * stay removed instead of being re-asserted. Only pins the user pins in THIS
 * process (tracked in `localIntent`) survive a stale server page long enough
 * for their PATCH to land.
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
// pin ids the user pinned in THIS process (post-boot). Populated from
// $pinnedSessionIds changes that did not originate inside reconcile, so
// localStorage-restored pins never enter it: a stored pin whose present
// server row says false is still server-authoritative at boot (cross-app
// contract), while a pin clicked moments ago survives the stale page until
// its PATCH lands.
const localIntent = new Set<string>()
// Writes we've issued but not yet had acked, id -> value written. A list page
// already in flight when we PATCH still carries the old value, so it must not
// be read as the server disagreeing with us. Cleared when the write settles —
// the request's own lifetime is the guard, so nothing can leave one open.
const unconfirmed = new Map<string, boolean>()
// Last pinned set the listener saw — the diff separates user clicks from
// boot-restored state. Re-anchored by __resetPinSyncForTests (test-only).
let prevPinned = new Set<string>()
// True while reconcile mutates the store itself (adopts/drops); those
// changes must not be labelled user intent.
let inReconcile = false

function profileFor(pinId: string): null | string | undefined {
  return $sessions.get().find(row => sessionMatchesStoredId(row, pinId))?.profile
}

/** PATCH the flag, guarding reads against pages that predate the write. */
function writePin(id: string, pinned: boolean, profile?: null | string): Promise<void> {
  unconfirmed.set(id, pinned)

  return setSessionPinnedRemote(id, pinned, profile).then(
    () => {
      unconfirmed.delete(id)
    },
    (err: unknown) => {
      unconfirmed.delete(id)
      throw err
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
    const awaited = unconfirmed.has(pinId) ? unconfirmed.get(pinId) : unconfirmed.get(row.id)

    if (awaited !== undefined && awaited !== row.pinned) {
      continue
    }

    if (row.pinned && !heldLocally) {
      // Server-true is only authoritative for a pin we didn't mirror
      // ourselves. For a mirrored pin, local absence means the user just
      // unpinned — the page predates the unpin PATCH our push pass is about
      // to send. Adopting the stale true would resurrect the pin in the
      // very reconcile that removed it, and the unpin write never goes out.
      if (mirrored.has(pinId) || mirrored.has(row.id)) {
        continue
      }

      pinSession(pinId)
      // Already true server-side; record it so the push pass doesn't re-PATCH.
      mirrored.add(pinId)
    } else if (!row.pinned && heldLocally) {
      // Server-false is only authoritative for a pin the server has heard
      // about. The one exception: a pin the user pinned in THIS process
      // (localIntent) whose PATCH hasn't landed yet — the false is just the
      // page predating the write, so it survives to the push pass. A pin
      // restored from localStorage at boot is neither mirrored nor
      // localIntent, so a present server row saying false drops it — unpins
      // made by another surface while this app was stopped stay dead.
      if (
        !mirrored.has(pinId) &&
        !mirrored.has(row.id) &&
        (localIntent.has(pinId) || localIntent.has(row.id))
      ) {
        continue
      }

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

  inReconcile = true

  try {
    pullRemotePins()

    const current = new Set($pinnedSessionIds.get())

    // Unpinned: anything we were tracking that's no longer in the set.
    for (const id of [...mirrored, ...pending]) {
      if (!current.has(id)) {
        mirrored.delete(id)
        pending.delete(id)
        void writePin(id, false, profileFor(id)).catch(() => {})
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
  } finally {
    inReconcile = false
  }
}

// Sync once, then re-sync on pin-set and session-list changes. Call once per app.
export function watchSessionPins(): void {
  prevPinned = new Set($pinnedSessionIds.get())
  reconcile()
  $pinnedSessionIds.listen(next => {
    const nextSet = new Set(next)

    // Clicks outside reconcile are the user's own intent; adoptions inside
    // reconcile are not. The boot-restored set is the listener's baseline,
    // so it never counts as intent.
    if (!inReconcile) {
      for (const id of nextSet) {
        if (!prevPinned.has(id)) {
          localIntent.add(id)
        }
      }
    }

    prevPinned = nextSet
    reconcile()
  })
  $sessions.listen(reconcile)
}

/** Test-only: simulate an app restart — wipe same-process mirror/intent state
 *  while treating the current pinned set as boot-restored (re-anchors the
 *  listener baseline so nothing already in the store is labelled intent). */
export function __resetPinSyncForTests(): void {
  mirrored.clear()
  pending.clear()
  unconfirmed.clear()
  localIntent.clear()
  prevPinned = new Set($pinnedSessionIds.get())
}
