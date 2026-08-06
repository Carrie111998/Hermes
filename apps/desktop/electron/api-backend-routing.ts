export const LOCAL_BACKEND_REQUIRED_ERROR = 'This request requires a local Hermes backend'

export interface LocalitySensitiveRequest {
  /** Set by callers whose payload must never leave the machine (live voice
   *  preview transcription). Forces the request onto a local backend and fails
   *  closed if one cannot be used. */
  requireLocalBackend?: boolean
  profile?: string
}

/** Only `mode` is inspected; callers pass their full connection descriptor. */
export interface LocalityCheckedConnection {
  mode?: string
}

/**
 * Resolve the backend for a request that demands locality, bypassing the
 * remote-interception path entirely. Returns null for ordinary requests so the
 * caller keeps its normal routing.
 */
export async function resolveLocalitySensitiveBackend<TConnection>(
  request: LocalitySensitiveRequest | null | undefined,
  ensureBackend: (profile?: string) => Promise<TConnection>
): Promise<TConnection | null> {
  if (!request?.requireLocalBackend) {
    return null
  }

  return ensureBackend(request?.profile)
}

/**
 * Fail closed if a locality-sensitive request ended up on a non-local backend.
 * Called immediately before transmission — after any config change that could
 * have swapped the descriptor out from under the earlier resolution.
 */
export function assertLocalBackendForRequest<TConnection extends LocalityCheckedConnection>(
  request: LocalitySensitiveRequest | null | undefined,
  connection: TConnection | null | undefined
): void {
  if (request?.requireLocalBackend && connection?.mode !== 'local') {
    throw new Error(LOCAL_BACKEND_REQUIRED_ERROR)
  }
}
