import { describe, expect, it } from 'vitest'

import { buildBoardWipPatch, parseBoardWipLimit } from './board-switcher'

describe('board WIP limit editor contract', () => {
  it('accepts positive integers, treats blank as unlimited, and rejects invalid input', () => {
    expect(parseBoardWipLimit(' 3 ')).toBe(3)
    expect(parseBoardWipLimit('')).toBeNull()
    expect(parseBoardWipLimit('0')).toBeUndefined()
    expect(parseBoardWipLimit('1.5')).toBeUndefined()
    expect(parseBoardWipLimit('true')).toBeUndefined()
  })

  it('preserves board name and project fields in the exact PATCH payload', () => {
    expect(buildBoardWipPatch('  Board  ', 'project-1', 4)).toEqual({
      name: 'Board',
      project_id: 'project-1',
      wip_limit: 4
    })
    expect(buildBoardWipPatch('Board', '', null)).toEqual({
      name: 'Board',
      project_id: '',
      wip_limit: null
    })
  })
})
