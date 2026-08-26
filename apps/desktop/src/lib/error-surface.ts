// Structured turn-error descriptor forwarded by the gateway (see
// agent/error_surface.py). Names WHICH layer of the stack failed so the error
// card can say "Provider error" / "Gateway error" and offer layer-appropriate
// recovery actions, instead of toasting an opaque string.
//
// Advisory contract: older backends never send this — every consumer must
// keep working when it is absent (legacy string-sniffing stays as fallback).

export const ERROR_SURFACE_LAYERS = [
  'provider',
  'endpoint',
  'streaming',
  'auth',
  'billing',
  'gateway',
  'runtime',
  'disk'
] as const

export type ErrorSurfaceLayer = (typeof ERROR_SURFACE_LAYERS)[number]

export interface ErrorSurface {
  layer: ErrorSurfaceLayer
  /** Specific failure code (a FailoverReason value or site-specific code). */
  code: string
  /** False when retrying unchanged reproduces the same failure. */
  retryable: boolean
  /** The failing session's provider/model, captured at classification time —
   *  preferred over the foreground composer's atoms, which can point at a
   *  different model by the time the user clicks an action. */
  provider?: string
  model?: string
}

/** Validate a wire payload into an ErrorSurface, or null when absent/garbled. */
export function parseErrorSurface(value: unknown): ErrorSurface | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const raw = value as { code?: unknown; layer?: unknown; model?: unknown; provider?: unknown; retryable?: unknown }
  const layer = typeof raw.layer === 'string' ? (raw.layer as ErrorSurfaceLayer) : null

  if (!layer || !ERROR_SURFACE_LAYERS.includes(layer)) {
    return null
  }

  return {
    layer,
    code: typeof raw.code === 'string' && raw.code ? raw.code : 'unknown',
    retryable: raw.retryable !== false,
    ...(typeof raw.provider === 'string' && raw.provider ? { provider: raw.provider } : {}),
    ...(typeof raw.model === 'string' && raw.model ? { model: raw.model } : {})
  }
}

/**
 * Concise copy for the visible error card.
 *
 * Provider payloads are intentionally retained verbatim by
 * formatErrorDiagnostics(), but the transcript should not expose router
 * internals such as deployment hashes, pre-call flags, or cooldown arrays.
 * Prefer the structured surface taxonomy and keep the raw string behind
 * "Copy error details" for diagnosis.
 */
export function formatUserFacingError(errorText: string, surface?: ErrorSurface | null): string {
  const raw = errorText.trim()
  const retryAfter = raw.match(/try again in\s+(\d+)\s+seconds?/i)?.[1]
  const wait = retryAfter ? ` Retry is available in ${retryAfter} seconds.` : ''

  if (surface?.layer === 'billing') {
    return 'This provider has insufficient credits for the request. Hermes can continue on a configured fallback provider.'
  }

  if (surface?.layer === 'auth') {
    return 'This provider rejected its credential. Reconnect it in Settings or continue on a configured fallback provider.'
  }

  if (surface?.code === 'rate_limit' || surface?.code === 'upstream_rate_limit') {
    return `The selected model is temporarily unavailable.${wait} Hermes can continue on a configured fallback model.`
  }

  if (surface?.layer === 'streaming') {
    return `The provider connection ended before the reply completed.${wait}`
  }

  if (surface?.layer === 'endpoint') {
    return 'Hermes could not reach the configured model endpoint. Check the endpoint or continue on a configured fallback provider.'
  }

  // Older backends may not send a structured descriptor. Keep their useful
  // provider prose while stripping only known router implementation details.
  const cleaned = raw
    .replace(/^Error:\s*/i, '')
    .replace(/^HTTP\s+\d{3}:\s*/i, '')
    .split(/\s+Passed model=/i, 1)[0]
    .trim()

  return cleaned || 'The model request failed. Open the error details for diagnostics.'
}

/** Plain-text error-details blob for the error card's "Copy error details". */
export function formatErrorDiagnostics(input: {
  appVersion?: string
  errorText: string
  model?: string
  provider?: string
  surface?: ErrorSurface | null
}): string {
  // The descriptor's identity (captured when the turn failed) beats the
  // caller-supplied fallback (typically the foreground composer's atoms).
  const provider = input.surface?.provider || input.provider
  const model = input.surface?.model || input.model

  const lines = [
    '── Hermes error details ──',
    `time: ${new Date().toISOString()}`,
    input.surface ? `layer: ${input.surface.layer}` : null,
    input.surface ? `code: ${input.surface.code}` : null,
    input.surface ? `retryable: ${input.surface.retryable}` : null,
    provider ? `provider: ${provider}` : null,
    model ? `model: ${model}` : null,
    input.appVersion ? `app: ${input.appVersion}` : null,
    `error: ${input.errorText}`
  ]

  return lines.filter((line): line is string => Boolean(line)).join('\n')
}
