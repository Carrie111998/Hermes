import { atom } from 'nanostores'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const STORAGE_KEY = 'hermes.desktop.terminals.v1'

async function loadTerminalStore() {
  const $currentCwd = atom('/workspace')
  const $activeGatewayProfile = atom('default')

  const $activeGatewayIdentity = atom<{ connectionId: null | string; profile: string }>({
    connectionId: null,
    profile: 'default'
  })

  vi.doMock('@/store/session', () => ({
    $currentCwd
  }))
  vi.doMock('@/store/profile', () => ({
    $activeGatewayProfile,
    normalizeProfileKey: (name: string | null | undefined) => name?.trim() || 'default'
  }))
  vi.doMock('@/store/gateway', () => ({
    $activeGatewayIdentity
  }))

  return {
    ...(await import('./terminals')),
    $activeGatewayProfile,
    $currentCwd,
    setActiveGatewayIdentity: (connectionId: null | string, profile: string) => {
      $activeGatewayIdentity.set({ connectionId, profile })
      $activeGatewayProfile.set(profile)
    }
  }
}

describe('terminal store persistence', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('restores user tabs, active tab, and history on module load', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'term-two',
        terminals: [
          {
            auto: false,
            connectionId: 'homelab',
            cwd: '/repo/one',
            id: 'term-one',
            profile: 'venture',
            reviveBuffer: 'last output',
            title: 'zsh'
          },
          { auto: true, cwd: '/repo/two', id: 'term-two', title: 'Terminal' }
        ]
      })
    )

    const { $activeTerminalId, $terminals } = await loadTerminalStore()

    expect($activeTerminalId.get()).toBe('term-two')
    expect($terminals.get()).toEqual([
      {
        auto: false,
        connectionId: 'homelab',
        cwd: '/repo/one',
        id: 'term-one',
        kind: 'user',
        profile: 'venture',
        reviveBuffer: 'last output',
        title: 'zsh'
      },
      { auto: true, cwd: '/repo/two', id: 'term-two', kind: 'user', title: 'Terminal' }
    ])
  })

  it('persists user tabs and history synchronously, skipping agent mirrors', async () => {
    const { createTerminal, ensureAgentTerminal, renameTerminal, selectTerminal, updateTerminalReviveBuffer } =
      await loadTerminalStore()

    const userId = createTerminal('/repo')
    renameTerminal(userId, 'server')
    updateTerminalReviveBuffer(userId, 'recent scrollback')
    ensureAgentTerminal('proc-1', 'background task')
    selectTerminal(userId)

    // No flush/tick: persistence is synchronous, so the snapshot is already on
    // disk (this is what makes app-quit restore reliable).
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({
      activeTerminalId: userId,
      terminals: [
        { auto: false, cwd: '/repo', id: userId, profile: 'default', reviveBuffer: 'recent scrollback', title: 'server' }
      ]
    })
  })

  it('binds new tabs to the active gateway profile and persists the route', async () => {
    const { $terminals, createTerminal, setActiveGatewayIdentity } = await loadTerminalStore()
    setActiveGatewayIdentity(null, 'venture')

    const id = createTerminal('/remote/repo')

    expect($terminals.get().find(term => term.id === id)?.profile).toBe('venture')
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}').terminals[0].profile).toBe('venture')
  })

  it('persists the full registry backend identity for a new tab', async () => {
    const { $terminals, createTerminal, setActiveGatewayIdentity } = await loadTerminalStore()
    setActiveGatewayIdentity('homelab', 'venture')

    const id = createTerminal('/remote/repo')

    expect($terminals.get().find(term => term.id === id)).toMatchObject({
      connectionId: 'homelab',
      profile: 'venture'
    })
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}').terminals[0]).toMatchObject({
      connectionId: 'homelab',
      profile: 'venture'
    })
  })

  it('migrates a pre-profile tab to the full identity once without rebinding it', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'legacy',
        terminals: [{ auto: true, cwd: '/repo', id: 'legacy', title: 'Terminal' }]
      })
    )
    const { $terminals, bindTerminalIdentity } = await loadTerminalStore()

    bindTerminalIdentity('legacy', { connectionId: 'homelab', profile: 'venture' })
    bindTerminalIdentity('legacy', { connectionId: 'other', profile: 'jemma' })

    expect($terminals.get()[0]).toMatchObject({ connectionId: 'homelab', profile: 'venture' })
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}').terminals[0]).toMatchObject({
      connectionId: 'homelab',
      profile: 'venture'
    })
  })

  it('binds every legacy tab before the workspace can start their sessions', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'legacy-a',
        terminals: [
          { auto: true, cwd: '/a', id: 'legacy-a', title: 'Terminal' },
          { auto: true, cwd: '/b', id: 'legacy-b', title: 'Terminal' }
        ]
      })
    )
    const { $terminals, bindLegacyTerminalIdentities } = await loadTerminalStore()

    bindLegacyTerminalIdentities({ connectionId: 'homelab', profile: 'venture' })
    bindLegacyTerminalIdentities({ connectionId: 'other', profile: 'jemma' })

    expect($terminals.get()).toEqual([
      expect.objectContaining({ id: 'legacy-a', connectionId: 'homelab', profile: 'venture' }),
      expect.objectContaining({ id: 'legacy-b', connectionId: 'homelab', profile: 'venture' })
    ])
  })

  it('keeps profile-only persisted tabs on the legacy/local route', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'legacy-local',
        terminals: [{ auto: true, cwd: '/repo', id: 'legacy-local', profile: 'venture', title: 'Terminal' }]
      })
    )
    const { $activeTerminalId, $terminals, ensureTerminal, setActiveGatewayIdentity } = await loadTerminalStore()
    setActiveGatewayIdentity('homelab', 'venture')

    ensureTerminal()

    expect($activeTerminalId.get()).not.toBe('legacy-local')
    expect($terminals.get()).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: 'legacy-local', profile: 'venture' }),
        expect.objectContaining({ connectionId: 'homelab', profile: 'venture' })
      ])
    )
  })

  it('keeps explicit local separate from a legacy route for the same profile', async () => {
    const { $activeTerminalId, $terminals, createTerminal, ensureTerminal } = await loadTerminalStore()
    const legacyId = createTerminal('/legacy', { connectionId: null, profile: 'default' })

    ensureTerminal({ connectionId: 'local', profile: 'default' })

    expect($activeTerminalId.get()).not.toBe(legacyId)
    expect($terminals.get()).toEqual(
      expect.arrayContaining([
        expect.objectContaining({ id: legacyId, profile: 'default' }),
        expect.objectContaining({ connectionId: 'local', profile: 'default' })
      ])
    )
  })

  it('selects the active profile terminal and creates one when that profile has none', async () => {
    const { $activeTerminalId, $terminals, createTerminal, ensureTerminal, setActiveGatewayIdentity } =
      await loadTerminalStore()

    const localId = createTerminal('/windows')
    setActiveGatewayIdentity(null, 'venture')

    ensureTerminal({ connectionId: null, profile: 'venture' })
    const remoteId = $activeTerminalId.get()

    expect(remoteId).not.toBe(localId)
    expect($terminals.get().find(term => term.id === remoteId)?.profile).toBe('venture')

    setActiveGatewayIdentity(null, 'default')
    ensureTerminal({ connectionId: null, profile: 'default' })
    expect($activeTerminalId.get()).toBe(localId)
  })

  it('keeps an explicitly surfaced agent tab active across profile changes', async () => {
    const { $activeTerminalId, ensureAgentTerminal, ensureTerminal, selectTerminal } = await loadTerminalStore()
    const agentId = ensureAgentTerminal('proc-1', 'background task')!
    selectTerminal(agentId)

    ensureTerminal({ connectionId: null, profile: 'venture' })

    expect($activeTerminalId.get()).toBe(agentId)
  })

  it('never attaches a revive buffer to an agent tab', async () => {
    const { $terminals, ensureAgentTerminal, updateTerminalReviveBuffer } = await loadTerminalStore()

    const agentId = ensureAgentTerminal('proc-1', 'background task')!
    updateTerminalReviveBuffer(agentId, 'should be ignored')

    expect($terminals.get().find(term => term.id === agentId)?.reviveBuffer).toBeUndefined()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('tail-trims an oversized revive buffer to stay under the storage budget', async () => {
    const { $terminals, createTerminal, updateTerminalReviveBuffer } = await loadTerminalStore()

    const userId = createTerminal('/repo')
    const huge = 'x'.repeat(60_000)
    updateTerminalReviveBuffer(userId, huge)

    const stored = $terminals.get().find(term => term.id === userId)?.reviveBuffer ?? ''
    expect(stored.length).toBe(48_000)
    expect(stored).toBe(huge.slice(-48_000))
  })

  it('clears remembered tabs when all terminals close', async () => {
    const { closeAllTerminals, createTerminal } = await loadTerminalStore()

    createTerminal('/repo')
    expect(window.localStorage.getItem(STORAGE_KEY)).not.toBeNull()

    closeAllTerminals()
    expect(window.localStorage.getItem(STORAGE_KEY)).toBeNull()
  })

  it('restores and persists the last observed cwd so a reopened tab lands where the user cd-d', async () => {
    window.localStorage.setItem(
      STORAGE_KEY,
      JSON.stringify({
        activeTerminalId: 'term-one',
        terminals: [{ auto: false, cwd: '/repo', id: 'term-one', restoreCwd: '/repo/packages/api', title: 'zsh' }]
      })
    )

    const { $terminals, updateTerminalRestoreCwd } = await loadTerminalStore()

    expect($terminals.get()[0]?.restoreCwd).toBe('/repo/packages/api')

    updateTerminalRestoreCwd('term-one', '/repo/packages/web')
    expect($terminals.get()[0]?.restoreCwd).toBe('/repo/packages/web')
    expect(JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? '{}').terminals[0].restoreCwd).toBe(
      '/repo/packages/web'
    )
  })

  it('never attaches a restore cwd to an agent tab and ignores empty values', async () => {
    const { $terminals, createTerminal, ensureAgentTerminal, updateTerminalRestoreCwd } = await loadTerminalStore()

    const userId = createTerminal('/repo')
    const agentId = ensureAgentTerminal('proc-1', 'background task')!

    updateTerminalRestoreCwd(agentId, '/somewhere')
    updateTerminalRestoreCwd(userId, '   ')

    expect($terminals.get().find(term => term.id === agentId)?.restoreCwd).toBeUndefined()
    expect($terminals.get().find(term => term.id === userId)?.restoreCwd).toBeUndefined()
  })
})

