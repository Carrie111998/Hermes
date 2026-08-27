import { describe, expect, it } from 'vitest'

import { cleanRemotePath, parentRemotePath, remotePathCrumbs, remotePathLeaf } from './remote-picker-paths'

describe('remote picker paths', () => {
  it('keeps a Windows drive root navigable', () => {
    expect(cleanRemotePath('C:\\')).toBe('C:')
    expect(parentRemotePath('C:\\')).toBe('C:\\')
  })

  it('walks Windows paths without treating backslashes as filename text', () => {
    expect(parentRemotePath('C:\\Users\\Steven')).toBe('C:\\Users')
    expect(remotePathLeaf('C:\\Users\\Steven')).toBe('Steven')
    expect(remotePathCrumbs('C:\\Users\\Steven')).toEqual([
      { label: 'C:', path: 'C:\\' },
      { label: 'Users', path: 'C:\\Users' },
      { label: 'Steven', path: 'C:\\Users\\Steven' }
    ])
  })

  it('preserves POSIX navigation', () => {
    expect(cleanRemotePath('/var/log/')).toBe('/var/log')
    expect(parentRemotePath('/var/log')).toBe('/var')
    expect(remotePathLeaf('/var/log')).toBe('log')
    expect(remotePathCrumbs('/var/log')).toEqual([
      { label: '/', path: '/' },
      { label: 'var', path: '/var' },
      { label: 'log', path: '/var/log' }
    ])
  })
})
