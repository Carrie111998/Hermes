/**
 * Mirror the sidebar's localStorage pins into the backend "keep" flag, and
 * pull remote pins back so pinned sessions stay in sync across multiple
 * Desktop instances (Mac + Windows) sharing the same remote gateway.
 *
 * ## Why this exists
 *
 * Pins live in `$pinnedSessionIds` (localStorage) and drive the sidebar UI.
 * The `sessions.auto_archive` sweep, however, runs backend-side and is blind to
 * localStorage — so without this bridge it could hide a pinned chat.
 *
 * ## The cross-app sync problem (issue #72948)
 *
 * App A pins session X: `reconcile()` PATCHes ``pinned=true`` on the backend.
 * App B starts fresh, its localStorage has no pin for X, so X is unpinned in
 * App B's sidebar — confusing the user.
 *
 * Solution: at boot (and on reconnect) `pullRemotePins()` fetches the backend's
 * full pinned set and merges any remote pin that isn't already local into
 * `$pinnedSessionIds`.  Because every desktop instance both pushes (reconcile)
 * and pulls (pullRemotePins), the backend converges as the single source of
 * truth for the keep-flag.
 */

import { getPinnedSessionIds, setSessionPinnedRemote } from '@/hermes'
import { $pinnedSessionIds } from '@/store/layout'
import { $sessions, sessionMatchesStoredId } from '@/store/session'

// pin ids we've successfully PATCHed pinned=true this session.
const mirrored = new Set<string>()
// pin ids awaiting their row so we can resolve the owning profile before PATCH.
const pending = new Set<string>()

function profileFor(pinId: string): null | string | undefined {
  return $sessions.get().find(row => sessionMatchesStoredId(row, pinId))?.profile
}

function reconcile(): void {
  // Config/session REST is only reachable through the Electron bridge.
  if (!window.hermesDesktop) {
    return
  }

  const current = new Set($pinnedSessionIds.get())

  // Unpinned: anything we were tracking that's no longer in the set.
  for (const id of [...mirrored, ...pending]) {
    if (!current.has(id)) {
      mirrored.delete(id)
      pending.delete(id)
      void setSessionPinnedRemote(id, false, profileFor(id)).catch(() => {})
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
    void setSessionPinnedRemote(id, true, row.profile).catch(() => {
      // Let a later reconcile retry the mirror.
      mirrored.delete(id)
      pending.add(id)
    })
  }
}

/** Fetch the backend's pinned-session set and merge any new remote pins into
 *  the local store.  Throttled so rapid reconnect cycles don't hammer the
 *  backend. */
async function pullRemotePins(): Promise<void> {
  if (!window.hermesDesktop) {
    return
  }
  try {
    const resp = await getPinnedSessionIds()

    if (!resp?.pinned?.length) {
      return
    }

    const local = new Set($pinnedSessionIds.get())
    let changed = false

    for (const { id } of resp.pinned) {
      if (!local.has(id)) {
        const prev = $pinnedSessionIds.get()
        $pinnedSessionIds.set([...prev, id])
        local.add(id)
        changed = true
      }
    }

    if (changed) {
      reconcile()
    }
  } catch {
    // Non-fatal: the local pin set stays as-is; next cycle retries.
  }
}

// ── Lifecycle ──────────────────────────────────────────────────────────
// Timer (debounced & cleared on reconnect) so a reconnecting app re-pulls
// once the gateway is reachable, and a slow network doesn't stack calls.
let pullTimer: ReturnType<typeof setTimeout> | null = null
const PULL_DELAY_MS = 2_000

function schedulePull(): void {
  if (pullTimer) {
    clearTimeout(pullTimer)
  }
  pullTimer = setTimeout(() => {
    pullTimer = null
    void pullRemotePins()
  }, PULL_DELAY_MS)
}

// ── Public API ─────────────────────────────────────────────────────────

/** Start the pin-sync lifecycle. Call once per app. */
export function watchSessionPins(): void {
  // Sync once, then re-sync on pin-set and session-list changes.
  reconcile()

  // Pull remote pins after a short settlement delay so the gateway connection
  // is established and the session list has loaded.
  schedulePull()

  $pinnedSessionIds.listen(reconcile)
  $sessions.listen(reconcile)
  $sessions.listen(schedulePull)
}

/** Re-pull remote pins after a gateway reconnect. Called by the gateway
 *  connection controller when the WebSocket re-establishes. */
export function refreshRemotePins(): void {
  pullTimer = null // flush any stale scheduled pull
  void pullRemotePins()
}
