import type { DesktopConnectionProbeResult } from '@/global'
import { getDashboardHandoffTicket, getDashboardRemoteAccess } from '@/hermes'

export type ContinueOnPhoneFailureReason =
  | 'browser-auth-not-supported'
  | 'handoff-failed'
  | 'insecure-url'
  | 'not-configured'
  | 'unreachable'

export type ContinueOnPhoneResult =
  | { expiresAt: number; ok: true; url: string }
  | { ok: false; reason: ContinueOnPhoneFailureReason }

export interface ContinueOnPhoneDependencies {
  getRemoteAccess: (profile?: string) => ReturnType<typeof getDashboardRemoteAccess>
  mintHandoffTicket: (
    sessionId: string,
    profile?: string
  ) => ReturnType<typeof getDashboardHandoffTicket>
  now: () => number
  probe: (publicUrl: string) => Promise<Pick<DesktopConnectionProbeResult, 'authMode' | 'reachable'>>
}

const DEFAULT_DEPENDENCIES: ContinueOnPhoneDependencies = {
  getRemoteAccess: getDashboardRemoteAccess,
  mintHandoffTicket: getDashboardHandoffTicket,
  now: () => Date.now(),
  probe: publicUrl => window.hermesDesktop.probeConnectionConfig(publicUrl)
}

const MAX_HANDOFF_TTL_SECONDS = 5 * 60

function handoffExpiresAt(ttlSeconds: number, now: number): number | null {
  if (
    !Number.isSafeInteger(ttlSeconds) ||
    ttlSeconds <= 0 ||
    ttlSeconds > MAX_HANDOFF_TTL_SECONDS ||
    !Number.isSafeInteger(now)
  ) {
    return null
  }

  const ttlMilliseconds = ttlSeconds * 1_000
  const expiresAt = now + ttlMilliseconds

  return Number.isSafeInteger(ttlMilliseconds) && Number.isSafeInteger(expiresAt) ? expiresAt : null
}

/** The connection probe reports gated dashboards as OAuth mode. */
export function isPhoneHandoffAuthMode(authMode: DesktopConnectionProbeResult['authMode']): boolean {
  return authMode === 'oauth'
}

export function buildDashboardSessionUrl(
  publicUrl: string,
  sessionId: string,
  profile?: string,
  handoffTicket?: string
): string | null {
  const cleanSessionId = sessionId.trim()
  const cleanHandoff = (handoffTicket || '').trim()

  if (!cleanSessionId) {
    return null
  }

  let url: URL

  try {
    url = new URL(publicUrl)
  } catch {
    return null
  }

  if (url.protocol !== 'https:' || url.username || url.password) {
    return null
  }

  const basePath = url.pathname.replace(/\/+$/, '')
  url.search = ''
  url.hash = ''

  if (cleanHandoff) {
    // Fragments are not sent in HTTP request lines or Referer headers. The
    // public handoff bootstrap exchanges this one-time ticket in a same-origin
    // POST body, then removes it from browser history before opening chat.
    url.pathname = `${basePath}/handoff`
    url.hash = new URLSearchParams({ ticket: cleanHandoff }).toString()

    return url.toString()
  }

  url.pathname = `${basePath}/chat`
  url.searchParams.set('resume', cleanSessionId)

  if (profile?.trim()) {
    url.searchParams.set('profile', profile.trim())
  }

  return url.toString()
}

export async function resolveContinueOnPhoneUrl(
  sessionId: string,
  profile?: string,
  dependencies: ContinueOnPhoneDependencies = DEFAULT_DEPENDENCIES
): Promise<ContinueOnPhoneResult> {
  const { public_url: publicUrl } = await dependencies.getRemoteAccess(profile)

  if (!publicUrl) {
    return { ok: false, reason: 'not-configured' }
  }

  // Validate base URL shape before minting (no credentials, HTTPS only).
  const probeUrl = buildDashboardSessionUrl(publicUrl, sessionId, profile)

  if (!probeUrl) {
    return { ok: false, reason: 'insecure-url' }
  }

  const probe = await dependencies.probe(publicUrl)

  if (!probe.reachable) {
    return { ok: false, reason: 'unreachable' }
  }

  // Token-proxy topologies have no browser sign-in page. The probe reports
  // every auth_required dashboard as OAuth mode, including handoff-capable
  // deployments.
  if (!isPhoneHandoffAuthMode(probe.authMode)) {
    return { ok: false, reason: 'browser-auth-not-supported' }
  }

  let ticket: string
  let ttlSeconds: number

  try {
    const minted = await dependencies.mintHandoffTicket(sessionId, profile)
    ticket = (minted.ticket || '').trim()
    ttlSeconds = minted.ttl_seconds

    if (!ticket) {
      return { ok: false, reason: 'handoff-failed' }
    }
  } catch {
    return { ok: false, reason: 'handoff-failed' }
  }

  const url = buildDashboardSessionUrl(publicUrl, sessionId, profile, ticket)
  const expiresAt = handoffExpiresAt(ttlSeconds, dependencies.now())

  if (!url || !expiresAt) {
    return { ok: false, reason: 'handoff-failed' }
  }

  return { expiresAt, ok: true, url }
}
