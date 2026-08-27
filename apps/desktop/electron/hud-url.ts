// HUD mode's renderer URL. The pure, Electron-free piece lives here so it can
// be unit-tested (same split as session-windows.ts, and for the same reason:
// the contract below is invisible until it breaks at runtime).

import { pathToFileURL } from 'node:url'

import { normalizeSessionWindowOwnerRoute } from './session-windows'

/**
 * Build the renderer URL for the HUD window.
 *
 * Same query-before-hash contract as `buildSessionWindowUrl`: `?win=hud` and
 * optional `profile=` / `newChatGeneration=` MUST sit in the search string before the '#', or
 * HashRouter swallows them as part of the route.
 *
 * The profile is what the HUD renderer adopts at boot. Session ids are scoped
 * per profile, so without it a HUD opened on a non-primary conversation
 * resolves the id against the primary backend, misses, and falls back to that
 * backend's last session (#82285). Absent/blank means no override — ordinary
 * single-profile boots are unchanged.
 */
export function buildHudWindowUrl(
  sessionId: null | string | undefined,
  {
    devServer,
    newChatGeneration,
    ownerRoute,
    profile,
    rendererIndexPath
  }: {
    devServer?: null | string
    newChatGeneration?: null | number | string
    ownerRoute?: unknown
    profile?: null | string
    rendererIndexPath?: string
  } = {}
): string {
  const owner = normalizeSessionWindowOwnerRoute(ownerRoute)
  const profileKey = owner?.profile || (typeof profile === 'string' ? profile.trim() : '')
  const generation = newChatGeneration == null ? '' : String(newChatGeneration).trim()
  const query: string[] = ['win=hud']

  if (owner) {
    query.push(`connection=${encodeURIComponent(owner.connectionId)}`)
  }

  if (profileKey) {
    query.push(`profile=${encodeURIComponent(profileKey)}`)
  }

  if (owner?.targetProfile) {
    query.push(`targetProfile=${encodeURIComponent(owner.targetProfile)}`)
  }

  if (owner?.mode) {
    query.push(`mode=${owner.mode}`)
  }

  if (generation) {
    query.push(`newChatGeneration=${encodeURIComponent(generation)}`)
  }

  const queryString = `?${query.join('&')}`
  const route = sessionId ? `#/${encodeURIComponent(sessionId)}` : '#/'

  if (devServer) {
    const base = devServer.endsWith('/') ? devServer.slice(0, -1) : devServer

    return `${base}/${queryString}${route}`
  }

  return `${pathToFileURL(rendererIndexPath!).toString()}${queryString}${route}`
}
