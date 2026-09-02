import { describe, expect, it } from 'vitest'

import { revealArtLines } from '../banner.js'

describe('revealArtLines', () => {
  const lines: [string, string][] = [
    ['#ffffff', 'ABCD'],
    ['#ffffff', 'XY']
  ]

  it('reveals the art left-to-right without changing its width', () => {
    expect(revealArtLines(lines, 2)).toEqual([
      ['#ffffff', 'AB  '],
      ['#ffffff', 'XY  ']
    ])
  })

  it('clamps frames before the start and after the end', () => {
    expect(revealArtLines(lines, -1)).toEqual([
      ['#ffffff', '    '],
      ['#ffffff', '    ']
    ])
    expect(revealArtLines(lines, 99)).toEqual([
      ['#ffffff', 'ABCD'],
      ['#ffffff', 'XY  ']
    ])
  })
})