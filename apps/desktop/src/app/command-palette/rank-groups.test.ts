import { describe, expect, it } from 'vitest'

import { rankPaletteGroups } from './rank-groups'

interface Item {
  id: string
  keywords?: string[]
  label: string
}

const item = (id: string, label = `Session ${id}`, keywords?: string[]): Item => ({ id, keywords, label })

describe('rankPaletteGroups', () => {
  it('bounds matching rows across groups after ranking', () => {
    const groups = [
      { heading: 'Sessions', items: Array.from({ length: 80 }, (_, index) => item(`session-${index}`)) },
      { heading: 'Archived', items: Array.from({ length: 80 }, (_, index) => item(`archived-${index}`)) }
    ]

    const ranked = rankPaletteGroups(groups, 'session', 50)

    expect(ranked.flatMap(group => group.items)).toHaveLength(50)
    expect(ranked.every(group => group.items.length > 0)).toBe(true)
  })

  it('orders exact and prefix matches before weaker keyword matches', () => {
    const groups = [
      { heading: 'Weak', items: [item('weak', 'Other', ['tools'])] },
      { heading: 'Exact', items: [item('exact', 'Tools')] },
      { heading: 'Prefix', items: [item('prefix', 'Tools and skills')] }
    ]

    const ranked = rankPaletteGroups(groups, 'tools', 10)

    expect(ranked.map(group => group.heading)).toEqual(['Exact', 'Prefix', 'Weak'])
  })

  it('keeps stronger later-group matches ahead of weak rows from the first group', () => {
    const groups = [
      {
        heading: 'First',
        items: [item('exact', 'Tools'), ...Array.from({ length: 8 }, (_, index) => item(`weak-${index}`, 'Other', ['tools']))]
      },
      {
        heading: 'Second',
        items: [item('prefix-a', 'Tools alpha'), item('prefix-b', 'Tools beta')]
      }
    ]

    const ranked = rankPaletteGroups(groups, 'tools', 4)

    expect(ranked.find(group => group.heading === 'First')?.items.map(entry => entry.id)).toEqual(['exact', 'weak-0'])
    expect(ranked.find(group => group.heading === 'Second')?.items.map(entry => entry.id)).toEqual([
      'prefix-a',
      'prefix-b'
    ])
  })

  it('round-robins equal-score capacity across groups', () => {
    const groups = ['First', 'Second'].map(heading => ({
      heading,
      items: Array.from({ length: 10 }, (_, index) => item(`${heading}-${index}`, 'Tools match'))
    }))

    const ranked = rankPaletteGroups(groups, 'tools', 6)

    expect(ranked.map(group => group.items.length)).toEqual([3, 3])
  })

  it('leaves curated empty-search groups intact', () => {
    const groups = [{ heading: 'Curated', items: Array.from({ length: 70 }, (_, index) => item(String(index))) }]

    expect(rankPaletteGroups(groups, '', 50)).toBe(groups)
  })

  it('does not mutate source groups or item order', () => {
    const groups = [{ heading: 'Sessions', items: [item('second', 'Session two'), item('first', 'Session one')] }]
    const original = groups[0]!.items.slice()

    rankPaletteGroups(groups, 'session', 1)

    expect(groups[0]!.items).toEqual(original)
  })
})
