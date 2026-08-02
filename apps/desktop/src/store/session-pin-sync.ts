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
 * longer says anything about its pin state. Adopt pins this app hasn't seen.
 * Drop local pins the server reports unpinned **only after** we previously
 * mirrored them (confirmed server true) — never-synced localStorage pins push
 * instead of being wiped on boot. Only rows present in the payload are
 * consulted; `pinned === undefined` leaves the local set untouched.
 */

import { setSessionPinnedRemote } from '@/hermes'
import { arraysEqual } from '@/lib/storage'
import { $pinnedSessionIds, pinSession, unpinSession } from '@/store/layout'
import { $sessions, sessionMatchesStoredId, sessionPinId } from '@/store/session'

// pin ids we've successfully PATCHed pinned=true this session.
const mirrored = new Set<string>()
// pin ids awaiting their row so we can resolve the owning profile before PATCH.
const pending = new Set<string>()
// Writes we've issued but not yet had acked, id -> value written. A list page
// already in flight when we PATCH still carries the old value, so it must not
// be read as the server disagreeing with us. Cleared when the write settles —
// the request's own lifetime is the guard, so nothing can leave one open.
const unconfirmed = new Map<string, boolean>()
// normalizeLocalPinIds / pinSession write $pinnedSessionIds and would re-enter
// reconcile via the atom listener; collapse those into one pass.
let reconciling = false

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
 * Collapse tip/root aliases in localStorage onto durable `sessionPinId` keys.
 * Idempotent once the list is clean — prevents leftover tips from surviving an
 * unpin of the root and then re-pushing `pinned=true`.
 */
function normalizeLocalPinIds(): void {
  const sessions = $sessions.get()

  if (sessions.length === 0) {
    return
  }

  const prev = $pinnedSessionIds.get()
  const next: string[] = []
  const seen = new Set<string>()

  for (const id of prev) {
    const row = sessions.find(entry => sessionMatchesStoredId(entry, id))
    const durable = row ? sessionPinId(row) : id

    if (seen.has(durable)) {
      continue
    }

    seen.add(durable)
    next.push(durable)
  }

  if (!arraysEqual(prev, next)) {
    $pinnedSessionIds.set(next)
  }
}

/**
 * Adopt the server's pin state for every row in the current page.
 *
 * Runs AFTER local unpin pushes so a user clear is already in `unconfirmed` as
 * false — otherwise pull would re-adopt `row.pinned === true` in the same tick
 * and the pin would bounce back ("cannot unpin"). Remote pins the local set
 * has not seen are still adopted; mirrored pins avoid a redundant PATCH.
 *
 * Drop rule (2026-08-01): only drop a local pin when we previously confirmed
 * the server had it (`mirrored`). localStorage-only pins that never completed
 * a PATCH must **push**, not pull-unpin — otherwise boot / upgrade wipes the
 * whole Pinned section while state.db still has pinned=0 (LevelDB restore case).
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
    const heldLocally =
      local.has(pinId) || local.has(row.id) || [...local].some(id => sessionMatchesStoredId(row, id))

    // A write of ours the page hasn't caught up to yet is newer than the page.
    // unconfirmed is set synchronously in writePin before the network settles;
    // local unpin pushes run before this pull so the same tick cannot re-adopt.
    const awaited = unconfirmed.has(pinId) ? unconfirmed.get(pinId) : unconfirmed.get(row.id)

    if (awaited !== undefined && awaited !== row.pinned) {
      continue
    }

    if (row.pinned && !heldLocally) {
      pinSession(pinId)
      // Already true server-side; record it so the push pass doesn't re-PATCH.
      mirrored.add(pinId)
    } else if (!row.pinned && heldLocally) {
      const wasMirrored =
        mirrored.has(pinId) || mirrored.has(row.id) || [...mirrored].some(id => sessionMatchesStoredId(row, id))

      if (!wasMirrored) {
        // Boot migration / never-synced local pin: keep local; push pass PATCHes true.
        continue
      }

      // Confirmed server true earlier; now false → another client (or explicit) unpin.
      // Lineage-aware unpin clears tip + root aliases in one shot.
      unpinSession(pinId)
      mirrored.delete(pinId)
      mirrored.delete(row.id)

      for (const id of [...mirrored]) {
        if (sessionMatchesStoredId(row, id)) {
          mirrored.delete(id)
        }
      }
    } else if (row.pinned && heldLocally) {
      // Keep mirror bookkeeping on the durable id even when localStorage still
      // holds a tip alias (normalizeLocalPinIds will collapse it).
      mirrored.add(pinId)
    }
  }
}

function reconcile(): void {
  // Config/session REST is only reachable through the Electron bridge.
  if (!window.hermesDesktop) {
    return
  }

  if (reconciling) {
    return
  }

  reconciling = true

  try {
    // Collapse tip/root duplicates before pull/push so an unpin of either id
    // cannot leave a sibling alias that re-asserts pinned=true.
    normalizeLocalPinIds()

    const sessions = $sessions.get()
    // Snapshot local set BEFORE pull. User unpins must win this tick: push
    // false + unconfirmed first, else pull re-adopts server pinned=true and the
    // pin visually "cannot be cleared".
    const currentBeforePull = new Set($pinnedSessionIds.get())

    // Unpinned: anything we were tracking that's no longer in the set (or only
    // remains as a collapsed alias of a still-pinned durable id).
    for (const id of [...mirrored, ...pending]) {
      const stillHeld =
        currentBeforePull.has(id) ||
        [...currentBeforePull].some(held => {
          const row = sessions.find(entry => sessionMatchesStoredId(entry, held) || sessionMatchesStoredId(entry, id))

          return row ? sessionMatchesStoredId(row, held) && sessionMatchesStoredId(row, id) : held === id
        })

      if (!stillHeld) {
        mirrored.delete(id)
        pending.delete(id)
        // writePin sets unconfirmed=false before the network settles so the
        // pull pass below will not re-adopt a stale row.pinned===true page.
        void writePin(id, false, profileFor(id)).catch(() => {})
      }
    }

    pullRemotePins()

    const current = new Set($pinnedSessionIds.get())

    // Newly pinned: hold until we can resolve the row (for its profile).
    for (const id of current) {
      if (
        !mirrored.has(id) &&
        ![...mirrored].some(m => {
          const row = sessions.find(entry => sessionMatchesStoredId(entry, m) || sessionMatchesStoredId(entry, id))

          return row ? sessionMatchesStoredId(row, m) && sessionMatchesStoredId(row, id) : m === id
        })
      ) {
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

      const durable = sessionPinId(row)

      pending.delete(id)
      pending.delete(durable)
      mirrored.add(durable)
      // Always PATCH the durable id so tip/root never diverge server-side.
      void writePin(durable, true, row.profile).catch(() => {
        // Let a later reconcile retry the mirror.
        mirrored.delete(durable)
        pending.add(durable)
      })
    }
  } finally {
    reconciling = false
  }
}

// Sync once, then re-sync on pin-set and session-list changes. Call once per app.
export function watchSessionPins(): void {
  reconcile()
  $pinnedSessionIds.listen(reconcile)
  $sessions.listen(reconcile)
}
