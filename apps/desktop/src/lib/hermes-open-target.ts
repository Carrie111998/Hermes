/**
 * Shared resolver for in-app navigation targets that must stay compatible with
 * `hermes://open/<path>?…` deep links and `host.navigate('/path?…')`.
 *
 * Notification activation, deep-link delivery, and plugin `activate` payloads
 * all funnel through here so a toast click and an OS deep link land on the
 * same hash-router path.
 */

export type HermesOpenTarget =
  | string
  | { href: string }
  | { path: string; params?: Record<string, string> }

const HERMES_PROTOCOL = 'hermes:'

function appendSearch(path: string, params: URLSearchParams | Record<string, string> | undefined): string {
  if (!params) {
    return path
  }

  const search =
    params instanceof URLSearchParams
      ? params
      : new URLSearchParams(Object.entries(params).filter(([, v]) => v != null && v !== ''))

  const qs = search.toString()

  if (!qs) {
    return path
  }

  return path.includes('?') ? `${path}&${qs}` : `${path}?${qs}`
}

function isSafeAppPath(path: string): boolean {
  if (!path.startsWith('/') || path.startsWith('//')) {
    return false
  }

  // Block traversal and scheme smuggling in the path segment.
  if (path.includes('..') || path.includes('\\') || path.includes(':')) {
    return false
  }

  return true
}

/** Normalize a string target to a hash-router path (`/kanban?task=x`) or null. */
export function normalizeHermesOpenString(raw: string): string | null {
  const trimmed = raw.trim()

  if (!trimmed) {
    return null
  }

  if (trimmed.startsWith('hermes://') || trimmed.startsWith(`${HERMES_PROTOCOL}//`)) {
    try {
      const url = new URL(trimmed)

      // Only the `open` kind maps to in-app navigation. Other kinds (blueprint,
      // install, …) have their own handlers.
      if (url.hostname !== 'open') {
        return null
      }

      const name = decodeURIComponent((url.pathname || '').replace(/^\//, ''))

      if (!name) {
        return null
      }

      const path = `/${name}`

      if (!isSafeAppPath(path.split('?')[0] ?? path)) {
        return null
      }

      return appendSearch(path, url.searchParams)
    } catch {
      return null
    }
  }

  const path = trimmed.startsWith('#') ? trimmed.slice(1) : trimmed

  if (!isSafeAppPath(path.split('?')[0] ?? path)) {
    return null
  }

  return path
}

/** Resolve any supported activate/open target to a hash-router path, or null. */
export function resolveHermesOpenPath(target: HermesOpenTarget | null | undefined): string | null {
  if (target == null) {
    return null
  }

  if (typeof target === 'string') {
    return normalizeHermesOpenString(target)
  }

  if (typeof target !== 'object') {
    return null
  }

  if ('href' in target && typeof target.href === 'string') {
    return normalizeHermesOpenString(target.href)
  }

  if ('path' in target && typeof target.path === 'string') {
    const base = normalizeHermesOpenString(target.path)

    if (!base) {
      return null
    }

    return appendSearch(base, target.params)
  }

  return null
}

/** Build a path from a parsed `hermes://open/<name>?…` deep-link payload. */
export function pathFromOpenDeepLink(name: string, params: Record<string, string> = {}): string | null {
  if (!name) {
    return null
  }

  return resolveHermesOpenPath({ path: `/${name.replace(/^\//, '')}`, params })
}