describe('session cwd → terminal tab linking', () => {
  beforeEach(() => {
    window.localStorage.clear()
    vi.resetModules()
  })

  it('re-selects the tab already pointed at the new session cwd (trailing slash tolerated)', async () => {
    const { $activeTerminalId, $currentCwd, createTerminal } = await loadTerminalStore()

    const repoTab = createTerminal('/repo')
    const otherTab = createTerminal('/elsewhere')
    expect($activeTerminalId.get()).toBe(otherTab)

    $currentCwd.set('/repo/')
    expect($activeTerminalId.get()).toBe(repoTab)
  })

  it('matches the live shell cwd (restoreCwd) over the launch dir', async () => {
    const { $activeTerminalId, $currentCwd, createTerminal, updateTerminalRestoreCwd } = await loadTerminalStore()

    const movedTab = createTerminal('/repo')
    updateTerminalRestoreCwd(movedTab, '/repo/packages/api')
    const otherTab = createTerminal('/elsewhere')
    expect($activeTerminalId.get()).toBe(otherTab)

    $currentCwd.set('/repo/packages/api')
    expect($activeTerminalId.get()).toBe(movedTab)

    // The launch dir no longer describes where that shell lives.
    $currentCwd.set('/repo')
    expect($activeTerminalId.get()).toBe(movedTab)
  })

  it('leaves the active tab alone when no tab lives in the session cwd or the cwd is empty', async () => {
    const { $activeTerminalId, $currentCwd, createTerminal } = await loadTerminalStore()

    createTerminal('/repo')
    const activeTab = createTerminal('/elsewhere')

    $currentCwd.set('/unrelated')
    expect($activeTerminalId.get()).toBe(activeTab)

    $currentCwd.set('')
    expect($activeTerminalId.get()).toBe(activeTab)
  })

  it('never selects a same-cwd terminal owned by a different profile', async () => {
    const { $activeTerminalId, $currentCwd, createTerminal, setActiveGatewayIdentity } = await loadTerminalStore()
    const localId = createTerminal('/shared')
    setActiveGatewayIdentity(null, 'venture')
    const remoteId = createTerminal('/remote')

    $currentCwd.set('/shared')

    expect($activeTerminalId.get()).toBe(remoteId)
    expect($activeTerminalId.get()).not.toBe(localId)
  })

  it('stays put when the active tab already lives in the target cwd, and never matches agent tabs', async () => {
    const { $activeTerminalId, $currentCwd, createTerminal, ensureAgentTerminal, selectTerminal } =
      await loadTerminalStore()

    const first = createTerminal('/repo')
    const second = createTerminal('/repo')
    ensureAgentTerminal('proc-1', 'background task')
    selectTerminal(second)

    // Both tabs match; the one already active keeps focus (no first-match steal).
    $currentCwd.set('/repo')
    expect($activeTerminalId.get()).toBe(second)

    selectTerminal(first)
    $currentCwd.set('/repo')
    expect($activeTerminalId.get()).toBe(first)
  })
})
