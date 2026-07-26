import { describe, expect, it } from 'vitest'

import { isSameProjectPath, normalizeProjectPath, projectPathLabel } from './project-paths'

describe('normalizeProjectPath', () => {
  it('treats trailing-slash, doubled-separator, and dot-segment spellings as one path', () => {
    const canonical = normalizeProjectPath('/Users/me/code/hermes')

    for (const alias of [
      '/Users/me/code/hermes/',
      '/Users/me/code/hermes//',
      '/Users/me//code/hermes',
      '/Users/me/code/./hermes',
      '  /Users/me/code/hermes  ',
      '/Users/me/code/other/../hermes'
    ]) {
      expect(normalizeProjectPath(alias)).toBe(canonical)
    }
  })

  it('does not collapse a leading .. that would change meaning', () => {
    expect(normalizeProjectPath('../sibling/project')).toBe('../sibling/project')
  })

  it('keeps absolute and relative paths distinguishable', () => {
    expect(normalizeProjectPath('/a/b')).toBe('/a/b')
    expect(normalizeProjectPath('a/b')).toBe('a/b')
  })

  it('normalizes windows separators and drive-letter case', () => {
    expect(normalizeProjectPath('C:\\Users\\me\\proj')).toBe(normalizeProjectPath('c:/Users/me/proj'))
  })

  it('rejects empty and absurdly long values', () => {
    expect(normalizeProjectPath('')).toBe('')
    expect(normalizeProjectPath('   ')).toBe('')
    expect(normalizeProjectPath(null)).toBe('')
    expect(normalizeProjectPath(`/${'x'.repeat(5000)}`)).toBe('')
  })

  it('does NOT expand ~ (only the main process can resolve it)', () => {
    // Documents a real limit: `~/code` and its expanded form are different
    // entries here. The backend-echoed cwd is what collapses them in practice.
    expect(normalizeProjectPath('~/code')).toBe('~/code')
  })
})

describe('isSameProjectPath', () => {
  it('matches across spellings and rejects empties', () => {
    expect(isSameProjectPath('/a/b/', '/a//b')).toBe(true)
    expect(isSameProjectPath('/a/b', '/a/c')).toBe(false)
    expect(isSameProjectPath('', '')).toBe(false)
  })
})

describe('projectPathLabel', () => {
  it('uses the final segment regardless of trailing slash', () => {
    expect(projectPathLabel('/Users/me/code/hermes/')).toBe('hermes')
    expect(projectPathLabel('/Users/me/code/hermes')).toBe('hermes')
  })

  it('falls back to the path itself at the root', () => {
    expect(projectPathLabel('/')).toBe('/')
    expect(projectPathLabel('')).toBe('')
  })
})
