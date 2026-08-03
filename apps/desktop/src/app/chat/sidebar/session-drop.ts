/**
 * Drag-to-pin — the SIDEBAR's drop target for the pointer session drag
 * (`app/chat/session-drag.ts`). Dragging a row over the **Pinned** section
 * pins it at the hovered slot; dragging a pinned row over the flat
 * **Sessions** list unpins it and drops it at the hovered slot; dragging
 * within either list reorders it. Everything else about the drag is
 * unchanged — the same gesture still stacks/splits/links over a chat zone.
 *
 * Same contract as every other resolver over the shared drag session: the
 * geometry is snapshotted ONCE at drag start and each pointermove is pure
 * math against that cache, and NOTHING moves until release — the target list
 * renders an insertion line, never a live shuffle (`drag-session.ts`).
 *
 * A list registers itself instead of being discovered: the rendered rows are
 * only a WINDOW when the list virtualizes, so the drop-commit must run
 * against the section's own full ordering, not the DOM. `useSessionDropList`
 * publishes that ordering (plus the commit) for the element carrying
 * `data-session-list`; the snapshot reads both. Keyed per element, so a
 * second sidebar instance can never inherit the first one's list.
 */

import { atom } from 'nanostores'
import { type RefObject, useEffect, useRef } from 'react'

import { queryAllVisible } from '@/components/pane-shell/pane-visibility'
import { rectContains } from '@/components/pane-shell/tree/renderer/drag-session'
import type { ZoneRect } from '@/components/pane-shell/tree/zones-engine'

import type { SessionDragPayload } from '../composer/inline-refs'

/** Which flat list a section renders — the two drag-to-pin surfaces. */
export type SessionListKind = 'pinned' | 'sessions'

export interface SessionDropList {
  /** Stable per-section id; the published hint names it. */
  id: string
  kind: SessionListKind
  /** Every live session id the list owns IN ORDER — including rows a
   *  virtualizer hasn't painted, which the DOM alone can't see. */
  ids: string[]
  /** Commit: place the dragged session before `before` (`null` = at the end),
   *  pinning / unpinning as the target list requires. */
  onDrop: (payload: SessionDragPayload, before: null | string) => void
}

/** The live drop target under the pointer, for the target list's insertion
 *  line. Deliberately its own atom: `$dropHint` churns for every drag in the
 *  app, and only the hovered section cares about this one. */
export const $sessionListDrop = atom<null | SessionListDrop>(null)

/** Publish only real changes: `resolveMove` runs every frame, and the hovered
 *  section must not re-render for a pointer that stayed in the same slot. */
export function publishSessionListDrop(next: null | SessionListDrop) {
  const prev = $sessionListDrop.get()

  if (prev?.listId !== next?.listId || prev?.before !== next?.before || prev?.offsetY !== next?.offsetY) {
    $sessionListDrop.set(next)
  }
}

export interface SessionListDrop {
  listId: string
  /** Insert before this live session id; `null` = append. */
  before: null | string
  /** Insertion line's y, relative to the section element's top. */
  offsetY: number
}

const registry = new WeakMap<HTMLElement, RefObject<null | SessionDropList>>()

/**
 * Mark a section as a drag-to-pin target. Spread the returned ref onto the
 * element that also carries `data-session-list` (the whole section, header
 * included — dropping on a collapsed Pinned header still pins). Pass `null`
 * while the section isn't rendering its flat list (project / grouped views
 * derive their order and never accept a drop).
 */
export function useSessionDropList(list: null | SessionDropList) {
  const ref = useRef<HTMLDivElement>(null)
  // Read at drag start, never at render: hold the latest value in a box so a
  // per-render identity change costs nothing.
  const latest = useRef<null | SessionDropList>(list)
  latest.current = list

  useEffect(() => {
    const el = ref.current

    if (!el) {
      return
    }

    registry.set(el, latest)

    return () => {
      registry.delete(el)
    }
  }, [])

  return ref
}

/** A registered list's drag-start geometry. */
export interface SessionListSnapshot {
  list: SessionDropList
  rect: ZoneRect
  /** Rows the list owns, top-to-bottom: id + vertical midpoint + top edge. */
  slots: { bottom: number; id: string; mid: number; top: number }[]
}

export function snapshotSessionLists(): SessionListSnapshot[] {
  const snapshots: SessionListSnapshot[] = []

  for (const el of queryAllVisible<HTMLElement>('[data-session-list]')) {
    const list = registry.get(el)?.current

    if (!list) {
      continue
    }

    const owned = new Set(list.ids)
    const r = el.getBoundingClientRect()

    const slots = [...el.querySelectorAll<HTMLElement>('[data-session-slot]')]
      .filter(row => owned.has(row.dataset.sessionSlot ?? ''))
      .map(row => {
        const rr = row.getBoundingClientRect()

        return { bottom: rr.bottom, id: row.dataset.sessionSlot!, mid: rr.top + rr.height / 2, top: rr.top }
      })
      .sort((a, b) => a.top - b.top)

    snapshots.push({ list, rect: { left: r.left, top: r.top, right: r.right, bottom: r.bottom }, slots })
  }

  return snapshots
}

/** Pinned accepts anything (that's the pin gesture). The flat Sessions list
 *  accepts a pinned row (unpin) or one of its OWN rows (reorder) — never a
 *  cron/messaging row, which would otherwise be spliced into the saved
 *  Sessions order while still living in its own section. */
function accepts(list: SessionDropList, payload: SessionDragPayload): boolean {
  return list.kind === 'pinned' || payload.pinned === true || list.ids.includes(payload.id)
}

/**
 * The list under the pointer and the slot a release would drop into — the
 * dragged row itself is excluded from the slots, so re-dropping it where it
 * already sits is a no-op rather than an off-by-one.
 */
export function resolveSessionListDrop(
  lists: SessionListSnapshot[],
  payload: SessionDragPayload,
  x: number,
  y: number
): null | SessionListDrop {
  const hit = lists.find(entry => rectContains(entry.rect, x, y))

  if (!hit || !accepts(hit.list, payload)) {
    return null
  }

  const slots = hit.slots.filter(slot => slot.id !== payload.id)
  const before = slots.find(slot => y < slot.mid)
  const last = slots[slots.length - 1]

  return {
    before: before?.id ?? null,
    listId: hit.list.id,
    offsetY: (before?.top ?? last?.bottom ?? hit.rect.top) - hit.rect.top
  }
}

export function commitSessionListDrop(
  lists: SessionListSnapshot[],
  drop: SessionListDrop,
  payload: SessionDragPayload
) {
  lists.find(entry => entry.list.id === drop.listId)?.list.onDrop(payload, drop.before)
}
