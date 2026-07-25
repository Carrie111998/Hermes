import assert from 'node:assert/strict'

import { test } from 'vitest'

import {
  DEFAULT_HEALTH_PROBE_TIMEOUT_MS,
  isAuthRejectionError,
  isMissingHealthEndpointError,
  isReauthRequiredError,
  makeReauthRequiredError,
  REMOTE_SESSION_EXPIRED_MESSAGE,
  waitForHermesReady
} from './backend-health'

test('uses lightweight /api/health for current backends', async () => {
  const calls: string[][] = []

  await waitForHermesReady('http://127.0.0.1:9000/', {
    token: 'secret-token',
    fetchPublicJson: async url => {
      calls.push(['public', url])

      return { ok: true }
    },
    fetchJson: async url => {
      calls.push(['token', url])
      throw new Error('status should not be called')
    },
    sleep: async () => {},
    timeoutMs: 100,
    pollMs: 1
  })

  assert.deepEqual(calls, [['public', 'http://127.0.0.1:9000/api/health']])
})

test('falls back to /api/status only for old backends without /api/health', async () => {
  const calls: string[][] = []

  await waitForHermesReady('http://127.0.0.1:9000', {
    token: 'secret-token',
    fetchPublicJson: async url => {
      calls.push(['public', url])

      throw new Error('404: {"detail":"Not Found"}')
    },
    fetchJson: async (url, token) => {
      calls.push(['token', url, token ?? ''])

      return { version: 'old' }
    },
    sleep: async () => {},
    timeoutMs: 100,
    pollMs: 1
  })

  assert.deepEqual(calls, [
    ['public', 'http://127.0.0.1:9000/api/health'],
    ['token', 'http://127.0.0.1:9000/api/status', 'secret-token']
  ])
})

test('does not fall back to heavyweight /api/status for transient health failures', async () => {
  const calls: string[][] = []
  let currentTime = 0

  await assert.rejects(
    waitForHermesReady('http://127.0.0.1:9000', {
      fetchPublicJson: async url => {
        calls.push(['public', url])
        throw new Error('Timed out connecting to Hermes backend after 15000ms')
      },
      fetchJson: async url => {
        calls.push(['token', url])
      },
      sleep: async () => {},
      now: () => {
        currentTime += 20

        return currentTime
      },
      timeoutMs: 50,
      pollMs: 1
    }),
    /Timed out connecting/
  )

  assert.ok(calls.length > 0)
  assert.ok(calls.every(call => call[0] === 'public' && call[1].endsWith('/api/health')))
})

test('probes health on a short timeout but leaves the legacy fallback its own', async () => {
  const timeouts: (number | undefined)[] = []

  await waitForHermesReady('http://127.0.0.1:9000', {
    fetchPublicJson: async (_url, options) => {
      timeouts.push(options?.timeoutMs)

      throw new Error('404: {"detail":"Not Found"}')
    },
    fetchJson: async (_url, _token, options) => {
      timeouts.push(options?.timeoutMs)

      return { version: 'old' }
    },
    sleep: async () => {},
    timeoutMs: 100,
    pollMs: 1
  })

  assert.deepEqual(timeouts, [DEFAULT_HEALTH_PROBE_TIMEOUT_MS, undefined])
})

test('aborts as superseded when the bootstrap signal fires', async () => {
  const controller = new AbortController()
  controller.abort()

  await assert.rejects(
    waitForHermesReady('http://127.0.0.1:9000', {
      signal: controller.signal,
      fetchPublicJson: async () => {
        throw new Error('should not probe after abort')
      },
      fetchJson: async () => {
        throw new Error('should not probe after abort')
      },
      timeoutMs: 100,
      pollMs: 1
    }),
    (error: any) => error.kind === 'superseded'
  )
})

test('recognizes missing-route shapes only', () => {
  assert.equal(isMissingHealthEndpointError(new Error('404: {"detail":"Not Found"}')), true)
  assert.equal(
    isMissingHealthEndpointError(
      new Error('Expected JSON from /api/health but got HTML. The endpoint is likely missing on the Hermes backend.')
    ),
    true
  )
  assert.equal(isMissingHealthEndpointError(new Error('Timed out connecting to Hermes backend after 15000ms')), false)
  assert.equal(isMissingHealthEndpointError(new Error('500: boom')), false)
})

test('uses the credentialed probeHealth for /api/health when provided', async () => {
  const calls: string[][] = []

  await waitForHermesReady('http://gateway.example/hermes', {
    // A credential-free fetch would 401 no_cookie on a gated gateway — the probe
    // must go through the authed path instead.
    fetchPublicJson: async url => {
      calls.push(['public', url])
      throw new Error('401: {"reason":"no_cookie"}')
    },
    fetchJson: async () => {
      throw new Error('status should not be called')
    },
    probeHealth: async url => {
      calls.push(['authed', url])

      return { ok: true }
    },
    sleep: async () => {},
    timeoutMs: 100,
    pollMs: 1
  })

  assert.deepEqual(calls, [['authed', 'http://gateway.example/hermes/api/health']])
})

