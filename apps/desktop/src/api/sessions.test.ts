import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/gateway-rpc', () => ({ isMissingRestEndpoint: () => false }))
vi.mock('@/store/transcript-tail', () => ({ recordTranscriptTail: vi.fn() }))
vi.mock('./client', () => ({
  capabilityScoped: vi.fn(),
  getApiRequestConnection: vi.fn(() => 'prometheus'),
  hermesApi: vi.fn(),
  profileScoped: vi.fn(() => ({}))
}))

const client = await import('./client')
const {
  listSidebarSessions,
  setSessionArchived,
  setSessionPinnedRemote,
  setSessionUnreadRemote
} = await import('./sessions')

const hermesApi = vi.mocked(client.hermesApi)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(client.getApiRequestConnection).mockReturnValue('prometheus')
})

describe('listSidebarSessions remote ownership', () => {
  it('stamps active remote rows so a later resume stays on their gateway', async () => {
    hermesApi.mockResolvedValue({
      cron: { sessions: [] },
      messaging: { sessions: [] },
      recents: {
        sessions: [{ id: 'remote-session', profile: 'default', source: 'desktop', title: 'Remote chat' }]
      }
    } as never)

    const result = await listSidebarSessions({
      recentsProfile: 'default',
      recentsLimit: 40,
      recentsExclude: [],
      cronLimit: 20,
      messagingLimit: 40,
      messagingExclude: []
    })

    expect(result.recents.sessions[0]).toMatchObject({ connection_id: 'prometheus', id: 'remote-session' })
  })
})

describe('session mutation profile propagation', () => {
  it('setSessionArchived passes the owning profile in the body', async () => {
    hermesApi.mockResolvedValue({ ok: true } as never)

    await setSessionArchived('sess-1', true, 'worker-beta')

    expect(hermesApi).toHaveBeenCalledWith(expect.objectContaining({
      body: { archived: true, profile: 'worker-beta' }
    }))
  })

  it('setSessionPinnedRemote passes the owning profile in the body', async () => {
    hermesApi.mockResolvedValue({ ok: true } as never)

    await setSessionPinnedRemote('sess-1', true, 'worker-beta')

    expect(hermesApi).toHaveBeenCalledWith(expect.objectContaining({
      body: { pinned: true, profile: 'worker-beta' }
    }))
  })

  it('setSessionUnreadRemote passes the owning profile in the body', async () => {
    hermesApi.mockResolvedValue({ ok: true } as never)

    await setSessionUnreadRemote('sess-1', true, 'worker-beta')

    expect(hermesApi).toHaveBeenCalledWith(expect.objectContaining({
      body: { unread: true, profile: 'worker-beta' }
    }))
  })

  it('omits the body profile when no owning profile is given', async () => {
    hermesApi.mockResolvedValue({ ok: true } as never)

    await setSessionArchived('sess-1', true)

    expect(hermesApi).toHaveBeenCalledWith(expect.objectContaining({
      body: { archived: true }
    }))
  })
})
