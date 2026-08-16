import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

export type RemoteAttachStatus = 'idle' | 'connecting' | 'connected' | 'error'

export interface RemoteSessionEvent {
  event: 'session.message' | 'session.status' | 'session.tool_call'
  session_id?: string
  status?: string
  timestamp?: number | string
  [key: string]: unknown
}

export interface RemoteSession {
  id: string
  title: null | string
  status: string
  updated_at: string
  events: RemoteSessionEvent[]
}

export interface RemoteAttachState {
  host: string
  port: number
  token: string
  expiresAt: string
  sessions: RemoteSession[]
  status: RemoteAttachStatus
  error?: string
  attachedSessionId?: string
}

interface PersistedRemoteAttach {
  host: string
  port: number
  token: string
  expiresAt: string
}

interface PairResponse {
  token?: unknown
  expires_at?: unknown
}

interface SessionsResponse {
  sessions?: unknown
}

const STORAGE_KEY = 'hermes.desktop.remote-attach'
const DEFAULT_REMOTE_PORT = 8642
const MAX_SESSION_EVENTS = 200
const TOKEN_EXPIRED_ERROR = 'Attach token expired — pair again'

class RemoteRequestError extends Error {
  constructor(
    message: string,
    readonly status?: number
  ) {
    super(message)
  }
}

function readPersistedConnection(): PersistedRemoteAttach | null {
  const raw = storedString(STORAGE_KEY)

  if (!raw) {
    return null
  }

  try {
    const value = JSON.parse(raw) as Partial<PersistedRemoteAttach>
    const valid =
      typeof value.host === 'string' &&
      value.host.trim().length > 0 &&
      typeof value.port === 'number' &&
      Number.isInteger(value.port) &&
      value.port >= 1 &&
      value.port <= 65_535 &&
      typeof value.token === 'string' &&
      value.token.length > 0 &&
      typeof value.expiresAt === 'string' &&
      Number.isFinite(Date.parse(value.expiresAt)) &&
      Date.parse(value.expiresAt) > Date.now()

    if (valid) {
      return value as PersistedRemoteAttach
    }
  } catch {
    // Malformed or stale persisted credentials are treated as absent.
  }

  persistString(STORAGE_KEY, null)

  return null
}

const persisted = readPersistedConnection()

export const $remoteAttach = atom<RemoteAttachState>(
  persisted
    ? { ...persisted, sessions: [], status: 'connected' }
    : {
        host: '',
        port: DEFAULT_REMOTE_PORT,
        token: '',
        expiresAt: '',
        sessions: [],
        status: 'idle'
      }
)

let streamAbort: AbortController | null = null
let streamGeneration = 0
let connectionGeneration = 0
let refreshGeneration = 0

function persistConnection(state: RemoteAttachState): void {
  if (!state.host || !state.token || !state.expiresAt) {
    persistString(STORAGE_KEY, null)

    return
  }

  persistString(
    STORAGE_KEY,
    JSON.stringify({ host: state.host, port: state.port, token: state.token, expiresAt: state.expiresAt })
  )
}

function hostForUrl(host: string): string {
  if (host.startsWith('[') && host.endsWith(']')) {
    return host
  }

  return host.includes(':') ? `[${host}]` : host
}

function remoteBaseUrl(host: string, port: number): string {
  const trimmed = host.trim().replace(/\/+$/, '')

  if (/^https?:\/\//i.test(trimmed)) {
    const url = new URL(trimmed)
    url.port = String(port)
    url.pathname = ''
    url.search = ''
    url.hash = ''

    return url.toString().replace(/\/$/, '')
  }

  return `http://${hostForUrl(trimmed)}:${port}`
}

function authHeaders(token: string, json = false): Record<string, string> {
  return {
    Accept: json ? 'application/json' : 'text/event-stream',
    ...(token ? { Authorization: `Bearer ${token}` } : {}),
    ...(json ? { 'Content-Type': 'application/json' } : {})
  }
}

