import { describe, expect, it, vi } from 'vitest'

import type { SessionDragPayload } from '../composer/inline-refs'

import { commitSessionListDrop, resolveSessionListDrop, type SessionListSnapshot } from './session-drop'

/**
 * Drag-to-pin's targeting is pure math over the drag-start snapshot, so it
 * tests as data: which list the pointer is in, whether that list accepts the
 * dragged row, and which slot a release lands in.
 */

const RECT = { left: 0, top: 100, right: 240, bottom: 200 }

/** Three 20px rows stacked from y=100 (mids at 110 / 130 / 150). */
const slots = (ids: string[]) =>
  ids.map((id, index) => ({ bottom: 120 + index * 20, id, mid: 110 + index * 20, top: 100 + index * 20 }))

const list = (kind: 'pinned' | 'sessions', ids: string[], onDrop = vi.fn(), rect = RECT): SessionListSnapshot => ({
  list: { id: `list:${kind}`, ids, kind, onDrop },
  rect,
  slots: slots(ids)
})

const drag = (id: string, pinned = false): SessionDragPayload => ({ id, pinned, profile: 'default', title: id })

describe('resolveSessionListDrop', () => {
  it('returns null when the pointer is outside every list', () => {
    expect(resolveSessionListDrop([list('pinned', ['a', 'b'])], drag('x'), 10, 400)).toBeNull()
  })

  it('drops before the row whose midpoint the pointer is above', () => {
    expect(resolveSessionListDrop([list('pinned', ['a', 'b', 'c'])], drag('x'), 10, 125)).toMatchObject({
      before: 'b',
      listId: 'list:pinned'
    })
  })

  it('appends past the last row midpoint', () => {
    expect(resolveSessionListDrop([list('pinned', ['a', 'b', 'c'])], drag('x'), 10, 195)?.before).toBeNull()
  })

  it('ignores the dragged row itself, so re-dropping in place is a no-op', () => {
    // Over b's own slot: without the exclusion this would resolve "before b".
    expect(resolveSessionListDrop([list('pinned', ['a', 'b', 'c'])], drag('b'), 10, 125)?.before).toBe('c')
  })

  it('offsets the insertion line to the target row top, or the last row bottom', () => {
    const pinned = [list('pinned', ['a', 'b', 'c'])]

    expect(resolveSessionListDrop(pinned, drag('x'), 10, 125)?.offsetY).toBe(20)
    expect(resolveSessionListDrop(pinned, drag('x'), 10, 195)?.offsetY).toBe(60)
  })

  it('accepts any row on Pinned — that is the pin gesture', () => {
    expect(resolveSessionListDrop([list('pinned', [])], drag('fresh'), 10, 150)).toMatchObject({ before: null })
  })

  it('accepts a pinned row on Sessions (unpin) and its own rows (reorder)', () => {
    const sessions = [list('sessions', ['a', 'b'])]

    expect(resolveSessionListDrop(sessions, drag('pinned-row', true), 10, 125)).not.toBeNull()
    expect(resolveSessionListDrop(sessions, drag('a'), 10, 145)).not.toBeNull()
  })

  it('rejects a foreign row on Sessions, so a cron/messaging row is never spliced in', () => {
    expect(resolveSessionListDrop([list('sessions', ['a', 'b'])], drag('cron-row'), 10, 125)).toBeNull()
  })
})

describe('commitSessionListDrop', () => {
  it('routes the drop to the list the hint named', () => {
    const onPinned = vi.fn()
    const onSessions = vi.fn()

    const lists = [
      list('pinned', ['a'], onPinned),
      list('sessions', ['b'], onSessions, { left: 0, top: 300, right: 240, bottom: 400 })
    ]

    const payload = drag('a', true)

    commitSessionListDrop(lists, { before: 'b', listId: 'list:sessions', offsetY: 0 }, payload)

    expect(onSessions).toHaveBeenCalledWith(payload, 'b')
    expect(onPinned).not.toHaveBeenCalled()
  })
})
