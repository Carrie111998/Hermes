import { describe, expect, it, vi } from 'vitest'

vi.mock('@/sdk/index', () => ({ host: {} }))
vi.mock('@/store/session', () => ({ $connection: { listen: vi.fn() } }))
vi.mock('./focus-bridge', () => ({ installFocusedDrawingBridge: vi.fn() }))
vi.mock('./store', () => ({ handleChangedDocument: vi.fn(), openDrawing: vi.fn(), restoreExcalidrawDocuments: vi.fn() }))

import { parseDocumentEvent } from './plugin'

describe('Excalidraw desktop event identity', () => {
  it('uses the background socket runtime instead of the active remote connection', () => {
    const event = {
      type: 'excalidraw.open',
      profile: 'work-a',
      payload: { fingerprint: 'fingerprint', path: '/drawings/remote.excalidraw', profile: 'work-a', runtime: 'local' },
      runtime: 'remote:ssh:work-a:owner-a@host'
    }

    expect(parseDocumentEvent(event)).toMatchObject({
      identity: { path: '/drawings/remote.excalidraw', profile: 'work-a', runtime: 'remote:ssh:work-a:owner-a@host' }
    })
    expect(parseDocumentEvent({ ...event, profile: 'work-b' } as never)).toBeNull()
    expect(parseDocumentEvent({ ...event, runtime: undefined } as never)).toBeNull()
  })

  it('preserves the backend local identity for unstamped local events', () => {
    expect(parseDocumentEvent({
      payload: { fingerprint: 'fingerprint', path: '/drawings/local.excalidraw', profile: 'default', runtime: 'local' }
    } as never)?.identity.runtime).toBe('local')
  })
})
