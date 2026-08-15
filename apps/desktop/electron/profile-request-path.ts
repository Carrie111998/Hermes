const STATIC_PROFILE_RESOURCES = new Set(['active', 'import', 'projects', 'sessions'])

export function requestPathname(path: unknown): string | null {
  try {
    return new URL(String(path || ''), 'http://hermes.local').pathname
  } catch {
    return null
  }
}

/** Return the dynamic profile name carried by /api/profiles/{name}/... paths. */
export function profileNameFromRequestPath(path: unknown): string | null {
  const pathname = requestPathname(path)
  const match = pathname?.match(/^\/api\/profiles\/([^/]+)(?:\/|$)/)

  if (!match) {
    return null
  }

  let name = ''

  try {
    name = decodeURIComponent(match[1]).trim().toLowerCase()
  } catch {
    return null
  }

  if (!name || STATIC_PROFILE_RESOURCES.has(name)) {
    return null
  }

  return name
}

export function isProfileCollectionPath(path: unknown): boolean {
  return requestPathname(path) === '/api/profiles'
}
