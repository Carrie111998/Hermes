import { afterEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'

import { restoreErrorForSessionSurface, uploadSessionSurfaceAttachment } from './session-tile-actions'

describe('profile-owned session restore errors', () => {
  it('replaces backend paths and secrets with stable restore copy', () => {
    const raw = new Error('/home/alice/private/state.db token=super-secret')
    const presented = restoreErrorForSessionSurface(raw, 'profile-b')

    expect(presented).toBeInstanceOf(Error)
    expect((presented as Error).message).toBe('Restore failed')
    expect((presented as Error).message).not.toContain('alice')
    expect((presented as Error).message).not.toContain('super-secret')
  })

  it('preserves legacy unprofiled restore detail', () => {
    const raw = new Error('legacy restore detail')

    expect(restoreErrorForSessionSurface(raw)).toBe(raw)
  })
})

describe('profile-owned attachment staging', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    $connection.set(null)
  })

  it('uploads host bytes to a remote B owner even when foreground A is local', async () => {
    const getConnection = vi.fn(async (profile: string) => ({ mode: profile === 'profile-b' ? 'remote' : 'local' }))
    const readFileDataUrl = vi.fn(async () => 'data:text/plain;base64,aGVsbG8=')
    const requestGateway = vi.fn(async () => ({ attached: true, ref_text: '@file:hello.txt' }))
    vi.stubGlobal('window', { hermesDesktop: { getConnection, readFileDataUrl } })
    $connection.set({ mode: 'local', profile: 'profile-a' } as never)

    await uploadSessionSurfaceAttachment(
      { id: 'file:hello', kind: 'file', label: 'hello.txt', path: '/Users/me/hello.txt' },
      {
        profile: 'profile-b',
        requestGateway: requestGateway as never,
        sessionId: 'runtime-b',
        storedSessionId: 'stored-b'
      }
    )

    expect(getConnection).toHaveBeenCalledWith('profile-b')
    expect(requestGateway).toHaveBeenCalledWith('file.attach', {
      data_url: 'data:text/plain;base64,aGVsbG8=',
      name: 'hello.txt',
      path: '/Users/me/hello.txt',
      session_id: 'runtime-b'
    })
  })

  it('sends a shared path to a local B owner even when foreground A is remote', async () => {
    const getConnection = vi.fn(async () => ({ mode: 'local' }))
    const readFileDataUrl = vi.fn(async () => 'should-not-be-read')
    const requestGateway = vi.fn(async () => ({ attached: true, ref_text: '@file:hello.txt' }))
    vi.stubGlobal('window', { hermesDesktop: { getConnection, readFileDataUrl } })
    $connection.set({ mode: 'remote', profile: 'profile-a' } as never)

    await uploadSessionSurfaceAttachment(
      { id: 'file:hello', kind: 'file', label: 'hello.txt', path: '/Users/me/hello.txt' },
      {
        profile: 'profile-b',
        requestGateway: requestGateway as never,
        sessionId: 'runtime-b',
        storedSessionId: 'stored-b'
      }
    )

    expect(getConnection).toHaveBeenCalledWith('profile-b')
    expect(readFileDataUrl).not.toHaveBeenCalled()
    expect(requestGateway).toHaveBeenCalledWith('file.attach', {
      name: 'hello.txt',
      path: '/Users/me/hello.txt',
      session_id: 'runtime-b'
    })
  })

  it('uploads bytes for a local B owner whose session backend has an isolated filesystem', async () => {
    const getConnection = vi.fn(async () => ({ mode: 'local' }))
    const readFileDataUrl = vi.fn(async () => 'data:text/plain;base64,aGVsbG8=')
    const requestGateway = vi.fn(async () => ({ attached: true, ref_text: '@file:hello.txt' }))
    vi.stubGlobal('window', { hermesDesktop: { getConnection, readFileDataUrl } })

    await uploadSessionSurfaceAttachment(
      { id: 'file:hello', kind: 'file', label: 'hello.txt', path: '/Users/me/hello.txt' },
      {
        profile: 'profile-b',
        requestGateway: requestGateway as never,
        sessionId: 'runtime-b',
        terminalBackend: 'docker'
      }
    )

    expect(requestGateway).toHaveBeenCalledWith('file.attach', {
      data_url: 'data:text/plain;base64,aGVsbG8=',
      name: 'hello.txt',
      path: '/Users/me/hello.txt',
      session_id: 'runtime-b'
    })
  })
})
