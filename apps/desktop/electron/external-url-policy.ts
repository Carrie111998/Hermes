export type ExternalUrlClassification = {
  kind: 'external' | 'file'
  url: URL
}

export function classifyExternalUrl(rawUrl: unknown): ExternalUrlClassification | null {
  const raw = String(rawUrl || '').trim()

  if (!raw) {
    return null
  }

  let url: URL

  try {
    url = new URL(raw)
  } catch {
    return null
  }

  if (
    url.protocol === 'file:' &&
    (!url.hostname || url.hostname.toLowerCase() === 'localhost') &&
    !url.username &&
    !url.password &&
    !url.port &&
    !url.pathname.startsWith('//')
  ) {
    return { kind: 'file', url }
  }

  if (['http:', 'https:', 'mailto:'].includes(url.protocol)) {
    return { kind: 'external', url }
  }

  if (
    url.protocol === 'obsidian:' &&
    url.hostname.toLowerCase() === 'open' &&
    !url.username &&
    !url.password &&
    !url.port &&
    (!url.pathname || url.pathname === '/') &&
    !url.hash
  ) {
    return { kind: 'external', url }
  }

  return null
}
