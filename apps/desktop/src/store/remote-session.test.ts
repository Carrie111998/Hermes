import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

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
    close() {
      controller.close()
    },
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
    vi.useRealTimers()
    vi.resetModules()
    window.localStorage.clear()
  })

  afterEach(() => {
    vi.useRealTimers()
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

  it('marks an attached session ended and detaches when refresh no longer returns it', async () => {
    saveConnection()
    const stream = openEventStream()
    let signal: AbortSignal | undefined

    const fetchMock = vi
      .fn()
      .mockImplementationOnce((_url: string, init?: RequestInit) => {
        signal = init?.signal ?? undefined

        return Promise.resolve(stream.response)
      })
      .mockResolvedValueOnce(jsonResponse({ sessions: [] }))

    vi.stubGlobal('fetch', fetchMock)
    const { $remoteAttach, attachToSession, refreshSessions } = await import('./remote-session')

    const attached = {
      id: 's1',
      title: 'Live',
      status: 'active' as const,
      updated_at: '2026-08-16T12:00:00Z',
      events: []
    }

    $remoteAttach.set({ ...$remoteAttach.get(), sessions: [attached] })

    await attachToSession('s1')
    await refreshSessions()

    expect(signal?.aborted).toBe(true)
    expect($remoteAttach.get().sessions).toHaveLength(1)
    expect($remoteAttach.get().sessions[0]).toMatchObject({ id: 's1', status: 'ended' })
    expect($remoteAttach.get()).toMatchObject({
      attachedSessionId: undefined,
      status: 'connected',
      error: 'The remote session ended.'
    })
  })

  it('silently drops a non-attached session missing from a refresh', async () => {
    saveConnection()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({
          sessions: [{ id: 's1', title: 'Live', status: 'active', updated_at: '2026-08-16T12:00:00Z' }]
        })
      )
    )
    const { $remoteAttach, refreshSessions } = await import('./remote-session')
    $remoteAttach.set({
      ...$remoteAttach.get(),
      attachedSessionId: 's1',
      sessions: [
        { id: 's1', title: 'Live', status: 'active', updated_at: '2026-08-16T12:00:00Z', events: [] },
        { id: 's2', title: 'Gone', status: 'idle', updated_at: '2026-08-16T11:00:00Z', events: [] }
      ]
    })

    await refreshSessions()

    expect($remoteAttach.get()).toMatchObject({ attachedSessionId: 's1', status: 'connected', error: undefined })
    expect($remoteAttach.get().sessions.map(session => session.id)).toEqual(['s1'])
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

  it('redacts secrets recursively before streamed events enter the store', async () => {
    saveConnection()
    const stream = openEventStream()
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(stream.response))
    const { $remoteAttach, attachToSession } = await import('./remote-session')
    $remoteAttach.set({
      ...$remoteAttach.get(),
      sessions: [{ id: 's1', title: 'Live', status: 'idle', updated_at: 'now', events: [] }]
    })

    await attachToSession('s1')
    stream.send({
      event: 'session.message',
      session_id: 's1',
      message: {
        role: 'assistant',
        content:
          'keys sk-proj-abcdefghijklmnopqrstuvwxyz and sk-ant-zyxwvutsrqponmlkjihgfedc; Authorization: Bearer bearer-token-value-1234567890',
        metadata: {
          hex: '0123456789abcdef0123456789abcdef',
          base64: 'QWxhZGRpbjpvcGVuIHNlc2FtZQ==',
          alreadySafe: '[REDACTED]',
          shortSessionId: 'session-1234',
          timestamp: '2026-08-16T12:00:00Z'
        }
      }
    })

    await vi.waitFor(() => expect($remoteAttach.get().sessions[0]?.events).toHaveLength(1))

    expect($remoteAttach.get().sessions[0]?.events[0]).toMatchObject({
      session_id: 's1',
      message: {
        content: 'keys sk-*** and sk-***; Authorization: Bearer ***',
        metadata: {
          hex: '***',
          base64: '***',
          alreadySafe: '[REDACTED]',
          shortSessionId: 'session-1234',
          timestamp: '2026-08-16T12:00:00Z'
        }
      }
    })
  })

  it('redacts rendered session metadata returned by refresh', async () => {
    saveConnection()
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue(
        jsonResponse({
          sessions: [
            {
              id: 'session-1234',
              title: 'Deploy with sk-proj-abcdefghijklmnopqrstuvwxyz and abcdefghijklmnopqrstuvwx',
              status: 'active',
              updated_at: '2026-08-16T12:00:00Z'
            }
          ]
        })
      )
    )
    const { $remoteAttach, refreshSessions } = await import('./remote-session')

    await refreshSessions()

    expect($remoteAttach.get().sessions[0]).toMatchObject({
      id: 'session-1234',
      title: 'Deploy with sk-*** and ***',
      updated_at: '2026-08-16T12:00:00Z'
    })
  })

  it('retries a disconnected event stream three times with increasing backoff before failing', async () => {
    vi.useFakeTimers()
    saveConnection()
    const streams = Array.from({ length: 4 }, () => openEventStream())
    const fetchMock = vi.fn()

    for (const stream of streams) {
      fetchMock.mockResolvedValueOnce(stream.response)
    }

    vi.stubGlobal('fetch', fetchMock)
    const { $remoteAttach, attachToSession } = await import('./remote-session')

    await attachToSession('s1')
    streams[0].close()
    await vi.advanceTimersByTimeAsync(0)

    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect($remoteAttach.get()).toMatchObject({
      attachedSessionId: 's1',
      status: 'connecting',
      error: undefined,
      reconnecting: true
    })

    await vi.advanceTimersByTimeAsync(499)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    streams[1].close()
    await vi.advanceTimersByTimeAsync(1_499)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchMock).toHaveBeenCalledTimes(3)
    streams[2].close()
    await vi.advanceTimersByTimeAsync(3_999)
    expect(fetchMock).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchMock).toHaveBeenCalledTimes(4)
    streams[3].close()
    await vi.advanceTimersByTimeAsync(0)

    expect($remoteAttach.get()).toMatchObject({
      attachedSessionId: undefined,
      status: 'error',
      error: 'Remote session stream disconnected'
    })
  })

  it('does not retry after a reconnect receives 401', async () => {
    vi.useFakeTimers()
    saveConnection()
    const stream = openEventStream()

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(stream.response)
      .mockResolvedValueOnce(jsonResponse({ error: 'unauthorized' }, 401))

    vi.stubGlobal('fetch', fetchMock)
    const { $remoteAttach, attachToSession } = await import('./remote-session')

    await attachToSession('s1')
    stream.close()
    await vi.advanceTimersByTimeAsync(500)
    await vi.advanceTimersByTimeAsync(10_000)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect($remoteAttach.get()).toMatchObject({
      token: '',
      attachedSessionId: undefined,
      status: 'error',
      error: 'Attach token expired — pair again'
    })
  })

  it('does not retry when detach aborts a disconnected stream', async () => {
    vi.useFakeTimers()
    saveConnection()
    const stream = openEventStream()
    const fetchMock = vi.fn().mockResolvedValue(stream.response)
    vi.stubGlobal('fetch', fetchMock)
    const { attachToSession, detachSession } = await import('./remote-session')

    await attachToSession('s1')
    stream.close()
    await vi.advanceTimersByTimeAsync(0)
    detachSession()
    await vi.advanceTimersByTimeAsync(10_000)

    expect(fetchMock).toHaveBeenCalledOnce()
  })

  it('cancels a pending reconnect when a new pairing starts', async () => {
    vi.useFakeTimers()
    saveConnection()
    const stream = openEventStream()
    const expiresAt = new Date(Date.now() + 86_400_000).toISOString()

    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce(stream.response)
      .mockResolvedValueOnce(jsonResponse({ token: 'new-token', expires_at: expiresAt }))

    vi.stubGlobal('fetch', fetchMock)
    const { $remoteAttach, attachToSession, pairWithCode } = await import('./remote-session')

    await attachToSession('s1')
    stream.close()
    await vi.advanceTimersByTimeAsync(0)
    await pairWithCode('other.test', 9443, 'ABC123')
    await vi.advanceTimersByTimeAsync(10_000)

    expect(fetchMock).toHaveBeenCalledTimes(2)
    expect($remoteAttach.get()).toMatchObject({
      host: 'other.test',
      token: 'new-token',
      status: 'connected'
    })
    expect($remoteAttach.get().attachedSessionId).toBeUndefined()
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

  it('disconnects by dropping persisted credentials and aborting the active stream', async () => {
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
    const { $remoteAttach, attachToSession, disconnect } = await import('./remote-session')

    await attachToSession('s1')
    disconnect()

    expect(signal?.aborted).toBe(true)
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
    expect($remoteAttach.get()).toEqual({
      host: '',
      port: 8642,
      token: '',
      expiresAt: '',
      sessions: [],
      status: 'idle'
    })
  })

  it('ignores a chat response that completes after disconnecting', async () => {
    saveConnection()
    const pending = deferred<Response>()
    vi.stubGlobal('fetch', vi.fn().mockReturnValue(pending.promise))
    const { $remoteAttach, disconnect, sendRemoteChat } = await import('./remote-session')
    $remoteAttach.set({ ...$remoteAttach.get(), attachedSessionId: 's1' })

    const request = sendRemoteChat('Still there?')
    disconnect()
    pending.resolve(jsonResponse({ ok: true }))
    await request

    expect($remoteAttach.get()).toEqual({
      host: '',
      port: 8642,
      token: '',
      expiresAt: '',
      sessions: [],
      status: 'idle'
    })
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
