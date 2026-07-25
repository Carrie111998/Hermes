/**
 * native-auth-decisions.ts
 *
 * Pure decision helpers extracted from main.ts for the RFC 8252 native-app
 * auth flow. These encode three choices that were each the site of a real
 * runtime bug — invisible to the mocked flow tests because the tests never
 * exercised the real main.ts internals. Keeping them pure + unit-tested here
 * prevents silent regressions:
 *
 *   1. resolveJsonBody      — the token/refresh POST body must be the raw
 *      object (fetchJson owns JSON.stringify). Pre-stringifying double-encodes
 *      it into a JSON string, which the gateway's Pydantic model rejects with
 *      422 "Input should be a valid dictionary".
 *
 *   2. oauthSessionIsLive   — an OAuth gateway is "signed in" when EITHER a
 *      native bearer token OR a live cookie session exists. Gating on the
 *      cookie alone rejects a completed native login and loops the UI into
 *      "not signed in".
 *
 *   3. resolveOauthRestAuth — an oauth-mode REST call authenticates with the
 *      native bearer when present, else the cookie partition. Cookie-only
 *      routing returns 401 no_cookie for a cookieless native session.
 *
 * All three are trivial once named; the value is the test that pins the
 * contract so the god-file call sites can't drift back to the buggy shape.
 */

/**
 * Decide the request body to hand to fetchJson (which JSON.stringifies it).
 * Returns the object UNCHANGED — callers must NOT pre-stringify. A string here
 * would be double-encoded downstream; this function exists to document and
 * pin that contract at the one seam that got it wrong.
 */
export function resolveJsonBody<T>(body: T): T {
  return body
}

/**
 * True when an oauth gateway should be treated as signed-in. `hasNativeToken`
 * is whether a native bearer token is stored; `hasCookieSession` is whether a
 * live AT-or-RT cookie exists in the OAuth partition. Either suffices.
 */
export function oauthSessionIsLive(hasNativeToken: boolean, hasCookieSession: boolean): boolean {
  return hasNativeToken || hasCookieSession
}

/** Shape of one entry in a gateway's advertised provider list. */
export type GatewayAuthProvider = { name?: string; supportsPassword?: boolean }

/**
 * True when the pre-flight sign-in guard may hard-fail the boot.
 *
 * `authModeFromStatus` maps the gateway's `auth_required` flag onto 'oauth'
 * whenever the gate is on — but `auth_required` only says "this gateway is
 * gated", NOT "this gateway speaks OAuth". A gateway running the bundled
 * password provider is gated, reports auth_required, and is therefore driven
 * down the 'oauth' branch, yet it never mints a native bearer and its session
 * cookies are set by the plain /auth/password-login POST rather than the
 * /auth/callback redirect the OAuth partition is primed for. The cookie probe
 * then reads empty and the guard throws "uses OAuth, but you are not signed
 * in" — even though mintGatewayWsTicket would succeed against that very
 * partition. (BasicAuthProvider.start_login raises NotImplementedError by
 * design, and /auth/native/authorize rejects password providers with 400, so
 * neither OAuth liveness signal can EVER be true for such a gateway.)
 *
 * Gateways offering a password login therefore skip the pre-flight early-out
 * and let the ws-ticket mint be the authoritative liveness check — exactly as
 * the OAuth path's own comment already describes. A genuinely signed-out
 * gateway still fails, just one step later at the mint, where the message is
 * derived from a real 401 instead of a guess about cookie shape.
 *
 * ANY advertised password provider is enough to relax the guard, not only an
 * all-password list: `supports_password` is documented as password login
 * "rather than (or IN ADDITION TO) the OAuth redirect flow", so a mixed
 * deployment (e.g. `basic` + `nous`) can hold a perfectly valid session that
 * was established through the password leg — and that session satisfies
 * neither OAuth liveness signal either. Gating on `every` would hard-fail
 * exactly those users. This does not weaken authentication: the mint is still
 * the check, and a genuinely signed-out client gets a real 401 from it.
 *
 * Identifying the provider that actually backs the current session would be
 * more precise, but is not reachable here: `/api/auth/me` reports
 * `provider: "basic"` yet is itself behind the auth gate (401 without a
 * cookie), which is precisely the state this guard runs in.
 *
 * Providers absent/empty (older backends that don't publish the list, or a
 * fetch failure) keep the strict guard — the pre-existing behaviour.
 */
export function oauthGuardMayHardFail(providers: readonly GatewayAuthProvider[] | null | undefined): boolean {
  if (!Array.isArray(providers) || providers.length === 0) {
    return true
  }

  return !providers.some(p => p && p.supportsPassword === true)
}

export type OauthRestAuth = { kind: 'bearer'; token: string } | { kind: 'cookie' }

/**
 * Decide how an oauth-mode REST request authenticates: prefer the native
 * bearer (cookieless RFC 8252 flow) when a non-empty access token is present,
 * otherwise fall back to the cookie partition. `nativeAccessToken` is the
 * result of ensureNativeAccessToken (null/empty when there is no native
 * session or the refresh terminally failed).
 */
export function resolveOauthRestAuth(nativeAccessToken: string | null | undefined): OauthRestAuth {
  if (nativeAccessToken) {
    return { kind: 'bearer', token: nativeAccessToken }
  }

  return { kind: 'cookie' }
}