test('fails fast (no polling) when the credentialed probe reports reauth-required', async () => {
  let probes = 0
  let slept = 0

  await assert.rejects(
    waitForHermesReady('http://gateway.example/hermes', {
      fetchPublicJson: async () => ({ ok: true }),
      fetchJson: async () => {
        throw new Error('status should not be called')
      },
      // Simulates the oauth probe converting a confirmed 401 into the terminal
      // reauth error — polling can't revive an expired session.
      probeHealth: async () => {
        probes += 1
        throw makeReauthRequiredError(new Error('401: {"reason":"no_cookie"}'))
      },
      sleep: async () => {
        slept += 1
      },
      timeoutMs: 10_000,
      pollMs: 1
    }),
    (error: unknown) => isReauthRequiredError(error) && error instanceof Error
  )

  // One probe, then straight out — not a 10s poll loop.
  assert.equal(probes, 1)
  assert.equal(slept, 0)
})

test('isAuthRejectionError matches a confirmed 401/403 status prefix only', () => {
  assert.equal(isAuthRejectionError(new Error('401: {"reason":"no_cookie"}')), true)
  assert.equal(isAuthRejectionError(new Error('403: {"detail":"Forbidden"}')), true)
  assert.equal(isAuthRejectionError(new Error('404: {"detail":"Not Found"}')), false)
  assert.equal(isAuthRejectionError(new Error('500: boom')), false)
  assert.equal(isAuthRejectionError(new Error('Timed out connecting to Hermes backend after 5000ms')), false)
})

test('reauth-required error is tagged for both the boot latch and the renderer overlay', () => {
  const error = makeReauthRequiredError(new Error('401: {"reason":"no_cookie"}'))

  // Duck-typed needsOauthLogin so isReauthRequiredError (and @hermes/shared's
  // isGatewayReauthRequired) recognize it without a shared import.
  assert.equal(isReauthRequiredError(error), true)
  // Message carries the phrase the renderer's isRemoteReauthError keys on.
  assert.equal(error.message, REMOTE_SESSION_EXPIRED_MESSAGE)
  assert.ok(error.message.toLowerCase().includes('remote gateway session has expired'))
  assert.equal(isReauthRequiredError(new Error('plain error')), false)
  assert.equal(isReauthRequiredError(null), false)
})

test('routes the /api/status fallback through the credentialed probe, not the raw token fetch', async () => {
  const calls: string[] = []

  await waitForHermesReady('http://gateway.example/hermes', {
    fetchPublicJson: async () => {
      throw new Error('fetchPublicJson must not be used when a credentialed probe is provided')
    },
    // An oauth remote's token is null and fetchJson attaches no cookie/bearer —
    // the fallback must NOT fall back to this uncredentialed path.
    fetchJson: async () => {
      throw new Error('legacy uncredentialed fetchJson must not probe /api/status for a credentialed connection')
    },
    probeHealth: async url => {
      calls.push(url)
      // Old backend: no /api/health → 404 drives the fallback; /api/status then
      // answers over the SAME credentialed probe.
      if (url.endsWith('/api/health')) {
        throw new Error('404: {"detail":"Not Found"}')
      }

      return { ok: true }
    },
    sleep: async () => {},
    timeoutMs: 100,
    pollMs: 1
  })

  assert.deepEqual(calls, ['http://gateway.example/hermes/api/health', 'http://gateway.example/hermes/api/status'])
})

test('fails fast when the credentialed /api/status fallback reports reauth', async () => {
  let slept = 0

  await assert.rejects(
    waitForHermesReady('http://gateway.example/hermes', {
      fetchPublicJson: async () => ({ ok: true }),
      fetchJson: async () => {
        throw new Error('legacy fetchJson must not be used')
      },
      probeHealth: async url => {
        if (url.endsWith('/api/health')) {
          throw new Error('404: {"detail":"Not Found"}')
        }
        // Auth-gated /api/status on an old oauth gateway → terminal reauth.
        throw makeReauthRequiredError(new Error('401: {"reason":"no_cookie"}'))
      },
      sleep: async () => {
        slept += 1
      },
      timeoutMs: 10_000,
      pollMs: 1
    }),
    (error: unknown) => isReauthRequiredError(error)
  )

  // Straight out on the reauth from the fallback leg — not a poll-to-timeout.
  assert.equal(slept, 0)
})
