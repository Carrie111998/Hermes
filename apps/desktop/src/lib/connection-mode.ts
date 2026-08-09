/**
 * The resolved Desktop connection mode — the one connection fact extensions are
 * allowed to see (NousResearch/hermes-agent#82140).
 *
 * `local`  — this Desktop drives its own local backend, so a path the agent
 *            reports is already a path on the machine the user is looking at.
 * `remote` — this Desktop drives an SSH/URL/cloud backend, so a gateway-side
 *            path has to be transferred before the Desktop can open it.
 *
 * That distinction is the whole point: without it an extension can't tell
 * whether `/home/user/report.md` is openable here, which is what makes `MEDIA:`
 * and file-link handling ambiguous on remote gateways.
 *
 * Everything else on the connection descriptor — base URL, host, identity,
 * tokens, auth mode — stays behind the Electron bridge. Extensions get the
 * shape of the connection, never the credentials for it.
 */

import type { HermesConnection } from '@/global'

export type HermesConnectionMode = 'local' | 'remote'

/** RPCs on which the renderer announces its live mode to the backend. Session
 *  lifecycle pins it for new/resumed chats; `prompt.submit` re-announces every
 *  turn so switching the active connection or profile lands immediately rather
 *  than being stuck at whatever was true when the chat opened. */
const CONNECTION_MODE_METHODS = new Set(['prompt.submit', 'session.create', 'session.resume'])

/**
 * Narrow a connection descriptor to its mode.
 *
 * The descriptor's `mode` is already resolved (a `cloud` saved config resolves
 * to a `remote` connection), so this only has to guard the "no descriptor yet"
 * and "older shell that predates the field" cases — both of which resolve to
 * null. Null means "unknown", never "local": telling an extension a remote file
 * is local hands the user a link to a file that isn't on their machine.
 */
export function resolveConnectionMode(connection: HermesConnection | null | undefined): HermesConnectionMode | null {
  const mode = connection?.mode

  return mode === 'local' || mode === 'remote' ? mode : null
}

/**
 * Stamp `connection_mode` onto the params of an RPC that carries it.
 *
 * Applied at the single `requestGateway` choke point rather than at each of the
 * ~10 call sites, so a new session/prompt path announces correctly by
 * construction instead of by remembering to.
 *
 * An explicit param already on `params` wins (nothing sets one today; this
 * keeps the helper from silently overriding a deliberate caller). An unknown
 * mode adds no key at all, which leaves any previously-announced value intact
 * on the backend rather than clearing it during a reconnect window.
 */
export function withConnectionMode(
  method: string,
  params: Record<string, unknown>,
  mode: HermesConnectionMode | null
): Record<string, unknown> {
  if (!mode || !CONNECTION_MODE_METHODS.has(method) || 'connection_mode' in params) {
    return params
  }

  return { ...params, connection_mode: mode }
}
