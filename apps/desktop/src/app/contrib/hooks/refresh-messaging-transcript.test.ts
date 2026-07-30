import { describe, expect, it, vi } from 'vitest'

import {
  isTranscriptRefreshScopeCurrent,
  refreshMessagingTranscriptTarget,
  resolveAuthoritativeRuntimeState,
  resolveMessagingTranscriptTargets,
  resolveOpenMessagingCandidateIds,
  resolveOpenTranscriptSurfaces,
  resolveProfileScopedStoredSession
} from './refresh-messaging-transcript'

const session = (id: string, source: null | string = null, profile = 'default') => ({
  _lineage_root_id: null,
  id,
  profile,
  source
})

describe('isTranscriptRefreshScopeCurrent', () => {
  const surfaces = [
    {
      profile: 'default',
      runtimeSessionId: 'runtime-a',
      storedSessionId: 'stored-a'
    }
  ]

  it('rejects an old response after an A -> B -> A profile round trip', () => {
    expect(
      isTranscriptRefreshScopeCurrent({
        activeProfile: 'default',
        capturedProfileEpoch: 1,
        currentProfileEpoch: 3,
        profile: 'default',
        runtimeSessionId: 'runtime-a',
        surfaces
      })
    ).toBe(false)

    expect(
      isTranscriptRefreshScopeCurrent({
        activeProfile: 'default',
        capturedProfileEpoch: 3,
        currentProfileEpoch: 3,
        profile: 'default',
        runtimeSessionId: 'runtime-a',
        surfaces
      })
    ).toBe(true)
  })
})

describe('resolveProfileScopedStoredSession', () => {
  it('queries and stamps the target profile at the production adapter boundary', async () => {
    const getSession = vi.fn().mockResolvedValue(session('shared-id', 'qqbot', 'default'))

    await expect(
      resolveProfileScopedStoredSession({
        getSession,
        profile: ' work ',
        storedSessionId: 'shared-id'
      })
    ).resolves.toMatchObject({ id: 'shared-id', profile: 'work', source: 'qqbot' })
    expect(getSession).toHaveBeenCalledWith('shared-id', 'work')
  })
})

describe('resolveAuthoritativeRuntimeState', () => {
  it('prefers reconnect-published state over a stale cache entry', () => {
    const published = { 'runtime-messaging': { busy: true } }
    const cached = new Map([['runtime-messaging', { busy: false }]])

    expect(resolveAuthoritativeRuntimeState('runtime-messaging', published, cached)).toEqual({ busy: true })
  })
})

describe('resolveOpenMessagingCandidateIds', () => {
  it('does not let another profile session with the same id classify the active surface', () => {
    expect(
      resolveOpenMessagingCandidateIds({
        knownSessions: [session('shared-id', null, 'work')],
        messagingSessions: [session('shared-id', 'qqbot', 'default')],
        profile: 'work',
        selectedStoredSessionId: 'shared-id',
        sessionTiles: []
      })
    ).toEqual([])
  })

  it('does not keep a known local session as an unresolved messaging candidate', () => {
    const primary = session('stored-primary')

    expect(
      resolveOpenMessagingCandidateIds({
        knownSessions: [primary],
        messagingSessions: [],
        profile: 'default',
        selectedStoredSessionId: primary.id,
        sessionTiles: []
      })
    ).toEqual([])
  })

  it('keeps an open tile as a refresh candidate when its metadata is outside the capped sidebar slice', () => {
    expect(
      resolveOpenMessagingCandidateIds({
        knownSessions: [],
        messagingSessions: [],
        profile: 'default',
        selectedStoredSessionId: null,
        sessionTiles: [{ storedSessionId: 'stored-outside-sidebar-page' }]
      })
    ).toEqual(['stored-outside-sidebar-page'])
  })

  it('includes a messaging tile when the primary view is a non-messaging session', () => {
    const primary = session('stored-primary')
    const messaging = session('stored-messaging', 'qqbot')

    expect(
      resolveOpenMessagingCandidateIds({
        knownSessions: [primary],
        messagingSessions: [messaging],
        profile: 'default',
        selectedStoredSessionId: primary.id,
        sessionTiles: [{ storedSessionId: messaging.id }]
      })
    ).toEqual([messaging.id])
  })

  it('deduplicates a messaging session shown in both the primary view and a tile', () => {
    const messaging = session('stored-messaging', 'qqbot')

    expect(
      resolveOpenMessagingCandidateIds({
        knownSessions: [],
        messagingSessions: [messaging],
        profile: 'default',
        selectedStoredSessionId: messaging.id,
        sessionTiles: [{ storedSessionId: messaging.id }]
      })
    ).toEqual([messaging.id])
  })

  it('deduplicates lineage-root and live-tip aliases to the live stored id', () => {
    const messaging = {
      _lineage_root_id: 'stored-root',
      id: 'stored-tip',
      profile: 'default',
      source: 'qqbot'
    }

    expect(
      resolveOpenMessagingCandidateIds({
        knownSessions: [],
        messagingSessions: [messaging],
        profile: 'default',
        selectedStoredSessionId: 'stored-root',
        sessionTiles: [{ storedSessionId: 'stored-tip' }]
      })
    ).toEqual(['stored-tip'])
  })
})

