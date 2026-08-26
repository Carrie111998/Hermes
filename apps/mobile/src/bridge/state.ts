/**
 * state.ts — live connection target + auth status.
 *
 * The active gateway target (base URL + auth mode) lives here as both a plain
 * singleton (for non-React bridge code like http.ts) and nanostores atoms (for
 * React screens). Persisted to @capacitor/preferences so relaunch restores the
 * last gateway without re-entering the URL.
 */

import { atom } from 'nanostores'

import { requireSecureRemoteBaseUrl, type AuthMode } from './connection-config'
import { secureGet, secureRemove, secureSet } from './secure-store'

export interface GatewayTarget {
  baseUrl: string
  authMode: AuthMode
  /** Auth provider name (e.g. "basic") when password-gated; null for token mode. */
  provider: string | null
  /** Static session token for `authMode: 'token'` gateways (sent as the
   *  `X-Hermes-Session-Token` REST header and the ws `?token=`); null/absent for
   *  oauth, which authenticates via session cookies + a minted ws-ticket. */
  token?: string | null
}

export type AuthStatus =
  | 'unknown' // haven't probed yet
  | 'probing' // GET /api/status in flight
  | 'needs-login' // gated gateway, no live session
  | 'authed' // have a session cookie / token
  | 'error'

export const $target = atom<GatewayTarget | null>(null)
export const $authStatus = atom<AuthStatus>('unknown')
/** Bumped whenever a request 401s in a way that demands re-login. Screens watch
 *  this to bounce back to the login form. */
export const $reauthNonce = atom(0)

let _target: GatewayTarget | null = null

export function currentTarget(): GatewayTarget | null {
  return _target
}

const KEY = 'hermes.target'

export async function loadTarget(): Promise<GatewayTarget | null> {
  try {
    const value = await secureGet(KEY)
    if (value) {
      const restored = JSON.parse(value) as GatewayTarget
      _target = { ...restored, baseUrl: requireSecureRemoteBaseUrl(restored.baseUrl) }
      $target.set(_target)
    }
  } catch {
    _target = null
    $target.set(null)
    await secureRemove(KEY).catch(() => undefined)
  }

  return _target
}

function normalizeTarget(t: GatewayTarget | null): GatewayTarget | null {
  return t ? { ...t, baseUrl: requireSecureRemoteBaseUrl(t.baseUrl) } : null
}

/** Connect for this app process only when Android Keystore persistence fails.
 * The token stays solely in memory and is discarded on process exit. */
export function setTransientTarget(t: GatewayTarget): void {
  _target = normalizeTarget(t)
  $target.set(_target)
}

export async function setTarget(t: GatewayTarget | null): Promise<void> {
  const secureTarget = normalizeTarget(t)
  // Do not update the live target until the Keystore write has succeeded. A
  // failed persistence attempt must not leave a half-connected secret behind.
  if (secureTarget) await secureSet(KEY, JSON.stringify(secureTarget))
  else await secureRemove(KEY)
  _target = secureTarget
  $target.set(secureTarget)
}

export function setAuthStatus(s: AuthStatus): void {
  $authStatus.set(s)
}

export function requireReauth(): void {
  $authStatus.set('needs-login')
  $reauthNonce.set($reauthNonce.get() + 1)
}
