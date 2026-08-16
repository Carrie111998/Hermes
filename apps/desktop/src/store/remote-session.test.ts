import { beforeEach, describe, expect, it, vi } from 'vitest'

const STORAGE_KEY = 'hermes.desktop.remote-attach'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' }
  })
}

function openEventStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>
  const body = new ReadableStream<Uint8Array>({
    start(next) {
      controller = next
    }
  })

  return {
    response: new Response(body, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
    send(event: unknown) {
      controller.enqueue(new TextEncoder().encode(`data: ${JSON.stringify(event)}\n\n`))
    }
  }
}

function saveConnection(expiresAt = new Date(Date.now() + 60_000).toISOString()) {
  window.localStorage.setItem(
    STORAGE_KEY,
    JSON.stringify({ host: 'remote.test', port: 8642, token: 'attach-token', expiresAt })
  )
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(done => {
    resolve = done
  })

  return { promise, resolve }
}

describe('remote session store', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
    vi.resetModules()
    window.localStorage.clear()
  })

  it('starts idle when no valid persisted connection exists', async () => {
    const { $remoteAttach } = await import('./remote-session')

    expect($remoteAttach.get()).toEqual({
      host: '',
      port: 8642,
      token: '',
      expiresAt: '',
      sessions: [],
      status: 'idle'
    })
  })

  it('pairs with a code, persists the token, and clears an earlier error', async () => {
    const expiresAt = new Date(Date.now() + 86_400_000).toISOString()
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ token: 'new-token', expires_at: expiresAt }))
    vi.stubGlobal('fetch', fetchMock)
    const { $remoteAttach, pairWithCode } = await import('./remote-session')

    await pairWithCode('remote.test', 9443, 'ABC123')

    expect(fetchMock).toHaveBeenCalledWith(
      'http://remote.test:9443/api/remote/pair',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ code: 'ABC123' })
      })
    )
    expect($remoteAttach.get()).toMatchObject({
      host: 'remote.test',
      port: 9443,
      token: 'new-token',
      expiresAt,
      status: 'connected',
      error: undefined
    })
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({
      host: 'remote.test',
      port: 9443,
      token: 'new-token',
      expiresAt
    })
  })

  it('hydrates an unexpired connection but discards an expired token', async () => {
    saveConnection()
    let mod = await import('./remote-session')

    expect(mod.$remoteAttach.get()).toMatchObject({ token: 'attach-token', status: 'connected' })

    vi.resetModules()
    saveConnection(new Date(Date.now() - 1_000).toISOString())
    mod = await import('./remote-session')

    expect(mod.$remoteAttach.get().status).toBe('idle')
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('refreshes with bearer auth and merges server metadata without clobbering attached events', async () => {
    saveConnection()
    const stream = openEventStream()
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(stream.response)
      .mockResolvedValueOnce(
        jsonResponse({
          sessions: [{ id: 's1', title: 'Renamed remotely', status: 'active', updated_at: '2026-08-16T12:00:00Z' }]
        })
      )
    vi.stubGlobal('fetch', fetchMock)
    const { $remoteAttach, attachToSession, refreshSessions } = await import('./remote-session')
    $remoteAttach.set({
      ...$remoteAttach.get(),
      sessions: [{ id: 's1', title: 'Old title', status: 'idle', updated_at: '2026-08-16T11:00:00Z', events: [] }]
    })

    await attachToSession('s1')
    stream.send({
      event: 'session.message',
      session_id: 's1',
      message: { role: 'assistant', content: 'Live answer' }
    })
    await vi.waitFor(() => expect($remoteAttach.get().sessions[0]?.events).toHaveLength(1))

    await refreshSessions()

    expect(fetchMock.mock.calls[1]).toEqual([
      'http://remote.test:8642/api/remote/sessions',
      expect.objectContaining({ headers: expect.objectContaining({ Authorization: 'Bearer attach-token' }) })
    ])
    expect($remoteAttach.get().sessions[0]).toMatchObject({
      id: 's1',
      title: 'Renamed remotely',
      status: 'active',
      events: [expect.objectContaining({ event: 'session.message' })]
    })
  })

  it('preserves an attached row and its reference when a no-op refresh omits it', async () => {
    saveConnection()
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ sessions: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const { $remoteAttach, refreshSessions } = await import('./remote-session')
    const attached = {
      id: 's1',
      title: 'Live',
      status: 'active' as const,
      updated_at: '2026-08-16T12:00:00Z',
      events: []
    }
    $remoteAttach.set({ ...$remoteAttach.get(), sessions: [attached], attachedSessionId: 's1' })

    await refreshSessions()

    expect($remoteAttach.get().sessions).toHaveLength(1)
    expect($remoteAttach.get().sessions[0]).toBe(attached)
  })

  it('ignores a stale refresh that resolves after a newer one', async () => {
    saveConnection()
    const older = deferred<Response>()
    const newer = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise))
    const { $remoteAttach, refreshSessions } = await import('./remote-session')

    const olderRequest = refreshSessions()
    const newerRequest = refreshSessions()
    newer.resolve(
      jsonResponse({
        sessions: [{ id: 's1', title: 'New truth', status: 'active', updated_at: '2026-08-16T12:00:00Z' }]
      })
    )
    await newerRequest
    older.resolve(
      jsonResponse({
        sessions: [{ id: 's1', title: 'Stale truth', status: 'idle', updated_at: '2026-08-16T11:00:00Z' }]
      })
    )
    await olderRequest

    expect($remoteAttach.get().sessions[0]?.title).toBe('New truth')
  })

  it('streams status, message, and tool events and caps the attached log at 200 entries', async () => {
    saveConnection()
    const stream = openEventStream()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(stream.response))
    const { $remoteAttach, attachToSession } = await import('./remote-session')
    $remoteAttach.set({
      ...$remoteAttach.get(),
      sessions: [{ id: 's1', title: 'Live', status: 'idle', updated_at: 'now', events: [] }]
    })

    await attachToSession('s1')
    stream.send({ event: 'session.status', session_id: 's1', status: 'active' })
    stream.send({ event: 'session.tool_call', session_id: 's1', tool_call: { id: 'call-1', phase: 'started' } })
    for (let index = 0; index < 200; index += 1) {
      stream.send({ event: 'session.message', session_id: 's1', sequence: index })
    }

    await vi.waitFor(() => expect($remoteAttach.get().sessions[0]?.events).toHaveLength(200))
    const session = $remoteAttach.get().sessions[0]
    expect(session?.status).toBe('active')
    expect(session?.events[0]).toMatchObject({ event: 'session.message', sequence: 0 })
    expect(session?.events.at(-1)).toMatchObject({ sequence: 199 })
  })

  it('detaches by aborting the active stream', async () => {
    saveConnection()
    const stream = openEventStream()
    let signal: AbortSignal | undefined
    vi.stubGlobal(
      'fetch',
      vi.fn().mockImplementation((_url: string, init?: RequestInit) => {
        signal = init?.signal ?? undefined
        return Promise.resolve(stream.response)
      })
    )
    const { $remoteAttach, attachToSession, detachSession } = await import('./remote-session')

    await attachToSession('s1')
    detachSession()

    expect(signal?.aborted).toBe(true)
    expect($remoteAttach.get().attachedSessionId).toBeUndefined()
    expect($remoteAttach.get().status).toBe('connected')
  })

  it('posts chat content to the attached session with bearer auth', async () => {
    saveConnection()
    const fetchMock = vi.fn().mockResolvedValue(jsonResponse({ ok: true }))
    vi.stubGlobal('fetch', fetchMock)
    const { $remoteAttach, sendRemoteChat } = await import('./remote-session')
    $remoteAttach.set({ ...$remoteAttach.get(), attachedSessionId: 'session/one' })

    await sendRemoteChat('Keep going')

    expect(fetchMock).toHaveBeenCalledWith(
      'http://remote.test:8642/api/remote/sessions/session%2Fone/chat',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({ content: 'Keep going' }),
        headers: expect.objectContaining({ Authorization: 'Bearer attach-token' })
      })
    )
    expect($remoteAttach.get()).toMatchObject({ status: 'connected', error: undefined })
  })

  it('turns a 401 into the token-expired error and drops the stream', async () => {
    saveConnection()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(jsonResponse({ error: 'unauthorized' }, 401)))
    const { $remoteAttach, attachToSession } = await import('./remote-session')

    await attachToSession('s1')

    expect($remoteAttach.get()).toMatchObject({
      status: 'error',
      error: 'Attach token expired — pair again',
      attachedSessionId: undefined
    })
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('moves to an error state after a network failure and clears it on recovery', async () => {
    saveConnection()
    const fetchMock = vi
      .fn()
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))
    vi.stubGlobal('fetch', fetchMock)
    const { $remoteAttach, refreshSessions } = await import('./remote-session')

    await refreshSessions()
    expect($remoteAttach.get()).toMatchObject({ status: 'error', error: 'Failed to fetch' })

    await refreshSessions()
    expect($remoteAttach.get()).toMatchObject({ status: 'connected', error: undefined })
  })
})
