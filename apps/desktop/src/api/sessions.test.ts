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
const { getSession, listSidebarSessions } = await import('./sessions')

const hermesApi = vi.mocked(client.hermesApi)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(client.getApiRequestConnection).mockReturnValue('prometheus')
})

describe('getSession routing', () => {
  it('clears the ambient connection tag for an explicit profile-door target', async () => {
    vi.mocked(client.capabilityScoped).mockReturnValue({ profile: 'arthur' })
    hermesApi.mockResolvedValue({ id: 's1' } as never)

    await getSession('s1', { connectionId: null, profile: 'arthur' })

    expect(hermesApi).toHaveBeenCalledWith({
      connectionId: undefined,
      path: '/api/sessions/s1?profile=arthur',
      profile: 'arthur'
    })
  })

  it('preserves an explicit local registry target', async () => {
    vi.mocked(client.capabilityScoped).mockReturnValue({ profile: 'arthur' })
    hermesApi.mockResolvedValue({ id: 's1' } as never)

    await getSession('s1', { connectionId: 'local', profile: 'arthur' })

    expect(hermesApi).toHaveBeenCalledWith({
      connectionId: 'local',
      path: '/api/sessions/s1?profile=arthur',
      profile: 'arthur'
    })
  })
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
