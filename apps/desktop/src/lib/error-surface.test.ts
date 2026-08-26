import { describe, expect, it } from 'vitest'

import { formatErrorDiagnostics, formatUserFacingError, parseErrorSurface } from './error-surface'

describe('parseErrorSurface', () => {
  it('accepts a valid descriptor', () => {
    expect(parseErrorSurface({ layer: 'streaming', code: 'stream_drop', retryable: true })).toEqual({
      layer: 'streaming',
      code: 'stream_drop',
      retryable: true
    })
  })

  it('accepts every documented layer', () => {
    for (const layer of ['provider', 'endpoint', 'streaming', 'auth', 'billing', 'gateway', 'runtime', 'disk']) {
      expect(parseErrorSurface({ layer, code: 'x', retryable: false })?.layer).toBe(layer)
    }
  })

  it('rejects unknown layers and non-objects', () => {
    expect(parseErrorSurface({ layer: 'blockchain', code: 'x', retryable: true })).toBeNull()
    expect(parseErrorSurface('provider')).toBeNull()
    expect(parseErrorSurface(null)).toBeNull()
    expect(parseErrorSurface(undefined)).toBeNull()
    expect(parseErrorSurface(7)).toBeNull()
  })

  it('defaults code and retryable when missing', () => {
    expect(parseErrorSurface({ layer: 'gateway' })).toEqual({ layer: 'gateway', code: 'unknown', retryable: true })
  })

  it('honors retryable=false', () => {
    expect(parseErrorSurface({ layer: 'auth', code: 'auth_permanent', retryable: false })?.retryable).toBe(false)
  })

  it('carries the failing session identity when present', () => {
    const surface = parseErrorSurface({
      layer: 'provider',
      code: 'rate_limit',
      retryable: true,
      provider: 'openrouter',
      model: 'test/m1'
    })

    expect(surface?.provider).toBe('openrouter')
    expect(surface?.model).toBe('test/m1')
    // Absent identity yields no keys, not empty strings.
    expect(parseErrorSurface({ layer: 'provider', code: 'x', retryable: true })?.provider).toBeUndefined()
  })
})

describe('formatErrorDiagnostics', () => {
  it('includes layer, code, model and error', () => {
    const text = formatErrorDiagnostics({
      errorText: 'boom',
      model: 'anthropic/claude-opus-4.6',
      surface: { layer: 'provider', code: 'rate_limit', retryable: true }
    })

    expect(text).toContain('layer: provider')
    expect(text).toContain('code: rate_limit')
    expect(text).toContain('model: anthropic/claude-opus-4.6')
    expect(text).toContain('error: boom')
  })

  it('prefers the descriptor identity over the caller fallback', () => {
    const text = formatErrorDiagnostics({
      errorText: 'boom',
      // Foreground composer atom — potentially stale by click time.
      model: 'some/other-model',
      surface: { layer: 'provider', code: 'rate_limit', retryable: true, provider: 'openrouter', model: 'failed/model' }
    })

    expect(text).toContain('provider: openrouter')
    expect(text).toContain('model: failed/model')
    expect(text).not.toContain('some/other-model')
  })

  it('omits absent fields without leaving blank lines', () => {
    const text = formatErrorDiagnostics({ errorText: 'boom' })

    expect(text).not.toContain('layer:')
    expect(text).not.toContain('model:')
    expect(text.split('\n').every(line => line.trim().length > 0)).toBe(true)
  })
})

describe('formatUserFacingError', () => {
  it('replaces deployment cooldown internals with actionable rate-limit copy', () => {
    const raw =
      "HTTP 429: No deployments available for selected model, Try again in 45 seconds. Passed model=glm-5.3. pre-call-checks=True, cooldown_list=['deployment-hash']"

    const text = formatUserFacingError(raw, {
      layer: 'provider',
      code: 'rate_limit',
      retryable: true
    })

    expect(text).toContain('temporarily unavailable')
    expect(text).toContain('45 seconds')
    expect(text).toContain('configured fallback model')
    expect(text).not.toContain('deployment-hash')
    expect(text).not.toContain('pre-call-checks')
    expect(text).not.toContain('cooldown_list')
  })

  it('keeps raw provider text in diagnostics while simplifying the visible billing card', () => {
    const raw = 'HTTP 402: requested 8192 tokens but can only afford 93'
    const surface = { layer: 'billing', code: 'billing', retryable: false } as const

    expect(formatUserFacingError(raw, surface)).toContain('insufficient credits')
    expect(formatErrorDiagnostics({ errorText: raw, surface })).toContain(raw)
  })

  it('strips legacy router internals even without structured metadata', () => {
    const text = formatUserFacingError(
      "Error: HTTP 429: No deployments available. Passed model=glm-5.3. pre-call-checks=True, cooldown_list=['secret-ish-id']"
    )

    expect(text).toBe('No deployments available.')
  })
})
