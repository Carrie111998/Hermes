export interface RendererDeepLinkPayload {
  kind: string
  name: string
  params: Record<string, string>
}

export type ManagedDeepLink =
  { type: 'auth-callback' } | { type: 'blueprint'; payload: RendererDeepLinkPayload } | { type: 'unsupported' }

const SECRET_QUERY_KEY_RE =
  /(?:^|[._-])(?:auth|authorization|cookie|credential|device[_-]?code|secret|session|state|token)(?:$|[._-])/i

const ESCAPE_QUERY_KEYS = new Set([
  'agent',
  'agent_id',
  'base_url',
  'customer',
  'customer_id',
  'eva_session',
  'gateway',
  'gateway_url',
  'session_token',
  'token'
])

function hasAsciiControl(value: string): boolean {
  return Array.from(value).some(character => {
    const codePoint = character.codePointAt(0)

    return codePoint === undefined || codePoint <= 0x1f || codePoint === 0x7f
  })
}

export function classifyManagedDeepLink(rawUrl: string, callbackScheme: string): ManagedDeepLink {
  let url: URL

  try {
    url = new URL(rawUrl)
  } catch {
    return { type: 'unsupported' }
  }

  if (url.protocol !== `${callbackScheme}:` || url.username || url.password || url.port || url.hash) {
    return { type: 'unsupported' }
  }

  if (url.hostname === 'auth' && url.pathname === '/callback') {
    // Authentication stays in the main process. Never return callback query
    // material in a renderer-facing object.
    return { type: 'auth-callback' }
  }

  if (url.hostname !== 'blueprint') {
    return { type: 'unsupported' }
  }

  let name: string

  try {
    name = decodeURIComponent(url.pathname.replace(/^\//, ''))
  } catch {
    return { type: 'unsupported' }
  }

  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(name)) {
    return { type: 'unsupported' }
  }

  const params: Record<string, string> = {}

  for (const [key, value] of url.searchParams) {
    const normalizedKey = key.toLowerCase()

    if (
      !/^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$/.test(key) ||
      ESCAPE_QUERY_KEYS.has(normalizedKey) ||
      SECRET_QUERY_KEY_RE.test(normalizedKey) ||
      value.length > 2_048 ||
      hasAsciiControl(value) ||
      Object.prototype.hasOwnProperty.call(params, key)
    ) {
      return { type: 'unsupported' }
    }

    params[key] = value
  }

  return { type: 'blueprint', payload: { kind: 'blueprint', name, params } }
}
