import { describe, expect, it } from 'vitest'

import { commandPaletteQueryKey } from './query-scope'

const connection = {
  baseUrl: 'https://gateway.example',
  connectionId: 'gateway-a',
  mode: 'remote' as const,
  profile: 'default',
  remoteIdentity: 'gateway.example'
}

describe('commandPaletteQueryKey', () => {
  it('changes when the active gateway profile changes', () => {
    const first = commandPaletteQueryKey('sessions', connection, 'default')
    const second = commandPaletteQueryKey('sessions', connection, 'research')

    expect(first).not.toEqual(second)
    expect(first).toEqual(['command-palette', 'sessions', 'gateway-a', 'remote', 'https://gateway.example', 'gateway.example', 'default', 'default'])
    expect(second).toEqual(['command-palette', 'sessions', 'gateway-a', 'remote', 'https://gateway.example', 'gateway.example', 'default', 'research'])
  })

  it('keeps gateway identities distinct without delimiter collisions', () => {
    const first = commandPaletteQueryKey('config', { ...connection, connectionId: 'a:b', profile: 'c' }, 'd')
    const second = commandPaletteQueryKey('config', { ...connection, connectionId: 'a', profile: 'b:c' }, 'd')

    expect(first).not.toEqual(second)
  })
})
