import { requestGatewayForProfile } from '@/store/gateway'
import { normalizeProfileKey } from '@/store/profile'

/** A recovering gateway requester shape (method, params, timeout, signal). */
export type PetGatewayRequest = <T>(
  method: string,
  params?: Record<string, unknown>,
  timeoutMs?: number,
  signal?: AbortSignal
) => Promise<T>

/**
 * Profile-addressed pet RPC transport (Layer 7). Pets are per-profile, so gallery
 * calls, scale persistence, and pet config reads/writes all route through the
 * named profile's OWN socket via requestGatewayForProfile — connection ownership
 * is resolved by Electron, never guessed in the renderer, and never the active
 * gateway. Gallery calls take NO lease (only rendering slots/submits lease).
 */
export function petRequestFor(profile: string): PetGatewayRequest {
  const key = normalizeProfileKey(profile)

  return (method, params, timeoutMs, signal) =>
    requestGatewayForProfile(key, method, { ...params, profile: key }, timeoutMs, signal)
}
