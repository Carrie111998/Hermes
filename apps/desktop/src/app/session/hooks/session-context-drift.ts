import { parseSessionIdentityKey, sessionIdentityKey } from '@/lib/session-identity'

import { isNewChatRoute, routeSessionId, routeSessionProfile } from '../../routes'

/**
 * The chat a route token points at: the stored/routed session id, `'__new__'`
 * for the new-chat route, or null for a route that isn't a chat (settings and
 * the other overlay routes). Used to compare two route tokens by their *chat*
 * rather than their raw string.
 */
export type RouteTarget = string | null

function routeTokenParts(token: string): { pathname: string; search: string } {
  const firstSeparator = token.indexOf(':')

  if (firstSeparator === -1) {
    return { pathname: token, search: '' }
  }

  const secondSeparator = token.indexOf(':', firstSeparator + 1)

  return {
    pathname: token.slice(0, firstSeparator),
    search: secondSeparator === -1 ? token.slice(firstSeparator + 1) : token.slice(firstSeparator + 1, secondSeparator)
  }
}

/**
 * Reduce a route token to the chat it targets. The token is
 * `${pathname}:${search}:${hash}` (desktop-controller's routeToken), and only
 * the pathname selects the chat — search/hash carry overlay/panel state, so a
 * change there must not read as a session switch. We take the substring before
 * the first ':' as the pathname; that is safe because `location.pathname` never
 * contains a raw ':' (sessionRoute encodeURIComponent's the id, so a ':' in an
 * id arrives as %3A, and the app's other routes are literal colon-free paths).
 */
export function routeTargetFromToken(token: string): RouteTarget {
  const { pathname } = routeTokenParts(token)

  return routeSessionId(pathname) ?? (isNewChatRoute(pathname) ? '__new__' : null)
}

function routeIdentityFromToken(token: string, fallbackProfile?: null | string): RouteTarget {
  const { pathname, search } = routeTokenParts(token)
  const storedSessionId = routeSessionId(pathname)

  if (storedSessionId) {
    return sessionIdentityKey(storedSessionId, routeSessionProfile(search) ?? fallbackProfile)
  }

  return isNewChatRoute(pathname) ? '__new__' : null
}

function selectionIdentity(storedSessionId: string | null, profile?: null | string): string | null {
  return storedSessionId === null ? null : sessionIdentityKey(storedSessionId, profile)
}

function describeTarget(target: RouteTarget): string {
  if (!target || target === '__new__') {
    return String(target)
  }

  const { profile, storedSessionId } = parseSessionIdentityKey(target)

  return profile === 'default' ? storedSessionId : `${profile}/${storedSessionId}`
}

interface SessionContextDriftArgs {
  startRouteToken: string
  nowRouteToken: string
  startSelectedStoredId: string | null
  nowSelectedStoredId: string | null
  startSelectedProfile?: null | string
  nowSelectedProfile?: null | string
  /**
   * The stored session this submit is bound to, when known. Drift ignores a
   * move *to* this id: the submit pipeline itself re-homes selection and route
   * onto its target (a fresh create, a resume), and that self-inflicted move is
   * not a user switch. Omit it (pre-create new-chat draft) to treat any move to
   * a real chat as drift.
   */
  submitTargetStoredId?: string | null
  submitTargetProfile?: null | string
  /**
   * The compound durable identity that the composer had loaded when the text
   * was submitted (SubmitTextOptions.composerScope). The composer and the
   * session-side refs live in separate React subtrees and can each be internally
   * consistent yet still disagree at send time — this catches that drift
   * (#59305). Omit for non-composer submits.
   */
  composerScope?: string | null
  /**
   * resolveComposerSessionKey(submitTargetStoredId, sessions, profile) — the
   * compound durable lineage-root identity of the submit target, in the SAME
   * domain as composerScope. Compared against composerScope instead of the raw
   * submitTargetStoredId: the composer keys drafts/attachments on the lineage
   * root (stable across auto-compression tip rotation) while
   * submitTargetStoredId tracks the live tip — comparing composerScope
   * directly against the tip would false-positive-abort every submit into any
   * session that has ever compressed.
   */
  submitTargetComposerScope?: string | null
}

/**
 * Decide whether the session context genuinely changed under an in-flight
 * submit — the user (or a real navigation) moved to a DIFFERENT chat — as
 * opposed to the programmatic churn a busy gateway produces constantly:
 *   - selection null-resets on a gateway/profile switch or reconnect
 *     (gateway-switch's `setSelectedStoredSessionId(null)`),
 *   - search/hash-only route changes from overlays and side panels,
 *   - background gateway events retargeting the active runtime id (#47709 class,
 *     which is why the active ref is not a prong here at all).
 * Returns null when nothing genuinely drifted, or a short reason string
 * (`route:<from>-><to>` / `selection:<from>-><to>`) for the abort log.
 */
export function sessionContextDrift({
  startRouteToken,
  nowRouteToken,
  startSelectedStoredId,
  nowSelectedStoredId,
  startSelectedProfile,
  nowSelectedProfile,
  submitTargetStoredId,
  submitTargetProfile,
  composerScope,
  submitTargetComposerScope
}: SessionContextDriftArgs): string | null {
  // Composer prong: the composer's loaded scope disagrees with the resolved
  // submit target. Not a start/now comparison like the two prongs below — the
  // composer only hands us one snapshot per submit — but it belongs in the
  // same fail-closed gate since it's exactly the same "wrong session" failure
  // mode. Compared against submitTargetComposerScope (lineage-pinned), NOT
  // submitTargetStoredId (live tip) — see the field doc on
  // SessionContextDriftArgs for why those two must not be conflated.
  if (composerScope !== undefined && composerScope !== null && composerScope !== submitTargetComposerScope) {
    return `composer:${describeTarget(composerScope)}->${describeTarget(submitTargetComposerScope ?? null)}`
  }

  const targetStart = routeIdentityFromToken(startRouteToken, startSelectedProfile)
  const targetNow = routeIdentityFromToken(nowRouteToken, nowSelectedProfile)
  const submitTarget = selectionIdentity(submitTargetStoredId ?? null, submitTargetProfile)

  // Route prong: the routed chat moved to a different, real chat. A null target
  // (navigated to settings / a non-chat overlay route) or a search/hash-only
  // change (same target) is not drift, and neither is landing on the submit's
  // own target.
  if (targetNow !== targetStart && targetNow !== null && targetNow !== submitTarget) {
    return `route:${describeTarget(targetStart)}->${describeTarget(targetNow)}`
  }

  // Selection prong: selection moved to a different, real stored session. A
  // null-reset (nowSelectedStoredId === null) or a move onto the submit's own
  // target is not drift.
  const selectedStart = selectionIdentity(startSelectedStoredId, startSelectedProfile)
  const selectedNow = selectionIdentity(nowSelectedStoredId, nowSelectedProfile)

  if (selectedNow !== null && selectedNow !== selectedStart && selectedNow !== submitTarget) {
    return `selection:${describeTarget(selectedStart)}->${describeTarget(selectedNow)}`
  }

  return null
}
