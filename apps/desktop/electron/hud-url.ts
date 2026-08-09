import { pathToFileURL } from 'node:url'

/**
 * Build the renderer URL for HUD mode.
 *
 * Same query-before-hash contract as `buildSessionWindowUrl`: `?win=hud` (and
 * optional `profile=`) must sit in the search string before `#`, or HashRouter
 * swallows them as the route. The profile is what the HUD renderer adopts on
 * boot so a non-primary conversation lands on the right backend — session id
 * alone is not enough across profiles (see #82285).
 */
export function buildHudWindowUrl(
  sessionId: null | string | undefined,
  {
    devServer,
    profile,
    rendererIndexPath
  }: {
    devServer?: null | string
    profile?: null | string
    rendererIndexPath?: string
  } = {}
): string {
  const profileKey = typeof profile === 'string' ? profile.trim() : ''
  const query = profileKey
    ? `?win=hud&profile=${encodeURIComponent(profileKey)}`
    : '?win=hud'
  const route = sessionId ? `#/${encodeURIComponent(sessionId)}` : '#/'

  if (devServer) {
    const base = devServer.endsWith('/') ? devServer.slice(0, -1) : devServer

    return `${base}/${query}${route}`
  }

  return `${pathToFileURL(rendererIndexPath!).toString()}${query}${route}`
}