async function checkedFetch(url: string, init: RequestInit): Promise<Response> {
  const response = await fetch(url, init)

  if (response.status === 401) {
    throw new RemoteRequestError(TOKEN_EXPIRED_ERROR, 401)
  }

  if (!response.ok) {
    throw new RemoteRequestError(`Remote host returned HTTP ${response.status}`, response.status)
  }

  return response
}

async function requestJson<T>(url: string, init: RequestInit): Promise<T> {
  const response = await checkedFetch(url, init)

  try {
    return (await response.json()) as T
  } catch {
    throw new RemoteRequestError('Remote host returned invalid JSON')
  }
}

function closeStream(): void {
  streamGeneration += 1
  streamAbort?.abort()
  streamAbort = null
}

export function disconnect(): void {
  connectionGeneration += 1
  refreshGeneration += 1
  closeStream()
  persistString(STORAGE_KEY, null)
  $remoteAttach.set({
    host: '',
    port: DEFAULT_REMOTE_PORT,
    token: '',
    expiresAt: '',
    sessions: [],
    status: 'idle'
  })
}

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : 'Remote connection failed'
}

function publishError(error: unknown): void {
  closeStream()
  const authExpired = error instanceof RemoteRequestError && error.status === 401
  const previous = $remoteAttach.get()
  const next: RemoteAttachState = {
    ...previous,
    status: 'error',
    error: authExpired ? TOKEN_EXPIRED_ERROR : errorMessage(error),
    attachedSessionId: undefined,
    ...(authExpired ? { token: '', expiresAt: '' } : {})
  }

  $remoteAttach.set(next)
  if (authExpired) {
    persistString(STORAGE_KEY, null)
  }
}

function usableConnection(): PersistedRemoteAttach | null {
  const state = $remoteAttach.get()
  const expiresAt = Date.parse(state.expiresAt)

  if (!state.host || !state.token || !Number.isFinite(expiresAt) || expiresAt <= Date.now()) {
    publishError(new RemoteRequestError(TOKEN_EXPIRED_ERROR, 401))

    return null
  }

  return { host: state.host, port: state.port, token: state.token, expiresAt: state.expiresAt }
}

function parsedSession(value: unknown): RemoteSession | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return null
  }

  const row = value as Record<string, unknown>

  if (typeof row.id !== 'string' || !row.id) {
    return null
  }

  return {
    id: row.id,
    title: typeof row.title === 'string' ? row.title : null,
    status: typeof row.status === 'string' ? row.status : 'idle',
    updated_at: typeof row.updated_at === 'string' ? row.updated_at : '',
    events: []
  }
}

function latestLiveStatus(events: RemoteSessionEvent[]): string | undefined {
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index]

    if (event.event === 'session.status' && typeof event.status === 'string') {
      return event.status
    }
  }

  return undefined
}

function sameSession(left: RemoteSession, right: RemoteSession): boolean {
  return (
    left.id === right.id &&
    left.title === right.title &&
    left.status === right.status &&
    left.updated_at === right.updated_at &&
    left.events === right.events
  )
}

function mergeSessions(previous: RemoteSession[], incoming: RemoteSession[], attachedId?: string): RemoteSession[] {
  const previousById = new Map(previous.map(session => [session.id, session]))
  const merged = incoming.map(session => {
    const existing = previousById.get(session.id)

    if (!existing) {
      return session
    }

    const next: RemoteSession = {
      ...session,
      status: latestLiveStatus(existing.events) ?? session.status,
      events: existing.events
    }

    return sameSession(existing, next) ? existing : next
  })

  if (attachedId && !merged.some(session => session.id === attachedId)) {
    const attached = previousById.get(attachedId)

    if (attached) {
      merged.unshift(attached)
    }
  }

  return merged
}

function isRemoteSessionEvent(value: unknown): value is RemoteSessionEvent {
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    return false
  }

  const event = (value as { event?: unknown }).event

  return event === 'session.message' || event === 'session.tool_call' || event === 'session.status'
}

