import { atom } from 'nanostores'

// Explicit sidebar multi-select mode. Off by default: every existing row
// gesture (shift-click pin, alt+shift archive, ctrl/cmd-click new tab,
// ctrl/cmd+shift new window) keeps working unchanged until the user opts in
// via a row's "Select chats" menu item. Only while this is true do plain and
// shift clicks turn into selection toggles/ranges instead.
export const $selectionModeActive = atom(false)

export const $selectedSessionIds = atom<string[]>([])

export interface BulkSessionActions {
  archive: (sessionIds: string[]) => void
  remove: (sessionIds: string[]) => void
}

// Published by the wiring layer (ContribWiring) so the action bar — several
// prop layers away from where sessions are archived/removed — can call the
// real bulk verbs without threading them down.
export const $bulkSessionActions = atom<BulkSessionActions | null>(null)

export function registerBulkSessionActions(actions: BulkSessionActions | null): void {
  $bulkSessionActions.set(actions)
}

// The DATA order each scope's rows render in, published by the section that
// owns them (see sessions-section.tsx). Range selection walks this order, not
// DOM order — virtualization only mounts a window, and a range spanning a
// scrolled-out row must still cover everything between its two ends. A plain
// module map, not a store: nothing needs to render off it.
const rowOrderByScope = new Map<string, string[]>()

// The last row touched by an explicit click (enter, toggle, or the target end
// of a previous range) — the anchor a following shift-click ranges from.
let anchorId: string | null = null

export function registerSessionRowOrder(scope: string, ids: string[]): void {
  rowOrderByScope.set(scope, ids)
}

export function forgetSessionRowOrder(scope: string): void {
  rowOrderByScope.delete(scope)
}

/** Row-menu entry point: start selection mode with exactly this row selected. */
export function enterSelectionMode(sessionId: string): void {
  anchorId = sessionId
  $selectedSessionIds.set([sessionId])
  $selectionModeActive.set(true)
}

/** Plain click while selection mode is active: add/remove just this row. */
export function toggleSessionSelection(scope: string, sessionId: string): void {
  void scope
  const current = $selectedSessionIds.get()

  anchorId = sessionId
  $selectedSessionIds.set(
    current.includes(sessionId) ? current.filter(id => id !== sessionId) : [...current, sessionId]
  )
}

/** Shift-click while selection mode is active: the contiguous range from the
 *  anchor to this row, within the row's own scope. Falls back to a plain
 *  toggle when there's no anchor or no registered order for the scope (e.g. a
 *  project lane that never published one), rather than doing nothing. */
export function selectSessionRange(scope: string, sessionId: string): void {
  const order = rowOrderByScope.get(scope)

  if (!order || !anchorId || !order.includes(anchorId) || !order.includes(sessionId)) {
    toggleSessionSelection(scope, sessionId)

    return
  }

  const anchorIndex = order.indexOf(anchorId)
  const targetIndex = order.indexOf(sessionId)
  const [start, end] = anchorIndex <= targetIndex ? [anchorIndex, targetIndex] : [targetIndex, anchorIndex]

  $selectedSessionIds.set(order.slice(start, end + 1))
}

/** Escape, click-out, or a bulk verb finishing: exit selection mode entirely. */
export function clearSessionSelection(): void {
  anchorId = null
  $selectedSessionIds.set([])
  $selectionModeActive.set(false)
}

/** Drop specific ids from the selection (rows that just left the list),
 *  exiting selection mode only if that emptied it — an archive/delete from a
 *  single row's own menu runs outside a bulk verb, so nothing else clears the
 *  "0 selected" action bar a lone removed row would otherwise leave behind. */
export function pruneSessionSelection(removedIds: readonly string[]): void {
  const removed = new Set(removedIds)
  const next = $selectedSessionIds.get().filter(id => !removed.has(id))

  $selectedSessionIds.set(next)

  if (next.length === 0) {
    $selectionModeActive.set(false)
  }
}