describe('resolveOpenTranscriptSurfaces', () => {
  it('uses direct primary and tile runtime bindings without a stored-id reverse lookup', () => {
    expect(
      resolveOpenTranscriptSurfaces({
        activeRuntimeSessionId: 'runtime-primary',
        profile: 'work',
        selectedStoredSessionId: 'stored-primary-root',
        sessionTiles: [
          {
            runtimeId: 'runtime-tile',
            storedSessionId: 'stored-tile-root'
          }
        ]
      })
    ).toEqual([
      { profile: 'work', runtimeSessionId: 'runtime-primary', storedSessionId: 'stored-primary-root' },
      { profile: 'work', runtimeSessionId: 'runtime-tile', storedSessionId: 'stored-tile-root' }
    ])
  })
})

describe('resolveMessagingTranscriptTargets', () => {
  it('resolves an open messaging tile whose metadata is outside the capped sidebar slice', async () => {
    const resolved = {
      _lineage_root_id: 'stored-root',
      id: 'stored-tip',
      profile: 'work',
      source: 'qqbot'
    }

    const resolveStoredSession = vi.fn(async () => resolved)

    const targets = await resolveMessagingTranscriptTargets({
      getRuntimeState: () => ({ busy: false, storedSessionId: 'stored-tip' }),
      resolveStoredSession,
      sessionRows: [],
      surfaces: [{ profile: 'work', runtimeSessionId: 'runtime-tile', storedSessionId: 'stored-root' }]
    })

    expect(resolveStoredSession).toHaveBeenCalledWith('stored-tip', 'work')
    expect(targets).toEqual([
      {
        key: 'work:stored-root',
        profile: 'work',
        runtimeSessionIds: ['runtime-tile'],
        session: resolved,
        storedSessionId: 'stored-tip'
      }
    ])
  })

  it('selects only the row owned by the open surface profile when stored ids collide', async () => {
    const defaultRow = session('shared-id', 'qqbot', 'default')
    const workRow = session('shared-id', 'qqbot', 'work')
    const resolveStoredSession = vi.fn()

    const targets = await resolveMessagingTranscriptTargets({
      getRuntimeState: () => ({ busy: false, storedSessionId: 'shared-id' }),
      resolveStoredSession,
      sessionRows: [defaultRow, workRow],
      surfaces: [{ profile: 'work', runtimeSessionId: 'runtime-work', storedSessionId: 'shared-id' }]
    })

    expect(resolveStoredSession).not.toHaveBeenCalled()
    expect(targets).toEqual([
      {
        key: 'work:shared-id',
        profile: 'work',
        runtimeSessionIds: ['runtime-work'],
        session: workRow,
        storedSessionId: 'shared-id'
      }
    ])
  })

  it('rejects exact metadata returned from another profile', async () => {
    const resolveStoredSession = vi.fn(async () => session('shared-id', 'qqbot', 'default'))

    const targets = await resolveMessagingTranscriptTargets({
      getRuntimeState: () => ({ busy: false, storedSessionId: 'shared-id' }),
      resolveStoredSession,
      sessionRows: [],
      surfaces: [{ profile: 'work', runtimeSessionId: 'runtime-work', storedSessionId: 'shared-id' }]
    })

    expect(resolveStoredSession).toHaveBeenCalledWith('shared-id', 'work')
    expect(targets).toEqual([])
  })

  it('prefers the runtime-published live tip over a stale durable-root cache hit', async () => {
    const stale = {
      _lineage_root_id: 'stored-root',
      id: 'stored-old-tip',
      profile: 'default',
      source: 'qqbot'
    }

    const live = { ...stale, id: 'stored-live-tip' }
    const resolveStoredSession = vi.fn(async () => live)

    const targets = await resolveMessagingTranscriptTargets({
      getRuntimeState: () => ({ busy: false, storedSessionId: 'stored-live-tip' }),
      resolveStoredSession,
      sessionRows: [stale],
      surfaces: [{ profile: 'default', runtimeSessionId: 'runtime-tile', storedSessionId: 'stored-root' }]
    })

    expect(resolveStoredSession).toHaveBeenCalledWith('stored-live-tip', 'default')
    expect(targets[0]?.storedSessionId).toBe('stored-live-tip')
  })

  it('keeps refreshing when a tile retains an intermediate continuation tip', async () => {
    const live = {
      _lineage_root_id: 'stored-root',
      id: 'stored-live-tip',
      profile: 'default',
      source: 'qqbot'
    }

    const targets = await resolveMessagingTranscriptTargets({
      getRuntimeState: () => ({ busy: false, storedSessionId: 'stored-live-tip' }),
      resolveStoredSession: vi.fn(),
      sessionRows: [live],
      surfaces: [{ profile: 'default', runtimeSessionId: 'runtime-tile', storedSessionId: 'stored-intermediate-tip' }]
    })

    expect(targets[0]?.runtimeSessionIds).toEqual(['runtime-tile'])
    expect(targets[0]?.storedSessionId).toBe('stored-live-tip')
  })

  it('groups root/tip aliases while preserving every renderer runtime', async () => {
    const resolved = {
      _lineage_root_id: 'stored-root',
      id: 'stored-tip',
      profile: 'default',
      source: 'qqbot'
    }

    const targets = await resolveMessagingTranscriptTargets({
      getRuntimeState: runtimeSessionId => ({
        busy: false,
        storedSessionId: runtimeSessionId === 'runtime-root' ? 'stored-root' : 'stored-tip'
      }),
      resolveStoredSession: vi.fn(),
      sessionRows: [resolved],
      surfaces: [
        { profile: 'default', runtimeSessionId: 'runtime-root', storedSessionId: 'stored-root' },
        { profile: 'default', runtimeSessionId: 'runtime-tip', storedSessionId: 'stored-tip' }
      ]
    })

    expect(targets).toHaveLength(1)
    expect(targets[0]?.runtimeSessionIds).toEqual(['runtime-root', 'runtime-tip'])
    expect(targets[0]?.storedSessionId).toBe('stored-tip')
  })

  it('does not resolve metadata for a runtime published as busy', async () => {
    const resolveStoredSession = vi.fn()

    const targets = await resolveMessagingTranscriptTargets({
      getRuntimeState: () => ({ busy: true, storedSessionId: 'stored-messaging' }),
      resolveStoredSession,
      sessionRows: [],
      surfaces: [{ profile: 'default', runtimeSessionId: 'runtime-busy', storedSessionId: 'stored-messaging' }]
    })

    expect(targets).toEqual([])
    expect(resolveStoredSession).not.toHaveBeenCalled()
  })
})