function eventUpdatedAt(event: RemoteSessionEvent, fallback: string): string {
  if (typeof event.timestamp === 'string') {
    return event.timestamp
  }

  if (typeof event.timestamp === 'number' && Number.isFinite(event.timestamp)) {
    return new Date(event.timestamp * 1_000).toISOString()
  }

  return fallback
}

function applySessionEvent(sessionId: string, event: RemoteSessionEvent, generation: number): void {
  if (generation !== streamGeneration || $remoteAttach.get().attachedSessionId !== sessionId) {
    return
  }

  const state = $remoteAttach.get()
  const index = state.sessions.findIndex(session => session.id === sessionId)
  const existing: RemoteSession =
    index >= 0 ? state.sessions[index] : { id: sessionId, title: null, status: 'idle', updated_at: '', events: [] }
  const events = [...existing.events, event].slice(-MAX_SESSION_EVENTS)
  const nextSession: RemoteSession = {
    ...existing,
    status: event.event === 'session.status' && typeof event.status === 'string' ? event.status : existing.status,
    updated_at: eventUpdatedAt(event, existing.updated_at),
    events
  }
  const sessions = [...state.sessions]

  if (index >= 0) {
    sessions[index] = nextSession
  } else {
    sessions.unshift(nextSession)
  }

  $remoteAttach.set({ ...state, sessions, status: 'connected', error: undefined })
}

function consumeFrame(frame: string, sessionId: string, generation: number): void {
  const data = frame
    .split(/\r?\n/)
    .filter(line => line.startsWith('data:'))
    .map(line => line.slice(5).replace(/^ /, ''))
    .join('\n')

  if (!data) {
    return
  }

  try {
    const event: unknown = JSON.parse(data)

    if (isRemoteSessionEvent(event)) {
      applySessionEvent(sessionId, event, generation)
    }
  } catch {
    // A malformed event must not terminate an otherwise healthy stream.
  }
}

async function consumeEventStream(
  body: ReadableStream<Uint8Array>,
  sessionId: string,
  generation: number
): Promise<void> {
  const reader = body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''

  try {
    while (generation === streamGeneration) {
      const { done, value } = await reader.read()

      if (done) {
        break
      }

      buffer += decoder.decode(value, { stream: true })
      let boundary = buffer.match(/\r?\n\r?\n/)

      while (boundary?.index !== undefined) {
        consumeFrame(buffer.slice(0, boundary.index), sessionId, generation)
        buffer = buffer.slice(boundary.index + boundary[0].length)
        boundary = buffer.match(/\r?\n\r?\n/)
      }
    }
  } finally {
    reader.releaseLock()
  }

  if (generation === streamGeneration && $remoteAttach.get().attachedSessionId === sessionId) {
    publishError(new Error('Remote session stream disconnected'))
  }
}

export async function pairWithCode(host: string, port: number, code: string): Promise<void> {
  closeStream()
  const generation = ++connectionGeneration
  refreshGeneration += 1
  const normalizedHost = host.trim()

  persistString(STORAGE_KEY, null)
  $remoteAttach.set({
    host: normalizedHost,
    port,
    token: '',
    expiresAt: '',
    sessions: [],
    status: 'connecting',
    error: undefined
  })

  try {
    const result = await requestJson<PairResponse>(`${remoteBaseUrl(normalizedHost, port)}/api/remote/pair`, {
      method: 'POST',
      headers: authHeaders('', true),
      body: JSON.stringify({ code })
    })

    if (generation !== connectionGeneration) {
      return
    }

    if (
      typeof result.token !== 'string' ||
      !result.token ||
      typeof result.expires_at !== 'string' ||
      !Number.isFinite(Date.parse(result.expires_at)) ||
      Date.parse(result.expires_at) <= Date.now()
    ) {
      throw new RemoteRequestError('Remote host returned an invalid pairing response')
    }

    const next: RemoteAttachState = {
      host: normalizedHost,
      port,
      token: result.token,
      expiresAt: result.expires_at,
      sessions: [],
      status: 'connected',
      error: undefined
    }

    $remoteAttach.set(next)
    persistConnection(next)
  } catch (error) {
    if (generation === connectionGeneration) {
      publishError(error)
    }
  }
}

