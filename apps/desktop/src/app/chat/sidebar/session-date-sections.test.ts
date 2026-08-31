import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/hermes'
import type { SidebarSessionEntry } from '@/lib/session-branch-tree'

import { sessionDateBucket, withDateSections } from './session-date-sections'

// Fixed "now": Monday 2026-08-31 13:00 local.
const NOW = new Date(2026, 7, 31, 13, 0, 0)

const secondsAt = (...args: [number, number, number, number?, number?]) =>
  new Date(args[0], args[1], args[2], args[3] ?? 12, args[4] ?? 0).getTime() / 1000

function entry(id: string, lastActive: number, branchStem?: string): SidebarSessionEntry {
  return {
    branchStem,
    session: { id, last_active: lastActive, started_at: lastActive } as SessionInfo
  }
}

describe('sessionDateBucket', () => {
  it('buckets the local calendar day as today, including future timestamps', () => {
    expect(sessionDateBucket(secondsAt(2026, 7, 31, 0, 1), NOW)).toEqual({ kind: 'today' })
    expect(sessionDateBucket(secondsAt(2026, 7, 31, 23, 59), NOW)).toEqual({ kind: 'today' })
    expect(sessionDateBucket(secondsAt(2026, 8, 2), NOW)).toEqual({ kind: 'today' })
  })

  it('buckets the previous calendar day as yesterday', () => {
    expect(sessionDateBucket(secondsAt(2026, 7, 30, 23, 59), NOW)).toEqual({ kind: 'yesterday' })
    expect(sessionDateBucket(secondsAt(2026, 7, 30, 0, 0), NOW)).toEqual({ kind: 'yesterday' })
  })

  it('buckets the six days before yesterday as this week', () => {
    expect(sessionDateBucket(secondsAt(2026, 7, 29), NOW)).toEqual({ kind: 'thisWeek' })
    expect(sessionDateBucket(secondsAt(2026, 7, 24), NOW)).toEqual({ kind: 'thisWeek' })
  })

  it('buckets older same-month days as this month', () => {
    expect(sessionDateBucket(secondsAt(2026, 7, 23), NOW)).toEqual({ kind: 'thisMonth' })
    expect(sessionDateBucket(secondsAt(2026, 7, 1), NOW)).toEqual({ kind: 'thisMonth' })
  })

  it('buckets anything older by calendar month', () => {
    expect(sessionDateBucket(secondsAt(2026, 6, 31), NOW)).toEqual({ kind: 'month', month: 6, year: 2026 })
    expect(sessionDateBucket(secondsAt(2025, 11, 25), NOW)).toEqual({ kind: 'month', month: 11, year: 2025 })
  })

  it('falls into this week rather than this month across a month boundary', () => {
    // Wednesday 2026-09-02: Monday 08-31 is two days back — same week, prior month.
    const wednesday = new Date(2026, 8, 2, 13, 0, 0)

    expect(sessionDateBucket(secondsAt(2026, 7, 31), wednesday)).toEqual({ kind: 'thisWeek' })
    expect(sessionDateBucket(secondsAt(2026, 7, 20), wednesday)).toEqual({ kind: 'month', month: 7, year: 2026 })
  })
})

describe('withDateSections', () => {
  it('interleaves one header per bucket transition with stable ids', () => {
    const items = withDateSections(
      [
        entry('a', secondsAt(2026, 7, 31)),
        entry('b', secondsAt(2026, 7, 31, 9)),
        entry('c', secondsAt(2026, 7, 30)),
        entry('d', secondsAt(2026, 5, 10))
      ],
      NOW
    )

    expect(
      items.map(item => (item.kind === 'header' ? `#${item.id}` : item.entry.session.id))
    ).toEqual(['#date:today', 'a', 'b', '#date:yesterday', 'c', '#date:month:2026-5', 'd'])
  })

  it('keeps branch children in their parent section regardless of their own age', () => {
    const items = withDateSections(
      [
        entry('root', secondsAt(2026, 7, 31)),
        entry('old-branch', secondsAt(2026, 4, 1), 'feature/x'),
        entry('next', secondsAt(2026, 7, 30))
      ],
      NOW
    )

    expect(
      items.map(item => (item.kind === 'header' ? `#${item.id}` : item.entry.session.id))
    ).toEqual(['#date:today', 'root', 'old-branch', '#date:yesterday', 'next'])
  })

  it('lets an invalid timestamp inherit the surrounding section', () => {
    const items = withDateSections(
      [entry('a', secondsAt(2026, 7, 31)), entry('bad', 0), entry('c', secondsAt(2026, 7, 30))],
      NOW
    )

    expect(
      items.map(item => (item.kind === 'header' ? `#${item.id}` : item.entry.session.id))
    ).toEqual(['#date:today', 'a', 'bad', '#date:yesterday', 'c'])
  })

  it('renders no leading header when the list starts with invalid timestamps', () => {
    const items = withDateSections([entry('bad', Number.NaN)], NOW)

    expect(items).toEqual([{ entry: entry('bad', Number.NaN), kind: 'entry' }])
  })

  it('returns an empty list unchanged', () => {
    expect(withDateSections([], NOW)).toEqual([])
  })
})
