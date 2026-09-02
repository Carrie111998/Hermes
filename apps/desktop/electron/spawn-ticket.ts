/**
 * Spawn-ticket bootstrap for gated local backends (#93981).
 *
 * A profile with a non-loopback dashboard.public_url engages the dashboard
 * auth gate even when the desktop spawns the backend on loopback. The gate
 * rejects the legacy ?token= WS upgrade, so readiness probing needs a
 * single-use ticket instead. The OAuth ws-ticket route is unreachable at
 * this point (no dashboard session exists yet), so the backend exposes
 * POST /api/auth/spawn-ticket: token-guarded (X-Hermes-Session-Token),
 * listed public to bypass the cookie gate, and minting a normal
 * single-use ticket.
 *
 * Ticket burn rate: every gated-profile spawn probe consumes exactly one
 * single-use ticket per attempt (the readiness probe IS the consumer), so a
 * crash-looping backend burns one ticket per spawn cycle. The store is
 * TTL-GC'd (30s) and uncapped, so this churn is bounded by spawn frequency,
 * not by any store limit.
 */

const SPAWN_TICKET_TIMEOUT_MS = 8_000

export async function mintSpawnTicket(
  baseUrl: string,
  token: string,
  fetchImpl: typeof fetch = fetch
): Promise<string | null> {
  try {
    const res = await fetchImpl(`${baseUrl.replace(/\/+$/, '')}/api/auth/spawn-ticket`, {
      method: 'POST',
      headers: { 'x-hermes-session-token': token },
      signal: AbortSignal.timeout(SPAWN_TICKET_TIMEOUT_MS)
    })

    if (!res.ok) {
      return null
    }

    const body = (await res.json()) as { ticket?: unknown }

    return typeof body?.ticket === 'string' && body.ticket ? body.ticket : null
  } catch {
    return null
  }
}