export async function refreshSessions(): Promise<void> {
  const connection = usableConnection()

  if (!connection) {
    return
  }

  const connectionVersion = connectionGeneration
  const requestVersion = ++refreshGeneration
  const requestKey = `${connection.host}:${connection.port}:${connection.token}`
  $remoteAttach.set({ ...$remoteAttach.get(), status: 'connecting', error: undefined })

  try {
    const result = await requestJson<SessionsResponse>(
      `${remoteBaseUrl(connection.host, connection.port)}/api/remote/sessions`,
      { method: 'GET', headers: authHeaders(connection.token, true) }
    )
    const current = $remoteAttach.get()

    if (
      connectionVersion !== connectionGeneration ||
      requestVersion !== refreshGeneration ||
      requestKey !== `${current.host}:${current.port}:${current.token}`
    ) {
      return
    }

    const incoming = Array.isArray(result.sessions)
      ? result.sessions.map(parsedSession).filter((session): session is RemoteSession => session !== null)
      : []

    $remoteAttach.set({
      ...current,
      sessions: mergeSessions(current.sessions, incoming, current.attachedSessionId),
      status: 'connected',
      error: undefined
    })
  } catch (error) {
    const current = $remoteAttach.get()

    if (
      connectionVersion === connectionGeneration &&
      requestVersion === refreshGeneration &&
      requestKey === `${current.host}:${current.port}:${current.token}`
    ) {
      publishError(error)
    }
  }
}

export async function attachToSession(id: string): Promise<void> {
  const connection = usableConnection()

  if (!connection) {
    return
  }

  closeStream()
  const generation = streamGeneration
  const controller = new AbortController()
  streamAbort = controller
  $remoteAttach.set({
    ...$remoteAttach.get(),
    attachedSessionId: id,
    status: 'connecting',
    error: undefined
  })

  try {
    const response = await checkedFetch(
      `${remoteBaseUrl(connection.host, connection.port)}/api/remote/sessions/${encodeURIComponent(id)}/events`,
      {
        method: 'GET',
        headers: authHeaders(connection.token),
        signal: controller.signal
      }
    )

    if (generation !== streamGeneration || controller.signal.aborted) {
      return
    }

    if (!response.body) {
      throw new RemoteRequestError('Remote host returned an empty event stream')
    }

    $remoteAttach.set({ ...$remoteAttach.get(), status: 'connected', error: undefined })
    void consumeEventStream(response.body, id, generation).catch(error => {
      if (generation === streamGeneration && !controller.signal.aborted) {
        publishError(error)
      }
    })
  } catch (error) {
    if (generation === streamGeneration && !controller.signal.aborted) {
      publishError(error)
    }
  }
}

export function detachSession(): void {
  closeStream()
  const state = $remoteAttach.get()

  $remoteAttach.set({
    ...state,
    attachedSessionId: undefined,
    status: state.token ? 'connected' : 'idle',
    error: undefined
  })
}

export async function sendRemoteChat(text: string): Promise<void> {
  const connection = usableConnection()

  if (!connection) {
    return
  }

  const connectionVersion = connectionGeneration
  const sessionId = $remoteAttach.get().attachedSessionId

  if (!sessionId) {
    publishError(new Error('No remote session attached'))

    return
  }

  try {
    await requestJson<unknown>(
      `${remoteBaseUrl(connection.host, connection.port)}/api/remote/sessions/${encodeURIComponent(sessionId)}/chat`,
      {
        method: 'POST',
        headers: authHeaders(connection.token, true),
        body: JSON.stringify({ content: text })
      }
    )

    if (connectionVersion === connectionGeneration) {
      $remoteAttach.set({ ...$remoteAttach.get(), status: 'connected', error: undefined })
    }
  } catch (error) {
    if (connectionVersion === connectionGeneration) {
      publishError(error)
    }
  }
}
