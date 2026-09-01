/**
 * When the roster buckets bots under gateway headings, and when it lists them
 * flat.
 *
 * Sectioning is a presentation choice layered over the row list: it must never
 * drop or reorder a row, and it must step aside whenever a heading could not
 * tell rows apart (one gateway, a gateway filter) or the user asked for a flat
 * list. Everything the pane renders hangs off `sectioned`, so these pin the
 * contract rather than the markup.
 */

import { describe, expect, it } from 'vitest'

import { rosterGatewaySections } from './roster-sections'
import type { RosterRow } from './types'

const row = (name: string, connectionId: string) => ({ kind: 'bot', bot: { connectionId, name } as RosterRow }) as const

const local = { connectionId: 'local', kind: 'local', label: 'This device' }
const work = { connectionId: 'work', kind: 'ssh', label: 'Work' }

const rows = [row('alpha', 'work'), row('beta', 'local'), row('gamma', 'work')]

describe('gateway sections', () => {
  it('buckets rows per gateway when more than one gateway is known', () => {
    const result = rosterGatewaySections(rows, [local, work])

    expect(result.sectioned).toBe(true)
    expect(result.sections.map(section => [section.id, section.rows.map(r => r.bot.name)])).toEqual([
      ['local', ['beta']],
      ['work', ['alpha', 'gamma']]
    ])
  })

  it('stays flat for a single gateway or a gateway filter', () => {
    expect(rosterGatewaySections(rows, [work]).sectioned).toBe(false)
    expect(rosterGatewaySections(rows, [local, work], 'work').sectioned).toBe(false)
  })

  it('lists every row flat, in the order given, when grouping is switched off', () => {
    const result = rosterGatewaySections(rows, [local, work], 'all', false)

    expect(result.sectioned).toBe(false)
    expect(result.sections).toHaveLength(1)
    expect(result.sections[0].option).toBeNull()
    expect(result.sections[0].rows).toBe(rows)
  })
})
