import type { HermesApiRequest, HermesConnection } from '@/global'

const SPECTATOR_HEADER = 'X-Hermes-Spectator-Token'

export const SPECTATOR_ROOT_ATTRIBUTE = 'data-hermes-spectator'

const READ_ONLY_PREFIXES = [
  '/api/config',
  '/api/hermes/version',
  '/api/model/info',
  '/api/personalities',
  '/api/profiles',
  '/api/sessions',
  '/api/status'
] as const

declare global {
  interface Window {
    __HERMES_AUTH_REQUIRED__?: boolean
    __HERMES_BASE_PATH__?: string
    __HERMES_SPECTATOR_TOKEN__?: string
    __HERMES_SPECTATOR__?: boolean
    __HERMES_SPECTATOR_BASE_PATH__?: string
  }
}

function basePath(): string {
  const raw = window.__HERMES_BASE_PATH__?.trim() ?? ''

  if (!raw) {
    return ''
  }

  return `/${raw.replace(/^\/+|\/+$/g, '')}`
}

function requestPath(request: HermesApiRequest): string {
  if (!request.path.startsWith('/') || request.path.startsWith('//')) {
    throw new Error('Spectator requests must use a same-origin absolute path')
  }

  const parsed = new URL(request.path, window.location.origin)

  const allowed = READ_ONLY_PREFIXES.some(
    prefix => parsed.pathname === prefix || parsed.pathname.startsWith(`${prefix}/`)
  )

  if (!allowed) {
    throw new Error(`Spectator endpoint is not read-enabled: ${parsed.pathname}`)
  }

  if (request.profile && !parsed.searchParams.has('profile')) {
    parsed.searchParams.set('profile', request.profile)
  }

  return `${basePath()}${parsed.pathname}${parsed.search}${parsed.hash}`
}

export function isBrowserSpectator(): boolean {
  return typeof window !== 'undefined' && window.__HERMES_SPECTATOR__ === true && !window.hermesDesktop
}

/** Mark the document before React mounts so iPad-only layout/touch rules never
 * leak into native Desktop windows. Idempotent for StrictMode and hot reload. */
export function applySpectatorDocumentMode(): boolean {
  const enabled = isBrowserSpectator()

  document.documentElement.toggleAttribute(SPECTATOR_ROOT_ATTRIBUTE, enabled)

  return enabled
}

export function assertSpectatorReadRequest(request: HermesApiRequest): void {
  const method = (request.method ?? 'GET').toUpperCase()

  if (method !== 'GET' || request.body !== undefined || request.upload !== undefined || request.connectionId) {
    throw new Error('Hermes iPad spectator is read-only')
  }

  requestPath(request)
}

export async function browserSpectatorApi<T>(request: HermesApiRequest): Promise<T> {
  assertSpectatorReadRequest(request)

  const token = window.__HERMES_SPECTATOR_TOKEN__

  if (!token) {
    throw new Error('Spectator credential is unavailable')
  }

  const controller = new AbortController()
  const timeout = window.setTimeout(() => controller.abort(), request.timeoutMs ?? 30_000)
  const headers = new Headers({ Accept: 'application/json' })
  headers.set(SPECTATOR_HEADER, token)

  try {
    const response = await fetch(requestPath(request), {
      credentials: 'include',
      headers,
      method: 'GET',
      signal: controller.signal
    })

    if (response.status === 401 && window.__HERMES_AUTH_REQUIRED__) {
      const body = (await response
        .clone()
        .json()
        .catch(() => null)) as null | { login_url?: string }

      if (body?.login_url) {
        window.location.assign(body.login_url)

        return new Promise<T>(() => undefined)
      }
    }

    if (!response.ok) {
      const detail = await response.text().catch(() => response.statusText)
      throw new Error(`${response.status}: ${detail}`)
    }

    return (await response.json()) as T
  } finally {
    window.clearTimeout(timeout)
  }
}

async function websocketAuth(): Promise<readonly [string, string]> {
  const token = window.__HERMES_SPECTATOR_TOKEN__ ?? ''

  if (!token) {
    throw new Error('Spectator credential is unavailable')
  }

  return ['spectator', token]
}

export async function browserSpectatorConnection(): Promise<HermesConnection> {
  const [name, credential] = await websocketAuth()
  const ws = new URL(`${basePath()}/api/ws`, window.location.origin)
  ws.protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  ws.searchParams.set(name, credential)

  return {
    authMode: window.__HERMES_AUTH_REQUIRED__ ? 'oauth' : 'token',
    baseUrl: `${window.location.origin}${basePath()}`,
    isFullscreen: false,
    logs: [],
    mode: 'remote',
    nativeOverlayWidth: 0,
    source: 'settings',
    token: '',
    windowButtonPosition: null,
    wsUrl: ws.toString()
  }
}