describe('refreshMessagingTranscriptTarget', () => {
  const target = {
    key: 'default:stored-root',
    profile: 'default',
    runtimeSessionIds: ['runtime-messaging'],
    session: {
      _lineage_root_id: 'stored-root',
      id: 'stored-tip',
      profile: 'default',
      source: 'qqbot'
    },
    storedSessionId: 'stored-tip'
  }

  function deferred<T>() {
    let resolve!: (value: T) => void

    const promise = new Promise<T>(next => {
      resolve = next
    })

    return { promise, resolve }
  }

  it('drops a response when the authoritative runtime becomes busy during the request', async () => {
    const response = deferred<{ signature: string }>()
    const state = { busy: false, storedSessionId: 'stored-tip' }
    const commit = vi.fn()

    const refresh = refreshMessagingTranscriptTarget({
      commit,
      generationByTarget: new Map(),
      getCurrentRuntimeState: () => state,
      getSignature: value => value.signature,
      isRuntimeOpen: () => true,
      loadTranscript: () => response.promise,
      signatureByRuntimeId: new Map(),
      target
    })

    state.busy = true
    response.resolve({ signature: 'new' })
    await refresh

    expect(commit).not.toHaveBeenCalled()
  })

  it('allows only the latest overlapping response to commit', async () => {
    const firstResponse = deferred<{ signature: string }>()
    const secondResponse = deferred<{ signature: string }>()

    const loadTranscript = vi
      .fn<() => Promise<{ signature: string }>>()
      .mockImplementationOnce(() => firstResponse.promise)
      .mockImplementationOnce(() => secondResponse.promise)

    const commit = vi.fn()

    const options = {
      commit,
      generationByTarget: new Map<string, number>(),
      getCurrentRuntimeState: () => ({ busy: false, storedSessionId: 'stored-tip' }),
      getSignature: (value: { signature: string }) => value.signature,
      isRuntimeOpen: () => true,
      loadTranscript,
      signatureByRuntimeId: new Map<string, string>(),
      target
    }

    const first = refreshMessagingTranscriptTarget(options)
    const second = refreshMessagingTranscriptTarget(options)

    secondResponse.resolve({ signature: 'new' })
    await second
    firstResponse.resolve({ signature: 'old' })
    await first

    expect(commit).toHaveBeenCalledTimes(1)
    expect(commit).toHaveBeenCalledWith(
      'runtime-messaging',
      { busy: false, storedSessionId: 'stored-tip' },
      { signature: 'new' }
    )
  })

  it('tracks transcript signatures independently for each renderer runtime', async () => {
    const twoRuntimeTarget = { ...target, runtimeSessionIds: ['runtime-current', 'runtime-stale'] }
    const commit = vi.fn()

    await refreshMessagingTranscriptTarget({
      commit,
      generationByTarget: new Map(),
      getCurrentRuntimeState: () => ({ busy: false, storedSessionId: 'stored-tip' }),
      getSignature: value => value.signature,
      isRuntimeOpen: () => true,
      loadTranscript: async () => ({ signature: 'same-server-transcript' }),
      signatureByRuntimeId: new Map([['default:stored-root:runtime-current', 'same-server-transcript']]),
      target: twoRuntimeTarget
    })

    expect(commit).toHaveBeenCalledTimes(1)
    expect(commit).toHaveBeenCalledWith(
      'runtime-stale',
      { busy: false, storedSessionId: 'stored-tip' },
      { signature: 'same-server-transcript' }
    )
  })

  it('does not reuse a signature when the same runtime is rebound to another lineage', async () => {
    const commit = vi.fn()

    await refreshMessagingTranscriptTarget({
      commit,
      generationByTarget: new Map(),
      getCurrentRuntimeState: () => ({ busy: false, storedSessionId: 'stored-tip' }),
      getSignature: value => value.signature,
      isRuntimeOpen: () => true,
      loadTranscript: async () => ({ signature: 'same-server-transcript' }),
      signatureByRuntimeId: new Map([['runtime-messaging', 'same-server-transcript']]),
      target
    })

    expect(commit).toHaveBeenCalledTimes(1)
  })
})
