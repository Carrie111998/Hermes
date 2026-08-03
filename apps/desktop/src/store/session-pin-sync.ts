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

import type { HermesConnection } from '@/global'
import { setSessionPinnedRemote } from '@/hermes'
import { desktopConnectionScope } from '@/lib/connection-scope'
import { $pinnedSessionIds, pinSession, unpinSession } from '@/store/layout'
import { $connection, $sessions, sessionMatchesStoredId, sessionPinId } from '@/store/session'
import {
  activatePinnedSessionConnection,
  initializePinnedSessionScope,
  legacyPinnedSessionIds,
  pinnedSessionScopeInitialized
} from '@/store/session-pins'

interface PinSyncState {
  // Pin ids awaiting their row so we can resolve the owning profile before PATCH.
  pending: Set<string>
  // Pin ids we've successfully PATCHed pinned=true during this activation.
  mirrored: Set<string>
  // Object identity prevents an older write from clearing a newer guard.
  unconfirmed: Map<string, { pinned: boolean }>
}

let activeSyncScope: null | string = null
let activeSyncState: null | PinSyncState = null
let hydratingPinScope = false

function activatePinSyncConnection(connection: HermesConnection | null): void {
  const scope = desktopConnectionScope(connection)

  activeSyncScope = scope
  activeSyncState = scope
    ? { mirrored: new Set<string>(), pending: new Set<string>(), unconfirmed: new Map<string, { pinned: boolean }>() }
    : null
}

function hydratePinScope(connection: HermesConnection | null): void {
  activatePinSyncConnection(connection)
  hydratingPinScope = true

  try {
    activatePinnedSessionConnection(connection)
  } finally {
    hydratingPinScope = false
  }

  // A scoped localStorage value is a cache, not new user intent. Treat it as
  // already mirrored so the first modern session page can correct it without
  // echoing the stale cache back to the backend.
  if (activeSyncState) {
    activeSyncState.mirrored = new Set($pinnedSessionIds.get())
  }
}

function initializePinScopeFromSessions(): void {
  if (pinnedSessionScopeInitialized()) {
    return
  }

  const sessions = $sessions.get()

  if (sessions.length === 0) {
    return
  }

  // Modern backends own pin truth, so start their cache empty and let the pull
  // below populate it. For a backend predating `sessions.pinned`, preserve only
  // legacy local pins that resolve to a row on this connection; copying the old
  // global list wholesale is the cross-gateway leak fixed by #77318.
  const ids = sessions.some(row => typeof row.pinned === 'boolean')
    ? []
    : legacyPinnedSessionIds().filter(id => sessions.some(row => sessionMatchesStoredId(row, id)))

  initializePinnedSessionScope(ids)
}

function profileFor(pinId: string): null | string | undefined {
  return $sessions.get().find(row => sessionMatchesStoredId(row, pinId))?.profile
}

/** PATCH the flag, guarding reads against pages that predate the write. */
function writePin(state: PinSyncState, id: string, pinned: boolean, profile?: null | string): Promise<void> {
  const confirmation = { pinned }
  state.unconfirmed.set(id, confirmation)

  return setSessionPinnedRemote(id, pinned, profile).then(
    () => {
      if (state.unconfirmed.get(id) === confirmation) {
        state.unconfirmed.delete(id)
      }
    },
    (err: unknown) => {
      if (state.unconfirmed.get(id) === confirmation) {
        state.unconfirmed.delete(id)
      }

      throw err
    }
  )
}

/**
 * Adopt the server's pin state for every row in the current page.
 *
 * Runs after the push pass so local intent is already fenced (`pending` /
 * `unconfirmed`) by the time the page is read — a fresh local toggle whose
 * PATCH hasn't landed yet must win over the stale row, not be reverted by it
 * (#74570). Remote pins adopted here are marked mirrored before the local set
 * changes, so the re-entrant reconcile doesn't echo them back as a PATCH.
 */
function pullRemotePins(state: PinSyncState): void {
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
    const awaited = state.unconfirmed.get(pinId)?.pinned ?? state.unconfirmed.get(row.id)?.pinned

    if (awaited !== undefined && awaited !== row.pinned) {
      continue
    }

    // Local intent still waiting on its PATCH (row unresolved when the push
    // pass ran) is also newer than the page — never revert it.
    if (state.pending.has(pinId) || state.pending.has(row.id)) {
      continue
    }

    if (row.pinned && !heldLocally) {
      // Mark mirrored first: pinSession fires the pin listener synchronously,
      // and the nested reconcile must not see this as a new pin to PATCH.
      state.mirrored.add(pinId)
      pinSession(pinId)
    } else if (!row.pinned && heldLocally) {
      // Same discipline on the way down: forget the mirror before the nested
      // reconcile runs, or it re-PATCHes pinned=false the server already has.
      state.mirrored.delete(pinId)
      state.mirrored.delete(row.id)
      unpinSession(local.has(pinId) ? pinId : row.id)
    }
  }
}

function reconcile(): void {
  // Config/session REST is only reachable through the Electron bridge.
  if (!window.hermesDesktop) {
    return
  }

  const connection = $connection.get()

  if (!connection || desktopConnectionScope(connection) !== activeSyncScope) {
    return
  }

  const state = activeSyncState

  if (!state) {
    return
  }

  initializePinScopeFromSessions()

  // Push before pull. The pin listener fires synchronously on a local toggle,
  // so this reconcile runs before the PATCH for that toggle exists anywhere.
  // The push pass below records the intent (`pending`, then `unconfirmed` via
  // writePin) — only then may the pull read the page, where those fences stop
  // the still-stale row from silently reverting the user's action (#74570).
  const current = new Set($pinnedSessionIds.get())

  // Unpinned: anything we were tracking that's no longer in the set.
  for (const id of [...state.mirrored, ...state.pending]) {
    if (!current.has(id)) {
      state.mirrored.delete(id)
      state.pending.delete(id)
      void writePin(state, id, false, profileFor(id)).catch(() => {})
    }
  }

  // Newly pinned: hold until we can resolve the row (for its profile).
  for (const id of current) {
    if (!state.mirrored.has(id)) {
      state.pending.add(id)
    }
  }

  // Flush whatever we can resolve now; unresolved ids (row not loaded yet)
  // retry on the next $sessions change.
  for (const id of [...state.pending]) {
    const row = $sessions.get().find(entry => sessionMatchesStoredId(entry, id))

    if (!row) {
      continue
    }

    state.pending.delete(id)
    state.mirrored.add(id)
    void writePin(state, id, true, row.profile).catch(() => {
      // Let a later reconcile retry the mirror.
      state.mirrored.delete(id)
      state.pending.add(id)
    })
  }

  pullRemotePins(state)
}

// Sync once, then re-sync on connection, pin-set, and session-list changes.
// Call once per app; the returned cleanup keeps tests/HMR from stacking listeners.
export function watchSessionPins(): () => void {
  const offPins = $pinnedSessionIds.listen(() => {
    if (!hydratingPinScope) {
      reconcile()
    }
  })

  const offSessions = $sessions.listen(reconcile)

  const offConnection = $connection.listen(connection => {
    hydratePinScope(connection)
    reconcile()
  })

  hydratePinScope($connection.get())
  reconcile()

  return () => {
    offConnection()
    offSessions()
    offPins()
  }
}
