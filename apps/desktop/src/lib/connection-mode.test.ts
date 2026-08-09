import { describe, expect, it } from 'vitest'

import type { HermesConnection } from '@/global'

import { resolveConnectionMode, withConnectionMode } from './connection-mode'

const conn = (over: Partial<HermesConnection> = {}) =>
  ({ baseUrl: 'http://127.0.0.1:8787', ...over }) as HermesConnection

describe('resolveConnectionMode', () => {
  it.each(['local', 'remote'] as const)('passes through the resolved %s mode', mode => {
    expect(resolveConnectionMode(conn({ mode }))).toBe(mode)
  })

  it.each([
    ['no descriptor yet', null],
    ['bridge unavailable', undefined]
  ])('resolves null when there is %s', (_label, value) => {
    expect(resolveConnectionMode(value)).toBeNull()
  })

  it('resolves null rather than guessing local for an unset mode', () => {
    // An older shell that predates the field. Claiming "local" would tell an
    // extension a gateway-side file is openable here when it may not be.
    expect(resolveConnectionMode(conn())).toBeNull()
    expect(resolveConnectionMode(conn({ mode: 'cloud' as never }))).toBeNull()
  })
})

describe('withConnectionMode', () => {
  it.each(['session.create', 'session.resume', 'prompt.submit'])('stamps the mode onto %s', method => {
    expect(withConnectionMode(method, { text: 'hi' }, 'remote')).toEqual({
      connection_mode: 'remote',
      text: 'hi'
    })
  })

  it('leaves unrelated RPCs untouched', () => {
    const params = { limit: 40 }

    expect(withConnectionMode('session.list', params, 'remote')).toBe(params)
  })

  it('adds no key when the mode is unknown', () => {
    // Omitting is deliberate: it leaves any previously-announced value intact
    // on the backend instead of clearing it during a reconnect window.
    const params = { text: 'hi' }

    expect(withConnectionMode('prompt.submit', params, null)).toBe(params)
  })

  it('never overrides a mode a caller set explicitly', () => {
    expect(withConnectionMode('prompt.submit', { connection_mode: 'local' }, 'remote')).toEqual({
      connection_mode: 'local'
    })
  })

  it('does not mutate the caller params', () => {
    const params = { text: 'hi' }
    withConnectionMode('prompt.submit', params, 'local')

    expect(params).toEqual({ text: 'hi' })
  })
})
