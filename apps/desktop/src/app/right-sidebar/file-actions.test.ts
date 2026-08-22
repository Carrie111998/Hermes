import { describe, expect, it } from 'vitest'

import { parentPathForEntry } from './file-actions'

describe('file action parent targeting', () => {
  it('creates inside directories and beside files', () => {
    expect(parentPathForEntry('/repo/docs', true)).toBe('/repo/docs')
    expect(parentPathForEntry('/repo/readme.md', false)).toBe('/repo')
  })

  it('preserves POSIX and Windows roots', () => {
    expect(parentPathForEntry('/file.txt', false)).toBe('/')
    expect(parentPathForEntry('C:\\file.txt', false)).toBe('C:\\')
  })
})
