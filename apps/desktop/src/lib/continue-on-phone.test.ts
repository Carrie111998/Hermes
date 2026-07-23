import { describe, expect, it, vi } from 'vitest'

import {
  buildDashboardSessionUrl,
  type ContinueOnPhoneDependencies,
  isPhoneHandoffAuthMode,
  resolveContinueOnPhoneUrl
} from './continue-on-phone'

function dependencies(
  overrides: Partial<ContinueOnPhoneDependencies> = {}
): ContinueOnPhoneDependencies {
  return {
    getRemoteAccess: vi.fn().mockResolvedValue({ public_url: 'https://hermes.example.com/agent' }),
    mintHandoffTicket: vi.fn().mockResolvedValue({
      ticket: 'handoff-ticket-abc',
      ttl_seconds: 120,
      session_id: 'session-42',
      profile: 'work'
    }),
    probe: vi.fn().mockResolvedValue({ authMode: 'oauth', reachable: true }),
    ...overrides
  }
}

describe('buildDashboardSessionUrl', () => {
  it('preserves a dashboard path prefix and scopes the resumed session', () => {
    expect(buildDashboardSessionUrl('https://hermes.example.com/agent', 'session / 42', 'work profile')).toBe(
      'https://hermes.example.com/agent/chat?resume=session+%2F+42&profile=work+profile'
    )
  })

  it('appends a handoff ticket when provided', () => {
    expect(
      buildDashboardSessionUrl(
        'https://hermes.example.com/agent',
        'session-42',
        'work',
        'ticket/with spaces'
      )
    ).toBe(
      'https://hermes.example.com/agent/chat?resume=session-42&profile=work&handoff=ticket%2Fwith+spaces'
    )
  })

  it('requires an HTTPS public URL', () => {
    expect(buildDashboardSessionUrl('http://hermes.example.com', 'session-42')).toBeNull()
    expect(buildDashboardSessionUrl('not a url', 'session-42')).toBeNull()
    expect(buildDashboardSessionUrl('https://hermes.example.com', '')).toBeNull()
  })

  it('never carries URL credentials into the handoff', () => {
    expect(buildDashboardSessionUrl('https://user:password@hermes.example.com', 'session-42')).toBeNull()
  })
})

describe('isPhoneHandoffAuthMode', () => {
  it('accepts oauth and handoff gated modes', () => {
    expect(isPhoneHandoffAuthMode('oauth')).toBe(true)
    expect(isPhoneHandoffAuthMode('handoff')).toBe(true)
    expect(isPhoneHandoffAuthMode('token')).toBe(false)
    expect(isPhoneHandoffAuthMode('unknown')).toBe(false)
  })
})

describe('resolveContinueOnPhoneUrl', () => {
  it('returns a handoff URL without embedding credentials', async () => {
    const deps = dependencies()

    const result = await resolveContinueOnPhoneUrl('session-42', 'work', deps)

    expect(result).toEqual({
      ok: true,
      url: 'https://hermes.example.com/agent/chat?resume=session-42&profile=work&handoff=handoff-ticket-abc'
    })
    expect(deps.getRemoteAccess).toHaveBeenCalledWith('work')
    expect(deps.probe).toHaveBeenCalledWith('https://hermes.example.com/agent')
    expect(deps.mintHandoffTicket).toHaveBeenCalledWith('session-42', 'work')
    expect(result.ok && result.url).not.toContain('token=')
  })

  it('accepts handoff auth mode from the probe', async () => {
    const deps = dependencies({
      probe: vi.fn().mockResolvedValue({ authMode: 'handoff', reachable: true })
    })

    const result = await resolveContinueOnPhoneUrl('session-42', undefined, deps)

    expect(result.ok).toBe(true)
    expect(result.ok && result.url).toContain('handoff=handoff-ticket-abc')
  })

  it('refuses a dashboard without a configured public URL', async () => {
    const result = await resolveContinueOnPhoneUrl(
      'session-42',
      undefined,
      dependencies({ getRemoteAccess: vi.fn().mockResolvedValue({ public_url: '' }) })
    )

    expect(result).toEqual({ ok: false, reason: 'not-configured' })
  })

  it('refuses an insecure public URL before probing it', async () => {
    const deps = dependencies({
      getRemoteAccess: vi.fn().mockResolvedValue({ public_url: 'http://hermes.example.com' })
    })

    const result = await resolveContinueOnPhoneUrl('session-42', undefined, deps)

    expect(result).toEqual({ ok: false, reason: 'insecure-url' })
    expect(deps.probe).not.toHaveBeenCalled()
    expect(deps.mintHandoffTicket).not.toHaveBeenCalled()
  })

  it('refuses an unreachable dashboard or one without the auth gate', async () => {
    const unreachable = await resolveContinueOnPhoneUrl(
      'session-42',
      undefined,
      dependencies({ probe: vi.fn().mockResolvedValue({ authMode: 'unknown', reachable: false }) })
    )

    const unauthenticated = await resolveContinueOnPhoneUrl(
      'session-42',
      undefined,
      dependencies({ probe: vi.fn().mockResolvedValue({ authMode: 'token', reachable: true }) })
    )

    expect(unreachable).toEqual({ ok: false, reason: 'unreachable' })
    expect(unauthenticated).toEqual({ ok: false, reason: 'auth-required' })
  })

  it('surfaces mint failures without leaking a bare resume URL', async () => {
    const deps = dependencies({
      mintHandoffTicket: vi.fn().mockRejectedValue(new Error('mint failed'))
    })

    const result = await resolveContinueOnPhoneUrl('session-42', 'work', deps)

    expect(result).toEqual({ ok: false, reason: 'handoff-failed' })
  })
})
