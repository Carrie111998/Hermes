import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $connectionsRegistry } from './connection-registry-state'
import { $profiles } from './profile'
import {
  ambientGatewayOwnsEverySession,
  assertSessionOwnerResolved,
  sessionOwnerIsKnown
} from './session-owner-resolution'

const registry = (...connections: Array<{ id: string; kind: string }>) =>
  ({
    connections,
    lastUsed: connections[0]?.id ?? null,
    launchMode: 'primary',
    primary: connections[0]?.id ?? null
  }) as never

beforeEach(() => {
  $connectionsRegistry.set(null)
  $profiles.set([])
})

afterEach(() => {
  delete (window as unknown as { hermesDesktop?: unknown }).hermesDesktop
})

describe('session owner topology', () => {
  it('fails closed while the modern registry bridge is present but its async cache is not loaded', () => {
    ;(window as unknown as { hermesDesktop?: unknown }).hermesDesktop = {
      connections: { list: vi.fn(async () => Promise.reject(new Error('ipc unavailable'))) }
    }
    $connectionsRegistry.set(null)
    $profiles.set([{ name: 'default' }] as never)

    expect(sessionOwnerIsKnown('default')).toBe(true)
    expect(ambientGatewayOwnsEverySession()).toBe(false)
    expect(() =>
      assertSessionOwnerResolved('default', { method: 'session.resume', sessionId: 'registry-loading' })
    ).not.toThrow()
    expect(() => assertSessionOwnerResolved(null, { method: 'session.resume', sessionId: 'registry-loading' })).toThrow(
      /could not be resolved/i
    )
  })

  it('uses the ambient gateway only when a loaded registry proves a sole local backend', () => {
    $connectionsRegistry.set(registry({ id: 'local', kind: 'local' }))
    $profiles.set([{ name: 'default' }] as never)

    expect(sessionOwnerIsKnown('default')).toBe(true)
    expect(ambientGatewayOwnsEverySession()).toBe(true)
    expect(() =>
      assertSessionOwnerResolved(null, { method: 'approval.respond', sessionId: 'sole-local' })
    ).not.toThrow()

    $connectionsRegistry.set(registry({ id: 'remote', kind: 'remote' }))
    expect(ambientGatewayOwnsEverySession()).toBe(false)
    expect(() => assertSessionOwnerResolved(null, { method: 'approval.respond', sessionId: 'remote-only' })).toThrow(
      /could not be resolved/i
    )

    $connectionsRegistry.set(
      registry(
        { id: 'local', kind: 'local' },
        { id: 'homelab', kind: 'remote' }
      )
    )
    expect(sessionOwnerIsKnown(null)).toBe(false)
    expect(ambientGatewayOwnsEverySession()).toBe(false)
    expect(() => assertSessionOwnerResolved(null, { method: 'session.resume', sessionId: 'unknown-owner' })).toThrow(
      /could not be resolved/i
    )

    $connectionsRegistry.set(registry({ id: 'local', kind: 'local' }))
    $profiles.set([{ name: 'default' }, { name: 'loki' }] as never)
    expect(ambientGatewayOwnsEverySession()).toBe(false)
    expect(() => assertSessionOwnerResolved(null, { method: 'approval.respond', sessionId: 'multi-profile' })).toThrow(
      /could not be resolved/i
    )
  })

  it('preserves legacy profile-only owner routing', () => {
    $connectionsRegistry.set(null)
    $profiles.set([{ name: 'default' }] as never)

    expect(sessionOwnerIsKnown('default')).toBe(true)
    expect(ambientGatewayOwnsEverySession()).toBe(true)
    expect(() =>
      assertSessionOwnerResolved(null, { method: 'session.resume', sessionId: 'legacy-single-profile' })
    ).not.toThrow()

    $profiles.set([{ name: 'default' }, { name: 'loki' }] as never)
    expect(sessionOwnerIsKnown('loki')).toBe(true)
    expect(ambientGatewayOwnsEverySession()).toBe(false)
    expect(() =>
      assertSessionOwnerResolved('loki', { method: 'session.resume', sessionId: 'legacy-profile-owner' })
    ).not.toThrow()
  })
})
