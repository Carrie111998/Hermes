import type { DesktopConnectionProbeResult } from '@/global'
import { getDashboardHandoffTicket, getDashboardRemoteAccess } from '@/hermes'

export type ContinueOnPhoneFailureReason =
  | 'auth-required'
  | 'handoff-failed'
  | 'insecure-url'
  | 'not-configured'
  | 'unreachable'

export type ContinueOnPhoneResult =
  | { ok: true; url: string }
  | { ok: false; reason: ContinueOnPhoneFailureReason }

export interface ContinueOnPhoneDependencies {
  getRemoteAccess: (profile?: string) => ReturnType<typeof getDashboardRemoteAccess>
  mintHandoffTicket: (
    sessionId: string,
    profile?: string
  ) => ReturnType<typeof getDashboardHandoffTicket>
  probe: (publicUrl: string) => Promise<Pick<DesktopConnectionProbeResult, 'authMode' | 'reachable'>>
}

const DEFAULT_DEPENDENCIES: ContinueOnPhoneDependencies = {
  getRemoteAccess: getDashboardRemoteAccess,
  mintHandoffTicket: getDashboardHandoffTicket,
  probe: publicUrl => window.hermesDesktop.probeConnectionConfig(publicUrl)
}

/** Gated dashboard modes that can complete phone handoff (OAuth or handoff ticket). */
export function isPhoneHandoffAuthMode(
  authMode: DesktopConnectionProbeResult['authMode'] | string | undefined
): boolean {
  return authMode === 'oauth' || authMode === 'handoff'
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

  url.pathname = `${url.pathname.replace(/\/+$/, '')}/chat`
  url.search = ''
  url.hash = ''
  url.searchParams.set('resume', cleanSessionId)

  if (profile?.trim()) {
    url.searchParams.set('profile', profile.trim())
  }

  if (cleanHandoff) {
    url.searchParams.set('handoff', cleanHandoff)
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

  // Token-proxy topologies have no browser sign-in page. Gated modes
  // (oauth browser gate, or handoff-capable auth_required) are accepted.
  if (!isPhoneHandoffAuthMode(probe.authMode)) {
    return { ok: false, reason: 'auth-required' }
  }

  let ticket: string

  try {
    const minted = await dependencies.mintHandoffTicket(sessionId, profile)
    ticket = (minted.ticket || '').trim()

    if (!ticket) {
      return { ok: false, reason: 'handoff-failed' }
    }
  } catch {
    return { ok: false, reason: 'handoff-failed' }
  }

  const url = buildDashboardSessionUrl(publicUrl, sessionId, profile, ticket)

  if (!url) {
    return { ok: false, reason: 'insecure-url' }
  }

  return { ok: true, url }
}
