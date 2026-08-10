import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const DEFAULT_TREE = {
  type: 'split' as const,
  id: 'test-default-root',
  orientation: 'row' as const,
  weights: [0.3, 0.7],
  children: [
    { type: 'group' as const, id: 'test-default-sessions', panes: ['sessions'], active: 'sessions' },
    { type: 'group' as const, id: 'test-default-workspace', panes: ['workspace'], active: 'workspace' }
  ]
}

const OWNED_STORAGE_KEYS = [
  'hermes.desktop.layoutPreset.active',
  'hermes.desktop.composerPopout.zones.v1',
  'hermes.desktop.dismissedPanes.v1',
  'hermes.desktop.layoutTree.v2',
  'hermes.desktop.paneStates.v1',
  'hermes.desktop.panesFlipped',
  'hermes.desktop.reviewOpen',
  'hermes.desktop.rightRailActiveTab',
  'hermes.desktop.statusbarVisible',
  'hermes.desktop.terminalTakeover',
  'hermes.desktop.toolView.technical',
  'hermes.desktop.userPlacedPanes.v1'
]

const RAN_MODE_BACKUP_KEY = 'hermes.desktop.ranMode.v1.backup'
const COMPOSER_POPOUT_STORAGE_KEY = 'hermes.desktop.composerPopout.zones.v1'

function backupPayload(value: null | string): null | Record<string, unknown> {
  if (value === null) {
    return null
  }

  try {
    const envelope = JSON.parse(value) as { payload?: unknown }

    return typeof envelope.payload === 'string' ? (JSON.parse(envelope.payload) as Record<string, unknown>) : null
  } catch {
    return null
  }
}

function captureOwnedStorageFingerprint() {
  return JSON.stringify(OWNED_STORAGE_KEYS.map(key => [key, window.localStorage.getItem(key)]))
}

async function loadHarness() {
  const presets = await import('@/components/pane-shell/tree/presets')
  const tree = await import('@/components/pane-shell/tree/store')
  const composer = await import('@/store/composer-popout')
  const layout = await import('@/store/layout')
  const panes = await import('@/store/panes')
  const statusbar = await import('@/store/statusbar-prefs')
  const tools = await import('@/store/tool-view')
  const review = await import('@/store/review')
  const terminal = await import('@/app/right-sidebar/store')
  const ran = await import('./ran-mode')

  tree.declareDefaultTree(DEFAULT_TREE)
  panes.ensurePaneRegistered('chat-sidebar', { open: true })
  panes.ensurePaneRegistered('file-browser', { open: false })

  return { composer, layout, panes, presets, ran, review, statusbar, terminal, tools, tree }
}

function installQueuedLockHarness() {
  let activeLocks = 0
  let maxActiveLocks = 0
  let releaseFirst!: () => void
  let firstRequestStarted!: () => void

  const firstStarted = new Promise<void>(resolve => {
    firstRequestStarted = resolve
  })

  const firstGate = new Promise<void>(resolve => {
    releaseFirst = resolve
  })

  const requests: Array<{ mode: LockOptions['mode']; name: string }> = []
  let tail = Promise.resolve()

  Object.defineProperty(navigator, 'locks', {
    configurable: true,
    value: {
      request: (name: string, options: LockOptions, callback: () => Promise<boolean>) => {
        const requestIndex = requests.length
        const predecessor = tail

        requests.push({ mode: options.mode, name })

        let releaseSlot!: () => void

        tail = new Promise<void>(resolve => {
          releaseSlot = resolve
        })

        return predecessor.then(async () => {
          activeLocks += 1
          maxActiveLocks = Math.max(maxActiveLocks, activeLocks)

          try {
            if (requestIndex === 0) {
              firstRequestStarted()
              await firstGate
            }

            return await callback()
          } finally {
            activeLocks -= 1
            releaseSlot()
          }
        })
      }
    }
  })

  return {
    activeLocks: () => activeLocks,
    firstStarted,
    idle: () => tail,
    maxActiveLocks: () => maxActiveLocks,
    releaseFirst,
    requests
  }
}

beforeEach(() => {
  Object.defineProperty(navigator, 'locks', {
    configurable: true,
    value: { request: async (_name: string, _options: LockOptions, callback: () => Promise<boolean>) => callback() }
  })
  window.localStorage.clear()
  vi.resetModules()
})

afterEach(() => {
  Reflect.deleteProperty(navigator, 'locks')
  window.localStorage.clear()
  vi.restoreAllMocks()
  vi.doUnmock('@/store/windows')
  vi.resetModules()
})

describe('Ran Mode state transaction', () => {
  it('leaves upstream sidebar grouping, ordering, archive, and filters unchanged across a Ran cycle', async () => {
    const { layout, ran } = await loadHarness()

    layout.setSidebarGrouping('project')
    layout.setSidebarOrdering('cost')
    layout.setSidebarShowArchived(true)
    layout.toggleSidebarStatusFilter('working')
    layout.toggleSidebarProjectFilter('project-1')
    layout.toggleSidebarPrFilter('open')
    layout.$sidebarRowMeta.set(['pr', 'tokens'])

    const captureSidebarView = () => ({
      filtersActive: layout.$sidebarFiltersActive.get(),
      grouping: layout.$sidebarGrouping.get(),
      ordering: layout.$sidebarOrdering.get(),
      prFilter: layout.$sidebarPrFilter.get(),
      projectFilter: layout.$sidebarProjectFilter.get(),
      rowMeta: layout.$sidebarRowMeta.get(),
      showArchived: layout.$sidebarShowArchived.get(),
      statusFilter: layout.$sidebarStatusFilter.get()
    })

    const upstreamView = {
      filtersActive: true,
      grouping: 'project',
      ordering: 'cost',
      prFilter: ['open'],
      projectFilter: ['project-1'],
      rowMeta: ['pr', 'tokens'],
      showArchived: true,
      statusFilter: ['working']
    }

    expect(captureSidebarView()).toEqual(upstreamView)
    expect(await ran.enableRanMode()).toBe(true)
    expect(captureSidebarView()).toEqual(upstreamView)
    expect(await ran.disableRanMode()).toBe(true)
    expect(captureSidebarView()).toEqual(upstreamView)
  })

  it('writes the shared journal only while holding the cross-window exclusive lock', async () => {
    const nativeSetItem = Storage.prototype.setItem
    const originalLocks = Object.getOwnPropertyDescriptor(navigator, 'locks')
    let lockHeld = false
    let lockRequests = 0
    let requestedMode: LockOptions['mode']
    let requestedName: string | null = null

    Object.defineProperty(navigator, 'locks', {
      configurable: true,
      value: {
        request: async (name: string, options: LockOptions, callback: () => Promise<boolean>) => {
          lockRequests += 1
          requestedMode = options.mode
          requestedName = name
          lockHeld = true

          try {
            return await callback()
          } finally {
            lockHeld = false
          }
        }
      }
    })

    try {
      const { ran } = await loadHarness()

      vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
        if ((key === ran.RAN_MODE_STORAGE_KEY || key === RAN_MODE_BACKUP_KEY) && !lockHeld) {
          throw new DOMException('journal write escaped the exclusive lock', 'InvalidStateError')
        }

        nativeSetItem.call(this, key, value)
      })

      expect(await ran.enableRanMode()).toBe(true)
      expect(lockRequests).toBe(1)
      expect(requestedName).toBe(ran.RAN_MODE_LOCK_NAME)
      expect(requestedMode).toBe('exclusive')
    } finally {
      if (originalLocks) {
        Object.defineProperty(navigator, 'locks', originalLocks)
      } else {
        Reflect.deleteProperty(navigator, 'locks')
      }
    }
  })

  it('serializes two renderer instances that enable from the same empty journal', async () => {
    let activeLocks = 0
    let maxActiveLocks = 0
    let releaseFirst!: () => void
    let firstRequestStarted!: () => void

    const firstStarted = new Promise<void>(resolve => {
      firstRequestStarted = resolve
    })

    const firstGate = new Promise<void>(resolve => {
      releaseFirst = resolve
    })

    let requestCount = 0
    let tail = Promise.resolve()

    Object.defineProperty(navigator, 'locks', {
      configurable: true,
      value: {
        request: (_name: string, _options: LockOptions, callback: () => Promise<boolean>) => {
          const requestIndex = requestCount++
          const predecessor = tail
          let releaseSlot!: () => void
          tail = new Promise<void>(resolve => {
            releaseSlot = resolve
          })

          return predecessor.then(async () => {
            activeLocks += 1
            maxActiveLocks = Math.max(maxActiveLocks, activeLocks)

            try {
              if (requestIndex === 0) {
                firstRequestStarted()
                await firstGate
              }

              return await callback()
            } finally {
              activeLocks -= 1
              releaseSlot()
            }
          })
        }
      }
    })

    const first = await loadHarness()
    const firstEnable = first.ran.enableRanMode()
    await firstStarted

    vi.resetModules()
    const second = await loadHarness()
    const secondEnable = second.ran.enableRanMode()

    expect(requestCount).toBe(2)
    expect(activeLocks).toBe(1)
    releaseFirst()

    await expect(Promise.all([firstEnable, secondEnable])).resolves.toEqual([true, false])
    expect(maxActiveLocks).toBe(1)
    expect(JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      enabled: true,
      phase: 'active'
    })
  })

  it('serializes ordinary preset decisions through the Ran journal authority', async () => {
    let lockRequests = 0

    Object.defineProperty(navigator, 'locks', {
      configurable: true,
      value: {
        request: async (_name: string, _options: LockOptions, callback: () => Promise<boolean>) => {
          lockRequests += 1

          return callback()
        }
      }
    })

    const { presets, ran, tree } = await loadHarness()

    const chosenTree = {
      type: 'group' as const,
      id: 'serialized-preset',
      panes: ['workspace'],
      active: 'workspace'
    }

    expect(await presets.applyLayoutPreset('serialized-choice', chosenTree)).toBe(true)
    expect(lockRequests).toBe(1)
    expect(tree.$activePresetId.get()).toBe('serialized-choice')
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('keeps a newer enabled transaction after a queued peer disables the prior transaction', async () => {
    const first = await loadHarness()

    expect(await first.ran.enableRanMode()).toBe(true)

    const firstTransactionId = JSON.parse(
      window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}'
    ).transactionId

    const locks = installQueuedLockHarness()
    const firstDisable = first.ran.disableRanMode()

    await locks.firstStarted
    vi.resetModules()

    const second = await loadHarness()
    const secondEnable = second.ran.enableRanMode()

    expect(locks.requests).toEqual([
      { mode: 'exclusive', name: first.ran.RAN_MODE_LOCK_NAME },
      { mode: 'exclusive', name: first.ran.RAN_MODE_LOCK_NAME }
    ])
    locks.releaseFirst()

    await expect(Promise.all([firstDisable, secondEnable])).resolves.toEqual([true, true])

    const durable = JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    expect(durable).toMatchObject({ enabled: true, phase: 'active' })
    expect(durable.transactionId).not.toBe(firstTransactionId)
    expect(locks.maxActiveLocks()).toBe(1)
  })

  it('cannot let completed-record cleanup remove a queued newer generation', async () => {
    const first = await loadHarness()
    const nativeRemoveItem = Storage.prototype.removeItem

    const removeSpy = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
      if (key !== first.ran.RAN_MODE_STORAGE_KEY) {
        nativeRemoveItem.call(this, key)
      }
    })

    expect(await first.ran.enableRanMode()).toBe(true)
    expect(await first.ran.disableRanMode()).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: true,
      enabled: false
    })
    removeSpy.mockRestore()

    const locks = installQueuedLockHarness()
    const cleanup = first.ran.initializeRanMode()

    await locks.firstStarted
    vi.resetModules()

    const second = await loadHarness()
    const secondEnable = second.ran.enableRanMode()

    locks.releaseFirst()
    await expect(Promise.all([cleanup, secondEnable])).resolves.toEqual([false, true])
    expect(JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      enabled: true,
      phase: 'active'
    })
    expect(locks.maxActiveLocks()).toBe(1)
  })

  it('serializes malformed cleanup before a queued valid generation is created', async () => {
    window.localStorage.setItem('hermes.desktop.ranMode.v1', '{"version":1')

    const locks = installQueuedLockHarness()
    const first = await loadHarness()
    const cleanup = first.ran.initializeRanMode()

    await locks.firstStarted
    vi.resetModules()

    const second = await loadHarness()
    const secondEnable = second.ran.enableRanMode()

    locks.releaseFirst()
    await expect(Promise.all([cleanup, secondEnable])).resolves.toEqual([false, true])
    expect(JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      enabled: true,
      phase: 'active'
    })
    expect(locks.maxActiveLocks()).toBe(1)
  })

  it('rejects an overlapping opposite intent from the same renderer while its lock request is pending', async () => {
    const locks = installQueuedLockHarness()
    const { ran } = await loadHarness()
    const enable = ran.enableRanMode()

    await locks.firstStarted

    const overlappingDisable = ran.disableRanMode()

    expect(locks.requests).toEqual([{ mode: 'exclusive', name: ran.RAN_MODE_LOCK_NAME }])
    locks.releaseFirst()
    await expect(Promise.all([enable, overlappingDisable])).resolves.toEqual([true, false])
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      enabled: true,
      phase: 'active'
    })
  })

  it('fails closed without Web Locks instead of mutating the journal', async () => {
    Reflect.deleteProperty(navigator, 'locks')

    const { ran } = await loadHarness()

    expect(await ran.enableRanMode()).toBe(false)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
    expect(ran.$ranModeEnabled.get()).toBe(false)
  })

  it('captures once, applies idempotently, and restores the exact owned layout state once', async () => {
    const { panes, ran, review, statusbar, terminal, tools, tree } = await loadHarness()

    const customTree = {
      type: 'split' as const,
      id: 'custom-root',
      orientation: 'row' as const,
      weights: [0.42, 0.58],
      children: [
        { type: 'group' as const, id: 'custom-sessions', panes: ['sessions'], active: 'sessions' },
        {
          type: 'group' as const,
          id: 'custom-workspace',
          panes: ['workspace', 'file-browser'],
          active: 'file-browser'
        }
      ]
    }

    tree.applyTree(customTree, 'custom-before-ran')
    panes.$paneStates.set({
      'chat-sidebar': { open: false, widthOverride: 311 },
      'file-browser': { open: true, widthOverride: 407 }
    })
    statusbar.$statusbarVisible.set(true)
    tools.$toolViewMode.set('technical')
    review.$reviewOpen.set(true)
    terminal.$terminalTakeover.set(true)

    const before = {
      layout: tree.captureLayoutStateSnapshot(),
      panes: structuredClone(panes.$paneStates.get()),
      review: review.$reviewOpen.get(),
      statusbar: statusbar.$statusbarVisible.get(),
      terminal: terminal.$terminalTakeover.get(),
      tools: tools.$toolViewMode.get()
    }

    expect(await ran.enableRanMode()).toBe(true)
    const persistedAfterFirstEnable = window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)

    expect(ran.$ranModeEnabled.get()).toBe(true)
    expect(tree.$activePresetId.get()).toBe(ran.RAN_MODE_PRESET_ID)
    const ranTree = tree.$layoutTree.get()
    expect(ranTree?.type).toBe('split')
    expect(ranTree?.type === 'split' ? ranTree.children[0] : null).toMatchObject({
      type: 'group',
      id: 'ran-mode-sessions',
      minimized: true
    })
    expect(panes.$paneStates.get()['chat-sidebar']?.open).toBe(true)
    expect(panes.$paneStates.get()['file-browser']?.open).toBe(false)
    expect(statusbar.$statusbarVisible.get()).toBe(false)
    expect(tools.$toolViewMode.get()).toBe('product')
    expect(review.$reviewOpen.get()).toBe(false)
    expect(terminal.$terminalTakeover.get()).toBe(false)

    expect(await ran.enableRanMode()).toBe(false)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBe(persistedAfterFirstEnable)

    // Layout/pane state is snapshot-owned while the mode is active: temporary
    // expansion is allowed, but leaving Ran Mode restores the original workspace.
    tree.setTreeGroupMinimized('ran-mode-sessions', false)
    panes.setPaneOpen('file-browser', true)

    expect(await ran.disableRanMode()).toBe(true)
    expect(tree.captureLayoutStateSnapshot()).toEqual(before.layout)
    expect(panes.$paneStates.get()).toEqual(before.panes)
    expect(review.$reviewOpen.get()).toBe(before.review)
    expect(statusbar.$statusbarVisible.get()).toBe(before.statusbar)
    expect(terminal.$terminalTakeover.get()).toBe(before.terminal)
    expect(tools.$toolViewMode.get()).toBe(before.tools)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()

    const afterFirstDisable = JSON.stringify({
      layout: tree.captureLayoutStateSnapshot(),
      panes: panes.$paneStates.get()
    })

    expect(await ran.disableRanMode()).toBe(false)
    expect(JSON.stringify({ layout: tree.captureLayoutStateSnapshot(), panes: panes.$paneStates.get() })).toBe(
      afterFirstDisable
    )
  })

  it('restores composer pop-out placement after the Ran layout prunes removed live-session and contributed zones', async () => {
    const originalComposerZones = {
      'contributed-zone': { poppedOut: true, position: { bottom: 73, right: 89 } },
      'ran-mode-workspace': { poppedOut: true, position: { bottom: 31, right: 47 } },
      'session-zone-one': { poppedOut: true, position: { bottom: 41, right: 59 } },
      'session-zone-two': { poppedOut: false, position: { bottom: 53, right: 67 } }
    }

    window.localStorage.setItem(COMPOSER_POPOUT_STORAGE_KEY, JSON.stringify(originalComposerZones))

    const { composer, ran, tree } = await loadHarness()
    const { groupLeafIds } = await import('@/components/pane-shell/tree/model')

    const beforeTree = {
      type: 'split' as const,
      id: 'composer-before-ran-root',
      orientation: 'row' as const,
      weights: [1, 1, 1, 1],
      children: [
        { type: 'group' as const, id: 'session-zone-one', panes: ['session-tile:one'], active: 'session-tile:one' },
        { type: 'group' as const, id: 'session-zone-two', panes: ['session-tile:two'], active: 'session-tile:two' },
        { type: 'group' as const, id: 'ran-mode-workspace', panes: ['workspace'], active: 'workspace' },
        { type: 'group' as const, id: 'contributed-zone', panes: ['plugin-pane'], active: 'plugin-pane' }
      ]
    }

    tree.applyTree(beforeTree, 'composer-before-ran')

    const unlisten = tree.$layoutTree.subscribe(layout => {
      if (layout) {
        composer.pruneComposerPopoutZones(groupLeafIds(layout))
      }
    })

    expect(await ran.enableRanMode()).toBe(true)
    expect(composer.$composerPopoutZones.get()).toEqual({
      'ran-mode-workspace': originalComposerZones['ran-mode-workspace']
    })
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe(
      JSON.stringify({ 'ran-mode-workspace': originalComposerZones['ran-mode-workspace'] })
    )

    expect(await ran.disableRanMode()).toBe(true)
    expect(composer.$composerPopoutZones.get()).toEqual(originalComposerZones)
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe(JSON.stringify(originalComposerZones))
    expect(groupLeafIds(tree.$layoutTree.get()!)).toEqual(
      expect.arrayContaining(['session-zone-one', 'session-zone-two', 'ran-mode-workspace', 'contributed-zone'])
    )

    unlisten()
  })

  it.each([
    ['absent', null],
    ['explicitly empty', '{}']
  ])('preserves an %s composer placement through repeated enable and disable', async (_label, durableValue) => {
    if (durableValue !== null) {
      window.localStorage.setItem(COMPOSER_POPOUT_STORAGE_KEY, durableValue)
    }

    const { composer, ran } = await loadHarness()

    expect(composer.$composerPopoutZones.get()).toEqual({})
    expect(await ran.enableRanMode()).toBe(true)
    expect(await ran.enableRanMode()).toBe(false)
    expect(await ran.disableRanMode()).toBe(true)
    expect(await ran.disableRanMode()).toBe(false)
    expect(composer.$composerPopoutZones.get()).toEqual({})
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe(durableValue)
  })

  it('restores composer placement after restart while Ran Mode is active', async () => {
    const originalComposerZones = {
      'restart-removed-zone': { poppedOut: true, position: { bottom: 61, right: 79 } }
    }

    window.localStorage.setItem(COMPOSER_POPOUT_STORAGE_KEY, JSON.stringify(originalComposerZones))

    const first = await loadHarness()

    first.composer.pruneComposerPopoutZones(['restart-removed-zone'])
    expect(await first.ran.enableRanMode()).toBe(true)
    first.composer.pruneComposerPopoutZones([])
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe('{}')

    vi.resetModules()

    const restarted = await loadHarness()

    expect(await restarted.ran.initializeRanMode()).toBe(true)
    expect(restarted.composer.$composerPopoutZones.get()).toEqual({})
    expect(await restarted.ran.disableRanMode()).toBe(true)
    expect(restarted.composer.$composerPopoutZones.get()).toEqual(originalComposerZones)
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe(JSON.stringify(originalComposerZones))
  })

  it('keeps composer restoration incomplete when persistence is swallowed and retries after restart', async () => {
    const originalComposerZones = {
      'retry-removed-zone': { poppedOut: true, position: { bottom: 83, right: 97 } }
    }

    window.localStorage.setItem(COMPOSER_POPOUT_STORAGE_KEY, JSON.stringify(originalComposerZones))

    const first = await loadHarness()

    expect(await first.ran.enableRanMode()).toBe(true)
    first.composer.pruneComposerPopoutZones([])
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe('{}')

    const nativeSetItem = Storage.prototype.setItem

    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === COMPOSER_POPOUT_STORAGE_KEY) {
        throw new DOMException('composer persistence unavailable', 'QuotaExceededError')
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await first.ran.disableRanMode()).toBe(false)
    expect(first.ran.$ranModeEnabled.get()).toBe(true)
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe('{}')
    expect(JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      phase: 'inactive'
    })

    set.mockRestore()
    vi.resetModules()

    const restarted = await loadHarness()

    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(restarted.ran.$ranModeEnabled.get()).toBe(false)
    expect(restarted.composer.$composerPopoutZones.get()).toEqual(originalComposerZones)
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe(JSON.stringify(originalComposerZones))
    expect(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('keeps composer restoration incomplete when the composer store rejects its restore and retries after restart', async () => {
    const originalComposerZones = {
      'store-retry-zone': { poppedOut: true, position: { bottom: 89, right: 107 } }
    }

    window.localStorage.setItem(COMPOSER_POPOUT_STORAGE_KEY, JSON.stringify(originalComposerZones))

    const first = await loadHarness()

    expect(await first.ran.enableRanMode()).toBe(true)
    first.composer.pruneComposerPopoutZones([])

    const set = vi.spyOn(first.composer.$composerPopoutZones, 'set').mockImplementation(() => {
      throw new Error('composer store restore failed')
    })

    expect(await first.ran.disableRanMode()).toBe(false)
    expect(first.ran.$ranModeEnabled.get()).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      phase: 'inactive'
    })

    set.mockRestore()
    vi.resetModules()

    const restarted = await loadHarness()

    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(restarted.composer.$composerPopoutZones.get()).toEqual(originalComposerZones)
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe(JSON.stringify(originalComposerZones))
    expect(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('restores composer persistence only while holding the existing Ran Web Lock authority', async () => {
    const originalComposerZones = {
      'locked-zone': { poppedOut: true, position: { bottom: 101, right: 127 } }
    }

    let lockHeld = false

    window.localStorage.setItem(COMPOSER_POPOUT_STORAGE_KEY, JSON.stringify(originalComposerZones))
    Object.defineProperty(navigator, 'locks', {
      configurable: true,
      value: {
        request: async (_name: string, _options: LockOptions, callback: () => Promise<boolean>) => {
          lockHeld = true

          try {
            return await callback()
          } finally {
            lockHeld = false
          }
        }
      }
    })

    const first = await loadHarness()

    expect(await first.ran.enableRanMode()).toBe(true)
    first.composer.pruneComposerPopoutZones([])

    const nativeSetItem = Storage.prototype.setItem

    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === COMPOSER_POPOUT_STORAGE_KEY && value === JSON.stringify(originalComposerZones) && !lockHeld) {
        throw new DOMException('composer restore escaped the Ran lock', 'InvalidStateError')
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await first.ran.disableRanMode()).toBe(true)
    expect(first.composer.$composerPopoutZones.get()).toEqual(originalComposerZones)
    set.mockRestore()
  })

  it('keeps Reset Layout authoritative by reconciling the captured composer map to the reset tree', async () => {
    const originalComposerZones = {
      'reset-removed-zone': { poppedOut: true, position: { bottom: 137, right: 149 } }
    }

    window.localStorage.setItem(COMPOSER_POPOUT_STORAGE_KEY, JSON.stringify(originalComposerZones))

    const { composer, ran, tree } = await loadHarness()
    const { groupLeafIds } = await import('@/components/pane-shell/tree/model')

    const unlisten = tree.$layoutTree.subscribe(layout => {
      if (layout) {
        composer.pruneComposerPopoutZones(groupLeafIds(layout))
      }
    })

    expect(await ran.enableRanMode()).toBe(true)
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe('{}')
    expect(await ran.resetLayoutFromRanMode()).toBe(true)
    expect(composer.$composerPopoutZones.get()).toEqual({})
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe('{}')
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()

    unlisten()
  })

  it('preserves explicit user-owned status bar and tool-view changes made while active', async () => {
    const { ran, statusbar, tools } = await loadHarness()

    statusbar.$statusbarVisible.set(false)
    tools.$toolViewMode.set('product')
    expect(await ran.enableRanMode()).toBe(true)

    statusbar.$statusbarVisible.set(true)
    tools.$toolViewMode.set('technical')

    expect(await ran.disableRanMode()).toBe(true)
    expect(statusbar.$statusbarVisible.get()).toBe(true)
    expect(tools.$toolViewMode.get()).toBe('technical')
  })

  it('survives a module restart and restores the pre-entry state afterward', async () => {
    const first = await loadHarness()

    const customTree = {
      type: 'split' as const,
      id: 'restart-root',
      orientation: 'row' as const,
      weights: [0.36, 0.64],
      children: [
        { type: 'group' as const, id: 'restart-sessions', panes: ['sessions'], active: 'sessions' },
        { type: 'group' as const, id: 'restart-workspace', panes: ['workspace'], active: 'workspace' }
      ]
    }

    first.tree.applyTree(customTree, 'restart-custom')
    first.panes.$paneStates.set({
      'chat-sidebar': { open: true, widthOverride: 287 },
      'file-browser': { open: true, widthOverride: 333 }
    })
    first.statusbar.$statusbarVisible.set(true)
    first.tools.$toolViewMode.set('technical')
    const originalLayout = first.tree.captureLayoutStateSnapshot()
    const originalPanes = structuredClone(first.panes.$paneStates.get())

    expect(await first.ran.enableRanMode()).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      enabled: true,
      phase: 'active',
      version: 1
    })

    vi.resetModules()

    const second = await loadHarness()
    expect(await second.ran.initializeRanMode()).toBe(true)
    expect(second.ran.$ranModeEnabled.get()).toBe(true)
    expect(second.tree.$activePresetId.get()).toBe(second.ran.RAN_MODE_PRESET_ID)
    expect(second.statusbar.$statusbarVisible.get()).toBe(false)
    expect(second.tools.$toolViewMode.get()).toBe('product')

    vi.resetModules()

    const third = await loadHarness()
    expect(await third.ran.initializeRanMode()).toBe(true)
    expect(third.ran.$ranModeEnabled.get()).toBe(true)
    expect(third.tree.$activePresetId.get()).toBe(third.ran.RAN_MODE_PRESET_ID)

    expect(await third.ran.disableRanMode()).toBe(true)
    expect(third.tree.captureLayoutStateSnapshot()).toEqual(originalLayout)
    expect(third.panes.$paneStates.get()).toEqual(originalPanes)
    expect(third.statusbar.$statusbarVisible.get()).toBe(true)
    expect(third.tools.$toolViewMode.get()).toBe('technical')
  })

  it('recovers an interrupted applying phase without losing the snapshot', async () => {
    const first = await loadHarness()

    first.statusbar.$statusbarVisible.set(true)
    first.tools.$toolViewMode.set('technical')
    const originalLayout = first.tree.captureLayoutStateSnapshot()
    const originalPanes = structuredClone(first.panes.$paneStates.get())

    expect(await first.ran.enableRanMode()).toBe(true)
    const interrupted = JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')
    interrupted.phase = 'applying'
    window.localStorage.setItem(first.ran.RAN_MODE_STORAGE_KEY, JSON.stringify(interrupted))

    // Simulate a crash between durable snapshot creation and preset application.
    first.tree.restoreLayoutStateSnapshot(originalLayout)
    first.panes.$paneStates.set(originalPanes)
    first.statusbar.$statusbarVisible.set(true)
    first.tools.$toolViewMode.set('technical')

    vi.resetModules()

    const recovered = await loadHarness()
    expect(await recovered.ran.initializeRanMode()).toBe(true)
    expect(recovered.ran.$ranModeEnabled.get()).toBe(true)
    expect(recovered.tree.$activePresetId.get()).toBe(recovered.ran.RAN_MODE_PRESET_ID)
    expect(recovered.statusbar.$statusbarVisible.get()).toBe(false)
    expect(recovered.tools.$toolViewMode.get()).toBe('product')

    expect(await recovered.ran.disableRanMode()).toBe(true)
    expect(recovered.tree.captureLayoutStateSnapshot()).toEqual(originalLayout)
    expect(recovered.panes.$paneStates.get()).toEqual(originalPanes)
    expect(recovered.statusbar.$statusbarVisible.get()).toBe(true)
    expect(recovered.tools.$toolViewMode.get()).toBe('technical')
  })

  it('lets an explicit layout-preset choice exit the mode without restoring over that choice', async () => {
    const { ran, tree } = await loadHarness()
    expect(await ran.enableRanMode()).toBe(true)

    const chosenTree = {
      type: 'group' as const,
      id: 'chosen-after-ran',
      panes: ['workspace'],
      active: 'workspace'
    }

    expect(await ran.leaveRanModeForLayoutChange('chosen-layout', chosenTree)).toBe(true)

    expect(ran.$ranModeEnabled.get()).toBe(false)
    expect(tree.$activePresetId.get()).toBe('chosen-layout')
    expect(tree.$layoutTree.get()).toMatchObject({ id: 'chosen-after-ran', type: 'group' })
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('does not mutate UI state when the recovery snapshot cannot be durably written', async () => {
    const { ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()
    const nativeSetItem = Storage.prototype.setItem

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === ran.RAN_MODE_STORAGE_KEY || key === RAN_MODE_BACKUP_KEY) {
        throw new DOMException('quota exceeded', 'QuotaExceededError')
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await ran.enableRanMode()).toBe(false)
    expect(ran.$ranModeEnabled.get()).toBe(false)
    expect(tree.captureLayoutStateSnapshot()).toEqual(before)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('does not mutate UI state when a partial snapshot write fails exact readback', async () => {
    const { ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()
    const nativeSetItem = Storage.prototype.setItem

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      nativeSetItem.call(
        this,
        key,
        key === ran.RAN_MODE_STORAGE_KEY || key === RAN_MODE_BACKUP_KEY ? value.slice(0, -1) : value
      )
    })

    expect(await ran.enableRanMode()).toBe(false)
    expect(ran.$ranModeEnabled.get()).toBe(false)
    expect(tree.captureLayoutStateSnapshot()).toEqual(before)

    vi.restoreAllMocks()
    expect(await ran.initializeRanMode()).toBe(false)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('keeps the recoverable applying predecessor when active-successor staging is truncated', async () => {
    const { ran } = await loadHarness()
    const nativeSetItem = Storage.prototype.setItem
    let backupWrites = 0

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === RAN_MODE_BACKUP_KEY) {
        backupWrites += 1
        nativeSetItem.call(this, key, backupWrites === 2 ? value.slice(0, -1) : value)

        return
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await ran.enableRanMode()).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      enabled: true,
      phase: 'applying'
    })

    vi.restoreAllMocks()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(true)
    expect(restarted.ran.$ranModeEnabled.get()).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      enabled: true,
      phase: 'active'
    })
  })

  it('rejects a checksum-corrupt successor slot without retiring the applying predecessor', async () => {
    const { ran } = await loadHarness()
    const nativeSetItem = Storage.prototype.setItem
    let backupWrites = 0

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === RAN_MODE_BACKUP_KEY) {
        backupWrites += 1

        if (backupWrites === 2) {
          const envelope = JSON.parse(value) as Record<string, unknown>

          envelope.checksum = '00000000'
          nativeSetItem.call(this, key, JSON.stringify(envelope))

          return
        }
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await ran.enableRanMode()).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      enabled: true,
      phase: 'applying'
    })

    vi.restoreAllMocks()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      enabled: true,
      phase: 'active'
    })
  })

  it('ignores a stale predecessor slot after cleanup and a newer transaction generation', async () => {
    const first = await loadHarness()
    const nativeSetItem = Storage.prototype.setItem
    let staleBackup: null | string = null

    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === RAN_MODE_BACKUP_KEY && staleBackup === null && backupPayload(value)?.phase === 'active') {
        staleBackup = value
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await first.ran.enableRanMode()).toBe(true)

    const oldTransactionId = JSON.parse(
      window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}'
    ).transactionId

    expect(await first.ran.disableRanMode()).toBe(true)
    expect(await first.ran.enableRanMode()).toBe(true)
    const newer = JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    expect(newer.transactionId).not.toBe(oldTransactionId)
    expect(staleBackup).not.toBeNull()
    set.mockRestore()
    nativeSetItem.call(window.localStorage, RAN_MODE_BACKUP_KEY, staleBackup!)
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      transactionId: newer.transactionId
    })
    expect(window.localStorage.getItem(RAN_MODE_BACKUP_KEY)).toBeNull()
  })

  it('recovers a validated tombstone successor when its primary mirror is truncated', async () => {
    const { ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()
    const nativeSetItem = Storage.prototype.setItem
    let primaryWrites = 0

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === ran.RAN_MODE_STORAGE_KEY) {
        primaryWrites += 1
        nativeSetItem.call(this, key, primaryWrites >= 3 ? value.slice(0, -1) : value)

        return
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await ran.enableRanMode()).toBe(true)
    expect(await ran.disableRanMode()).toBe(false)
    expect(window.localStorage.getItem(RAN_MODE_BACKUP_KEY)).not.toBeNull()

    vi.restoreAllMocks()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(restarted.ran.$ranModeEnabled.get()).toBe(false)
    expect(restarted.tree.captureLayoutStateSnapshot()).toEqual(before)
  })

  it('recovers from a validated backup successor while primary repair remains unavailable', async () => {
    const first = await loadHarness()
    const nativeSetItem = Storage.prototype.setItem
    let primaryWrites = 0

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === first.ran.RAN_MODE_STORAGE_KEY && ++primaryWrites >= 2) {
        throw new DOMException('primary mirror unavailable', 'QuotaExceededError')
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await first.ran.enableRanMode()).toBe(true)
    expect(backupPayload(window.localStorage.getItem(RAN_MODE_BACKUP_KEY))).toMatchObject({
      enabled: true,
      phase: 'active'
    })
    vi.resetModules()

    const restarted = await loadHarness()

    expect(await restarted.ran.initializeRanMode()).toBe(true)
    expect(restarted.ran.$ranModeEnabled.get()).toBe(true)
  })

  it('preserves an incomplete tombstone when completed-successor staging is truncated', async () => {
    const { ran } = await loadHarness()
    const nativeSetItem = Storage.prototype.setItem
    let backupWrites = 0

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === RAN_MODE_BACKUP_KEY) {
        backupWrites += 1
        nativeSetItem.call(this, key, backupWrites === 4 ? value.slice(0, -1) : value)

        return
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await ran.enableRanMode()).toBe(true)
    expect(await ran.disableRanMode()).toBe(false)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      phase: 'inactive'
    })

    vi.restoreAllMocks()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(restarted.ran.$ranModeEnabled.get()).toBe(false)
  })

  it('keeps a completed predecessor when cleared-successor staging is truncated', async () => {
    const { ran } = await loadHarness()
    const nativeSetItem = Storage.prototype.setItem
    let backupWrites = 0

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === RAN_MODE_BACKUP_KEY) {
        backupWrites += 1
        nativeSetItem.call(this, key, backupWrites === 5 ? value.slice(0, -1) : value)

        return
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await ran.enableRanMode()).toBe(true)
    expect(await ran.disableRanMode()).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: true,
      enabled: false
    })

    vi.restoreAllMocks()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('completes disable safely with a durable inactive tombstone when cleanup removal fails', async () => {
    const { ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()
    expect(await ran.enableRanMode()).toBe(true)
    const nativeRemoveItem = Storage.prototype.removeItem

    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
      if (key === ran.RAN_MODE_STORAGE_KEY) {
        throw new DOMException('storage unavailable', 'SecurityError')
      }

      nativeRemoveItem.call(this, key)
    })

    expect(await ran.disableRanMode()).toBe(true)
    expect(ran.$ranModeEnabled.get()).toBe(false)
    expect(tree.captureLayoutStateSnapshot()).toEqual(before)

    const completed = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    expect(completed).toMatchObject({
      completed: true,
      enabled: false,
      phase: 'inactive',
      version: 1
    })
    expect(completed.settlementFingerprint).toBe(captureOwnedStorageFingerprint())

    const laterTree = {
      type: 'group' as const,
      id: 'later-off-mode-choice',
      panes: ['workspace'],
      active: 'workspace'
    }

    tree.applyTree(laterTree, 'later-choice')
    remove.mockRestore()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(restarted.tree.$activePresetId.get()).toBe('later-choice')
    expect(restarted.tree.$layoutTree.get()).toMatchObject({ id: 'later-off-mode-choice' })
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('does not restore a stale in-memory snapshot after another peer cleared the transaction', async () => {
    const { ran, tree } = await loadHarness()
    expect(await ran.enableRanMode()).toBe(true)
    window.localStorage.removeItem(ran.RAN_MODE_STORAGE_KEY)

    const peerTree = {
      type: 'group' as const,
      id: 'peer-layout-after-ran',
      panes: ['workspace'],
      active: 'workspace'
    }

    tree.applyTree(peerTree, 'peer-layout')

    expect(await ran.disableRanMode()).toBe(false)
    expect(ran.$ranModeEnabled.get()).toBe(false)
    expect(tree.$activePresetId.get()).toBe('peer-layout')
    expect(tree.$layoutTree.get()).toMatchObject({ id: 'peer-layout-after-ran' })
  })

  it('fails closed when neither completion nor cleanup can be persisted', async () => {
    const { ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()
    expect(await ran.enableRanMode()).toBe(true)
    const nativeSetItem = Storage.prototype.setItem
    const nativeRemoveItem = Storage.prototype.removeItem

    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === RAN_MODE_BACKUP_KEY && backupPayload(value)?.completed === true) {
        throw new DOMException('completion write unavailable', 'SecurityError')
      }

      nativeSetItem.call(this, key, value)
    })

    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
      if (key === ran.RAN_MODE_STORAGE_KEY) {
        throw new DOMException('cleanup unavailable', 'SecurityError')
      }

      nativeRemoveItem.call(this, key)
    })

    expect(await ran.disableRanMode()).toBe(false)
    expect(ran.$ranModeEnabled.get()).toBe(true)
    expect(tree.$activePresetId.get()).toBe(ran.RAN_MODE_PRESET_ID)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      phase: 'inactive'
    })

    const blockedTree = {
      type: 'group' as const,
      id: 'blocked-choice',
      panes: ['workspace'],
      active: 'workspace'
    }

    expect(await ran.leaveRanModeForLayoutChange('blocked-choice', blockedTree)).toBe(false)
    expect(await ran.resetLayoutFromRanMode()).toBe(false)
    expect(tree.$activePresetId.get()).toBe(ran.RAN_MODE_PRESET_ID)

    set.mockRestore()
    remove.mockRestore()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(restarted.tree.captureLayoutStateSnapshot()).toEqual(before)
    expect(restarted.ran.$ranModeEnabled.get()).toBe(false)
    expect(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('keeps a failed restore incomplete and retries the durable tombstone', async () => {
    const { ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()
    expect(await ran.enableRanMode()).toBe(true)
    const nativeClone = globalThis.structuredClone
    let failRestoreOnce = true

    const clone = vi.spyOn(globalThis, 'structuredClone').mockImplementation((value) => {
      if (failRestoreOnce) {
        failRestoreOnce = false
        throw new DOMException('restore failed', 'DataCloneError')
      }

      return nativeClone(value)
    })

    expect(await ran.disableRanMode()).toBe(false)
    expect(ran.$ranModeEnabled.get()).toBe(true)
    expect(tree.$activePresetId.get()).toBe(ran.RAN_MODE_PRESET_ID)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      phase: 'inactive'
    })

    clone.mockRestore()
    expect(await ran.initializeRanMode()).toBe(false)
    expect(ran.$ranModeEnabled.get()).toBe(false)
    expect(tree.captureLayoutStateSnapshot()).toEqual(before)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('keeps settlement incomplete when an owned preference fails durable readback', async () => {
    const { ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()
    expect(await ran.enableRanMode()).toBe(true)
    const nativeSetItem = Storage.prototype.setItem

    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === 'hermes.desktop.layoutTree.v2') {
        throw new DOMException('layout persistence unavailable', 'SecurityError')
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await ran.disableRanMode()).toBe(false)
    expect(ran.$ranModeEnabled.get()).toBe(true)
    expect(tree.$activePresetId.get()).toBe(ran.RAN_MODE_PRESET_ID)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      phase: 'inactive'
    })

    set.mockRestore()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(restarted.ran.$ranModeEnabled.get()).toBe(false)
    expect(restarted.tree.captureLayoutStateSnapshot()).toEqual(before)
    expect(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('does not confuse an apparent baseline ABA with completed exit recovery', async () => {
    const { ran, tree } = await loadHarness()
    const baseline = tree.captureLayoutStateSnapshot()

    const chosenTree = {
      type: 'group' as const,
      id: 'aba-newer-choice',
      panes: ['workspace'],
      active: 'workspace'
    }

    expect(await ran.enableRanMode()).toBe(true)
    const nativeSetItem = Storage.prototype.setItem

    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === RAN_MODE_BACKUP_KEY && backupPayload(value)?.completed === true) {
        throw new DOMException('completion write unavailable', 'SecurityError')
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await ran.leaveRanModeForLayoutChange('aba-choice', chosenTree)).toBe(false)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      phase: 'leaving'
    })

    // An appearance that happens to equal A is not settlement proof. Restart
    // must still replay the durable, newer exit intent B.
    tree.restoreLayoutStateSnapshot(baseline)
    expect(tree.captureLayoutStateSnapshot()).toEqual(baseline)
    set.mockRestore()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(restarted.tree.$activePresetId.get()).toBe('aba-choice')
    expect(restarted.tree.$layoutTree.get()).toMatchObject({ id: 'aba-newer-choice' })
    expect(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('ignores a delayed peer tombstone when a newer live record is durable', async () => {
    const { ran, tree } = await loadHarness()
    expect(await ran.enableRanMode()).toBe(true)
    const oldLive = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    const staleTombstone = {
      completed: false,
      enabled: false,
      phase: 'inactive',
      restorePolicy: 'conditional',
      snapshot: oldLive.snapshot,
      transactionId: oldLive.transactionId,
      version: 1
    }

    const newerLive = {
      ...oldLive,
      transactionId: 'newer-live-transaction',
      snapshot: {
        ...oldLive.snapshot,
        layout: {
          ...oldLive.snapshot.layout,
          activePresetId: 'newer-live-baseline'
        }
      }
    }

    window.localStorage.setItem(ran.RAN_MODE_STORAGE_KEY, JSON.stringify(newerLive))

    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(staleTombstone),
        oldValue: JSON.stringify(oldLive),
        storageArea: window.localStorage
      })
    )

    expect(ran.$ranModeEnabled.get()).toBe(true)
    expect(tree.$activePresetId.get()).toBe(ran.RAN_MODE_PRESET_ID)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBe(JSON.stringify(newerLive))
  })

  it('reconciles a delayed live event to the durable disabled phase of the same transaction', async () => {
    const { ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()
    expect(await ran.enableRanMode()).toBe(true)
    const live = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    const tombstone = {
      completed: false,
      enabled: false,
      phase: 'inactive',
      restorePolicy: 'conditional',
      snapshot: live.snapshot,
      transactionId: live.transactionId,
      version: 1
    }

    const durable = JSON.stringify(tombstone)

    window.localStorage.setItem(ran.RAN_MODE_STORAGE_KEY, durable)
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(live),
        oldValue: null,
        storageArea: window.localStorage
      })
    )

    await vi.waitFor(() => {
      expect(ran.$ranModeEnabled.get()).toBe(false)
      expect(tree.captureLayoutStateSnapshot()).toEqual(before)
    })
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBe(durable)
  })

  it('does not resurrect Ran-owned state from a live event after durable cleanup', async () => {
    const { ran, tree } = await loadHarness()
    expect(await ran.enableRanMode()).toBe(true)
    const live = window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)

    const laterTree = {
      type: 'group' as const,
      id: 'post-cleanup-choice',
      panes: ['workspace'],
      active: 'workspace'
    }

    window.localStorage.removeItem(ran.RAN_MODE_STORAGE_KEY)
    tree.applyTree(laterTree, 'post-cleanup')
    const later = tree.captureLayoutStateSnapshot()
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: live,
        oldValue: null,
        storageArea: window.localStorage
      })
    )

    expect(tree.$activePresetId.get()).toBe('post-cleanup')
    expect(tree.captureLayoutStateSnapshot()).toEqual(later)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('does not let a delayed completed peer event overwrite a newer durable preset', async () => {
    const { ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()

    expect(await ran.enableRanMode()).toBe(true)

    const live = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    const completed = {
      ...live,
      completed: true,
      enabled: false,
      phase: 'inactive',
      restorePolicy: 'conditional'
    }

    const laterTree = {
      type: 'group' as const,
      id: 'newer-preset-after-peer-completion',
      panes: ['workspace'],
      active: 'workspace'
    }

    window.localStorage.removeItem(ran.RAN_MODE_STORAGE_KEY)
    tree.applyTree(laterTree, 'newer-preset')
    const later = tree.captureLayoutStateSnapshot()
    const locks = installQueuedLockHarness()

    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(completed),
        oldValue: JSON.stringify(live),
        storageArea: window.localStorage
      })
    )

    await locks.firstStarted
    expect(locks.requests).toEqual([{ mode: 'exclusive', name: ran.RAN_MODE_LOCK_NAME }])
    expect(tree.captureLayoutStateSnapshot()).toEqual(later)
    locks.releaseFirst()
    await locks.idle()

    expect(tree.captureLayoutStateSnapshot()).not.toEqual(before)
    expect(tree.captureLayoutStateSnapshot()).toEqual(later)
    expect(tree.$activePresetId.get()).toBe('newer-preset')
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('does not let a delayed completed peer event overwrite newer bytes for the same preset id', async () => {
    const { ran, tree } = await loadHarness()

    expect(await ran.enableRanMode()).toBe(true)

    const live = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    const completed = {
      ...live,
      completed: true,
      enabled: false,
      phase: 'inactive',
      restorePolicy: 'conditional'
    }

    const newerDefaultTree = {
      type: 'group' as const,
      id: 'newer-default-generation',
      panes: ['workspace'],
      active: 'workspace'
    }

    window.localStorage.removeItem(ran.RAN_MODE_STORAGE_KEY)
    tree.applyTree(newerDefaultTree, 'default')
    const later = tree.captureLayoutStateSnapshot()
    const locks = installQueuedLockHarness()

    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(completed),
        oldValue: JSON.stringify(live),
        storageArea: window.localStorage
      })
    )

    await locks.firstStarted
    expect(tree.captureLayoutStateSnapshot()).toEqual(later)
    locks.releaseFirst()
    await locks.idle()

    expect(tree.captureLayoutStateSnapshot()).toEqual(later)
    expect(tree.$activePresetId.get()).toBe('default')
    expect(tree.$layoutTree.get()).toMatchObject({ id: 'newer-default-generation' })
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('does not let a delayed completed peer event overwrite newer non-layout owned state', async () => {
    const { ran, review, tree } = await loadHarness()

    expect(await ran.enableRanMode()).toBe(true)

    const live = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    window.localStorage.removeItem(ran.RAN_MODE_STORAGE_KEY)
    tree.applyTree(live.snapshot.layout.tree, live.snapshot.layout.activePresetId)

    const completed = {
      ...live,
      completed: true,
      enabled: false,
      phase: 'inactive',
      restorePolicy: 'conditional',
      settlementFingerprint: captureOwnedStorageFingerprint()
    }

    review.$reviewOpen.set(!live.snapshot.reviewOpen)

    const laterReviewOpen = review.$reviewOpen.get()
    const locks = installQueuedLockHarness()

    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(completed),
        oldValue: JSON.stringify(live),
        storageArea: window.localStorage
      })
    )

    await locks.firstStarted
    expect(review.$reviewOpen.get()).toBe(laterReviewOpen)
    locks.releaseFirst()
    await locks.idle()

    expect(review.$reviewOpen.get()).toBe(laterReviewOpen)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('does not let a delayed completed reset clear newer dismissed-pane state', async () => {
    const { ran, tree } = await loadHarness()

    expect(await ran.enableRanMode()).toBe(true)

    const live = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    window.localStorage.removeItem(ran.RAN_MODE_STORAGE_KEY)
    tree.resetLayoutTree()

    const completed = {
      ...live,
      completed: true,
      enabled: false,
      exit: { kind: 'reset' },
      phase: 'leaving',
      restorePolicy: 'conditional',
      settlementFingerprint: captureOwnedStorageFingerprint()
    }

    const dismissedPanesKey = 'hermes.desktop.dismissedPanes.v1'
    const newerDismissedPanes = JSON.stringify(['newer-dismissed-pane'])

    window.localStorage.setItem(dismissedPanesKey, newerDismissedPanes)

    const locks = installQueuedLockHarness()

    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(completed),
        oldValue: JSON.stringify(live),
        storageArea: window.localStorage
      })
    )

    await locks.firstStarted
    expect(window.localStorage.getItem(dismissedPanesKey)).toBe(newerDismissedPanes)
    locks.releaseFirst()
    await locks.idle()

    expect(window.localStorage.getItem(dismissedPanesKey)).toBe(newerDismissedPanes)
  })

  it.each([
    ['panes flipped', 'hermes.desktop.panesFlipped', 'false', 'true'],
    [
      'composer zones',
      'hermes.desktop.composerPopout.zones.v1',
      '{}',
      JSON.stringify({ newer: { poppedOut: true, position: { bottom: 10, right: 10 } } })
    ],
    ['right-rail preview', 'hermes.desktop.rightRailActiveTab', 'file:older', 'file:newer']
  ])('does not let delayed settlement overwrite newer %s subscriber state', async (_label, key, replayValue, newerValue) => {
    const { ran, tree } = await loadHarness()

    expect(await ran.enableRanMode()).toBe(true)

    const live = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    window.localStorage.removeItem(ran.RAN_MODE_STORAGE_KEY)
    tree.applyTree(live.snapshot.layout.tree, live.snapshot.layout.activePresetId)

    const completed = {
      ...live,
      completed: true,
      enabled: false,
      phase: 'inactive',
      restorePolicy: 'conditional',
      settlementFingerprint: captureOwnedStorageFingerprint()
    }

    const unlisten = tree.$layoutTree.subscribe(() => window.localStorage.setItem(key, replayValue))

    window.localStorage.setItem(key, newerValue)

    const locks = installQueuedLockHarness()

    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(completed),
        oldValue: JSON.stringify(live),
        storageArea: window.localStorage
      })
    )

    await locks.firstStarted
    expect(window.localStorage.getItem(key)).toBe(newerValue)
    locks.releaseFirst()
    await locks.idle()
    unlisten()

    expect(window.localStorage.getItem(key)).toBe(newerValue)
  })

  it('blocks reset and preset mutation from a durable incomplete tombstone before initialize', async () => {
    const first = await loadHarness()
    expect(await first.ran.enableRanMode()).toBe(true)
    const nativeSetItem = Storage.prototype.setItem

    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === RAN_MODE_BACKUP_KEY && backupPayload(value)?.completed === true) {
        throw new DOMException('completion write unavailable', 'SecurityError')
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await first.ran.disableRanMode()).toBe(false)
    const tombstone = window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY)
    set.mockRestore()
    vi.resetModules()

    const second = await loadHarness()
    const before = second.tree.captureLayoutStateSnapshot()

    const blockedTree = {
      type: 'group' as const,
      id: 'blocked-before-initialize',
      panes: ['workspace'],
      active: 'workspace'
    }

    expect(second.ran.$ranModeEnabled.get()).toBe(false)
    expect(await second.ran.resetLayoutFromRanMode()).toBe(false)
    await second.presets.applyLayoutPreset('blocked-before-initialize', blockedTree)
    expect(second.tree.captureLayoutStateSnapshot()).toEqual(before)
    expect(window.localStorage.getItem(second.ran.RAN_MODE_STORAGE_KEY)).toBe(tombstone)
  })

  it('rejects a delayed disabled record from an older transaction generation', async () => {
    const { ran, tree } = await loadHarness()
    expect(await ran.enableRanMode()).toBe(true)
    const oldLive = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    const staleTombstone = {
      completed: false,
      enabled: false,
      phase: 'inactive',
      restorePolicy: 'conditional',
      snapshot: oldLive.snapshot,
      transactionId: oldLive.transactionId,
      version: 1
    }

    const newerTombstone = {
      ...staleTombstone,
      exit: {
        kind: 'preset',
        presetId: 'newer-transaction-choice',
        tree: { type: 'group', id: 'newer-transaction-tree', panes: ['workspace'], active: 'workspace' }
      },
      phase: 'leaving',
      transactionId: 'newer-transaction'
    }

    const laterTree = {
      type: 'group' as const,
      id: 'later-local-choice',
      panes: ['workspace'],
      active: 'workspace'
    }

    window.localStorage.setItem(ran.RAN_MODE_STORAGE_KEY, JSON.stringify(newerTombstone))
    tree.applyTree(laterTree, 'later-local-choice')

    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(staleTombstone),
        oldValue: JSON.stringify(oldLive),
        storageArea: window.localStorage
      })
    )

    expect(tree.$activePresetId.get()).toBe('later-local-choice')
    expect(tree.$layoutTree.get()).toMatchObject({ id: 'later-local-choice' })
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBe(JSON.stringify(newerTombstone))
  })

  it('keeps HUD and auxiliary renderers out of primary Ran Mode transactions', async () => {
    vi.doMock('@/store/windows', async importOriginal => {
      const actual = await importOriginal<Record<string, unknown>>()

      return { ...actual, isAuxiliaryWindow: () => true }
    })

    const { presets, ran, tree } = await loadHarness()
    const before = tree.captureLayoutStateSnapshot()

    const auxiliaryTree = {
      type: 'group' as const,
      id: 'auxiliary-choice',
      panes: ['workspace'],
      active: 'workspace'
    }

    expect(await ran.enableRanMode()).toBe(false)
    expect(await ran.initializeRanMode()).toBe(false)
    expect(await ran.disableRanMode()).toBe(false)
    expect(await ran.resetLayoutFromRanMode()).toBe(false)
    expect(await ran.leaveRanModeForLayoutChange('auxiliary-choice', auxiliaryTree)).toBe(false)
    await presets.applyLayoutPreset('auxiliary-choice', auxiliaryTree)
    expect(tree.captureLayoutStateSnapshot()).toEqual(before)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('keeps HUD layout state ephemeral instead of mutating primary shared storage', async () => {
    const primaryTree = {
      type: 'group' as const,
      id: 'primary-persisted-tree',
      panes: ['workspace'],
      active: 'workspace'
    }

    const durableBefore = {
      dismissed: JSON.stringify(['primary-dismissed-pane']),
      paneStates: JSON.stringify({ workspace: { open: true, widthOverride: 640 } }),
      preset: 'primary-preset',
      tree: JSON.stringify(primaryTree),
      userPlaced: JSON.stringify(['primary-user-placed-pane'])
    }

    window.localStorage.setItem('hermes.desktop.layoutTree.v2', durableBefore.tree)
    window.localStorage.setItem('hermes.desktop.layoutPreset.active', durableBefore.preset)
    window.localStorage.setItem('hermes.desktop.dismissedPanes.v1', durableBefore.dismissed)
    window.localStorage.setItem('hermes.desktop.paneStates.v1', durableBefore.paneStates)
    window.localStorage.setItem('hermes.desktop.userPlacedPanes.v1', durableBefore.userPlaced)

    vi.doMock('@/store/windows', async importOriginal => {
      const actual = await importOriginal<Record<string, unknown>>()

      return { ...actual, isAuxiliaryWindow: () => true, isSecondaryWindow: () => false }
    })

    const { tree } = await loadHarness()

    const hudTree = {
      type: 'group' as const,
      id: 'hud-ephemeral-tree',
      panes: ['workspace'],
      active: 'workspace'
    }

    tree.applyTree(hudTree, 'hud-ephemeral')
    tree.resetLayoutTree()

    expect(window.localStorage.getItem('hermes.desktop.layoutTree.v2')).toBe(durableBefore.tree)
    expect(window.localStorage.getItem('hermes.desktop.layoutPreset.active')).toBe(durableBefore.preset)
    expect(window.localStorage.getItem('hermes.desktop.dismissedPanes.v1')).toBe(durableBefore.dismissed)
    expect(window.localStorage.getItem('hermes.desktop.paneStates.v1')).toBe(durableBefore.paneStates)
    expect(window.localStorage.getItem('hermes.desktop.userPlacedPanes.v1')).toBe(durableBefore.userPlaced)
  })

  it('keeps auxiliary-owned preferences and composer zones out of primary storage', async () => {
    vi.doMock('@/store/windows', async importOriginal => {
      const actual = await importOriginal<Record<string, unknown>>()

      return { ...actual, isAuxiliaryWindow: () => true }
    })

    const durableBefore = {
      composer: JSON.stringify({
        'primary-custom-zone': { poppedOut: true, position: { bottom: 37, right: 53 } }
      }),
      flipped: 'true',
      review: 'true',
      terminal: 'true',
      toolView: 'true',
      statusbar: 'true'
    }

    window.localStorage.setItem('hermes.desktop.composerPopout.zones.v1', durableBefore.composer)
    window.localStorage.setItem('hermes.desktop.panesFlipped', durableBefore.flipped)
    window.localStorage.setItem('hermes.desktop.reviewOpen', durableBefore.review)
    window.localStorage.setItem('hermes.desktop.terminalTakeover', durableBefore.terminal)
    window.localStorage.setItem('hermes.desktop.toolView.technical', durableBefore.toolView)
    window.localStorage.setItem('hermes.desktop.statusbarVisible', durableBefore.statusbar)

    const composer = await import('@/store/composer-popout')
    const layout = await import('@/store/layout')
    const review = await import('@/store/review')
    const statusbar = await import('@/store/statusbar-prefs')
    const terminal = await import('@/app/right-sidebar/store')
    const tools = await import('@/store/tool-view')

    expect(composer.$composerPopoutZones.get()).toEqual({})
    expect(layout.$panesFlipped.get()).toBe(false)
    expect(review.$reviewOpen.get()).toBe(false)
    expect(statusbar.$statusbarVisible.get()).toBe(false)
    expect(terminal.$terminalTakeover.get()).toBe(false)
    expect(tools.$toolViewMode.get()).toBe('product')

    expect(window.localStorage.getItem('hermes.desktop.reviewOpen')).toBe(durableBefore.review)
    expect(window.localStorage.getItem('hermes.desktop.terminalTakeover')).toBe(durableBefore.terminal)
    expect(window.localStorage.getItem('hermes.desktop.toolView.technical')).toBe(durableBefore.toolView)

    composer.pruneComposerPopoutZones([])
    layout.$panesFlipped.set(false)
    review.$reviewOpen.set(false)
    statusbar.$statusbarVisible.set(false)
    terminal.setTerminalTakeover(false)
    tools.setToolViewMode('product')

    expect(window.localStorage.getItem('hermes.desktop.composerPopout.zones.v1')).toBe(durableBefore.composer)
    expect(window.localStorage.getItem('hermes.desktop.panesFlipped')).toBe(durableBefore.flipped)
    expect(window.localStorage.getItem('hermes.desktop.reviewOpen')).toBe(durableBefore.review)
    expect(window.localStorage.getItem('hermes.desktop.terminalTakeover')).toBe(durableBefore.terminal)
    expect(window.localStorage.getItem('hermes.desktop.toolView.technical')).toBe(durableBefore.toolView)
    expect(window.localStorage.getItem('hermes.desktop.statusbarVisible')).toBe(durableBefore.statusbar)
  })

  it('isolates panes flipped while preserving upstream right-rail persistence in auxiliary renderers', async () => {
    vi.doMock('@/store/windows', async importOriginal => {
      const actual = await importOriginal<Record<string, unknown>>()

      return { ...actual, isAuxiliaryWindow: () => true }
    })

    window.localStorage.setItem('hermes.desktop.panesFlipped', 'true')
    window.localStorage.setItem('hermes.desktop.rightRailActiveTab', 'url:https://example.com/primary')

    const layout = await import('@/store/layout')

    expect(layout.$panesFlipped.get()).toBe(false)
    expect(layout.$rightRailActiveTabId.get()).toBe('url:https://example.com/primary')

    layout.$panesFlipped.set(true)
    layout.selectRightRailTab('file:C:/work/auxiliary.md')

    expect(window.localStorage.getItem('hermes.desktop.panesFlipped')).toBe('true')
    expect(window.localStorage.getItem('hermes.desktop.rightRailActiveTab')).toBe('file:C:/work/auxiliary.md')
  })

  it('does not let a delayed live-record removal overwrite newer non-layout owned state', async () => {
    const { ran, review } = await loadHarness()

    expect(await ran.enableRanMode()).toBe(true)

    const record = window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)
    const live = JSON.parse(record ?? '{}')
    const locks = installQueuedLockHarness()

    window.localStorage.removeItem(ran.RAN_MODE_STORAGE_KEY)
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: null,
        oldValue: record,
        storageArea: window.localStorage
      })
    )

    await locks.firstStarted

    const laterReviewOpen = !live.snapshot.reviewOpen

    review.$reviewOpen.set(laterReviewOpen)
    expect(review.$reviewOpen.get()).toBe(laterReviewOpen)

    locks.releaseFirst()
    await locks.idle()

    expect(review.$reviewOpen.get()).toBe(laterReviewOpen)
    expect(window.localStorage.getItem('hermes.desktop.reviewOpen')).toBe(String(laterReviewOpen))
    expect(ran.$ranModeEnabled.get()).toBe(false)
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('replays a normally completed peer disable through its full queued storage-event sequence', async () => {
    const originalComposerZones = {
      'peer-removed-zone': { poppedOut: true, position: { bottom: 109, right: 131 } }
    }

    window.localStorage.setItem(COMPOSER_POPOUT_STORAGE_KEY, JSON.stringify(originalComposerZones))

    const first = await loadHarness()
    const before = first.tree.captureLayoutStateSnapshot()

    expect(await first.ran.enableRanMode()).toBe(true)
    first.composer.pruneComposerPopoutZones([])

    vi.resetModules()
    const peer = await loadHarness()

    expect(await peer.ran.initializeRanMode()).toBe(true)
    expect(peer.ran.$ranModeEnabled.get()).toBe(true)

    const nativeSetItem = Storage.prototype.setItem
    const nativeRemoveItem = Storage.prototype.removeItem
    const primaryEvents: Array<{ newValue: null | string; oldValue: null | string }> = []

    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      const oldValue = nativeSetItem === Storage.prototype.setItem ? this.getItem(key) : window.localStorage.getItem(key)

      nativeSetItem.call(this, key, value)

      if (key === first.ran.RAN_MODE_STORAGE_KEY && oldValue !== value) {
        primaryEvents.push({ newValue: value, oldValue })
      }
    })
    vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
      const oldValue = this.getItem(key)

      nativeRemoveItem.call(this, key)

      if (key === first.ran.RAN_MODE_STORAGE_KEY && oldValue !== null) {
        primaryEvents.push({ newValue: null, oldValue })
      }
    })

    expect(await first.ran.disableRanMode()).toBe(true)
    expect(primaryEvents.length).toBeGreaterThanOrEqual(3)

    vi.restoreAllMocks()
    const locks = installQueuedLockHarness()

    for (const event of primaryEvents) {
      window.dispatchEvent(
        new StorageEvent('storage', {
          key: peer.ran.RAN_MODE_STORAGE_KEY,
          newValue: event.newValue,
          oldValue: event.oldValue,
          storageArea: window.localStorage
        })
      )
    }

    await locks.firstStarted
    locks.releaseFirst()
    await vi.waitFor(() => {
      expect(peer.ran.$ranModeEnabled.get()).toBe(false)
      expect(peer.tree.captureLayoutStateSnapshot()).toEqual(before)
      expect(peer.composer.$composerPopoutZones.get()).toEqual(originalComposerZones)
    })
    expect(window.localStorage.getItem(COMPOSER_POPOUT_STORAGE_KEY)).toBe(JSON.stringify(originalComposerZones))
    await locks.idle()
  })

  it('keeps a completed tombstone inert against a delayed incomplete peer event', async () => {
    const first = await loadHarness()
    expect(await first.ran.enableRanMode()).toBe(true)

    const chosenTree = {
      type: 'group' as const,
      id: 'peer-explicit-choice',
      panes: ['workspace'],
      active: 'workspace'
    }

    const nativeRemoveItem = Storage.prototype.removeItem

    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
      if (key === first.ran.RAN_MODE_STORAGE_KEY) {
        throw new DOMException('keep tombstone for peer replay', 'SecurityError')
      }

      nativeRemoveItem.call(this, key)
    })

    expect(await first.ran.leaveRanModeForLayoutChange('peer-choice', chosenTree)).toBe(true)
    expect(JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: true,
      enabled: false,
      phase: 'leaving'
    })

    const completed = JSON.parse(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY) ?? '{}')
    const incomplete = { ...completed, completed: false }

    const laterTree = {
      type: 'group' as const,
      id: 'later-off-mode-choice-after-completion',
      panes: ['workspace'],
      active: 'workspace'
    }

    first.tree.applyTree(laterTree, 'later-choice')
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: first.ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(incomplete),
        oldValue: null,
        storageArea: window.localStorage
      })
    )
    expect(first.tree.$activePresetId.get()).toBe('later-choice')
    expect(first.tree.$layoutTree.get()).toMatchObject({ id: 'later-off-mode-choice-after-completion' })
    expect(window.localStorage.getItem(first.ran.RAN_MODE_STORAGE_KEY)).toBe(JSON.stringify(completed))

    // A completed tombstone left behind by cleanup failure is now harmless on
    // restart and must not replay over the explicit choice.
    remove.mockRestore()
    vi.resetModules()

    const peer = await loadHarness()
    expect(await peer.ran.initializeRanMode()).toBe(false)
    expect(peer.tree.$activePresetId.get()).toBe('later-choice')
    expect(peer.tree.$layoutTree.get()).toMatchObject({ id: 'later-off-mode-choice-after-completion' })
    expect(window.localStorage.getItem(peer.ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('applies a completed tombstone only to an active peer from the same transaction', async () => {
    const { ran, tree } = await loadHarness()
    expect(await ran.enableRanMode()).toBe(true)
    const live = JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')

    const peerTree = {
      type: 'group' as const,
      id: 'peer-completed-choice',
      panes: ['workspace'],
      active: 'workspace'
    }

    const completed = {
      completed: true,
      enabled: false,
      exit: { kind: 'preset', presetId: 'peer-completed', tree: peerTree },
      phase: 'leaving',
      restorePolicy: 'conditional',
      snapshot: live.snapshot,
      transactionId: live.transactionId,
      version: 1
    }

    window.localStorage.setItem(ran.RAN_MODE_STORAGE_KEY, JSON.stringify(completed))

    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(completed),
        oldValue: JSON.stringify(live),
        storageArea: window.localStorage
      })
    )

    await vi.waitFor(() => {
      expect(ran.$ranModeEnabled.get()).toBe(false)
      expect(tree.$activePresetId.get()).toBe('peer-completed')
      expect(tree.$layoutTree.get()).toMatchObject({ id: 'peer-completed-choice' })
    })
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBe(JSON.stringify(completed))

    const laterTree = { ...peerTree, id: 'later-after-peer-completion' }
    tree.applyTree(laterTree, 'later-after-peer-completion')
    window.dispatchEvent(
      new StorageEvent('storage', {
        key: ran.RAN_MODE_STORAGE_KEY,
        newValue: JSON.stringify(completed),
        oldValue: JSON.stringify(live),
        storageArea: window.localStorage
      })
    )

    await vi.waitFor(() => expect(ran.isRanModeRecoveryIncomplete()).toBe(false))
    expect(tree.$activePresetId.get()).toBe('later-after-peer-completion')
    expect(tree.$layoutTree.get()).toMatchObject({ id: 'later-after-peer-completion' })
  })

  it('restores the baseline while retaining panes contributed during Ran Mode', async () => {
    const { panes, ran, tree } = await loadHarness()
    const { registry } = await import('@/contrib/registry')
    const { findGroupOfPane } = await import('@/components/pane-shell/tree/model')

    expect(await ran.enableRanMode()).toBe(true)
    registry.register({
      area: 'panes',
      data: { placement: 'main' },
      id: 'workspace',
      render: () => null,
      title: 'Workspace'
    })
    registry.register({
      area: 'panes',
      data: { placement: 'right' },
      id: 'late-pane',
      render: () => null,
      title: 'Late pane'
    })
    panes.ensurePaneRegistered('late-pane', { open: true, widthOverride: 333 })
    tree.adoptContributedPanes()
    expect(findGroupOfPane(tree.$layoutTree.get()!, 'late-pane')).not.toBeNull()

    expect(await ran.disableRanMode()).toBe(true)
    expect(findGroupOfPane(tree.$layoutTree.get()!, 'late-pane')).not.toBeNull()
    expect(panes.$paneStates.get()['late-pane']).toEqual({ open: true, widthOverride: 333 })
  })

  it('keeps non-reset settlement incomplete when contributed-pane adoption cannot clear dismissed persistence', async () => {
    const { ran, tree } = await loadHarness()
    const { registry } = await import('@/contrib/registry')
    const dismissedKey = 'hermes.desktop.dismissedPanes.v1'
    const staleDismissed = JSON.stringify(['stale-plugin-pane'])

    expect(await ran.enableRanMode()).toBe(true)

    tree.$dismissedPanes.set(new Set(['stale-plugin-pane']))
    window.localStorage.setItem(dismissedKey, staleDismissed)

    const unregister = registry.register({
      area: 'panes',
      id: 'stale-plugin-pane',
      source: 'plugin:test',
      data: { placement: 'right' },
      render: () => null
    })

    const nativeRemoveItem = Storage.prototype.removeItem

    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
      if (key === dismissedKey) {
        throw new DOMException('dismissed persistence unavailable', 'SecurityError')
      }

      nativeRemoveItem.call(this, key)
    })

    expect(await ran.disableRanMode()).toBe(false)
    expect(tree.$dismissedPanes.get()).toEqual(new Set())
    expect(window.localStorage.getItem(dismissedKey)).toBe(staleDismissed)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      phase: 'inactive'
    })

    remove.mockRestore()
    unregister()
  })

  it('keeps reset incomplete when a handler final-tree write is dropped', async () => {
    const { ran, tree } = await loadHarness()
    expect(await ran.enableRanMode()).toBe(true)

    const handlerTree = {
      type: 'group' as const,
      id: 'dropped-reset-handler-tree',
      panes: ['workspace'],
      active: 'workspace'
    }

    const unregister = tree.registerLayoutResetHandler(() => tree.applyTree(handlerTree, 'reset-handler'))
    const nativeSetItem = Storage.prototype.setItem

    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(function (this: Storage, key, value) {
      if (key === 'hermes.desktop.layoutTree.v2') {
        throw new DOMException('final tree write unavailable', 'SecurityError')
      }

      nativeSetItem.call(this, key, value)
    })

    expect(await ran.resetLayoutFromRanMode()).toBe(false)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      exit: { kind: 'reset' },
      phase: 'leaving'
    })

    set.mockRestore()
    unregister()
  })

  it('verifies a reset handler that persists a concrete final tree', async () => {
    const { ran, tree } = await loadHarness()
    expect(await ran.enableRanMode()).toBe(true)

    const handlerTree = {
      type: 'group' as const,
      id: 'reset-handler-final-tree',
      panes: ['workspace'],
      active: 'workspace'
    }

    const unregister = tree.registerLayoutResetHandler(() => tree.applyTree(handlerTree, 'reset-handler'))

    expect(await ran.resetLayoutFromRanMode()).toBe(true)
    expect(tree.$layoutTree.get()).toMatchObject({ id: 'reset-handler-final-tree' })
    expect(window.localStorage.getItem('hermes.desktop.layoutTree.v2')).toBe(JSON.stringify(tree.$layoutTree.get()))
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
    unregister()
  })

  it('persists the canonical concrete reset tree for multiple live session tiles and clears dismissed panes across restart', async () => {
    const { ran, tree } = await loadHarness()
    const { registry } = await import('@/contrib/registry')
    const { findGroupOfPane } = await import('@/components/pane-shell/tree/model')
    const dismissedKey = 'hermes.desktop.dismissedPanes.v1'

    const unregisterPanes = ['session-tile:one', 'session-tile:two'].map(id =>
      registry.register({ area: 'panes', data: { placement: 'main' }, id, render: () => null, title: id })
    )

    tree.$dismissedPanes.set(new Set(['pre-dismissed-pane']))
    window.localStorage.setItem(dismissedKey, JSON.stringify(['pre-dismissed-pane']))
    expect(await ran.enableRanMode()).toBe(true)

    const unregisterReset = tree.registerLayoutResetHandler(() => {
      for (const id of ['session-tile:one', 'session-tile:two']) {
        const current = tree.$layoutTree.get()
        const mainGroup = current ? findGroupOfPane(current, 'workspace')?.id : null

        if (mainGroup) {
          tree.moveTreePane(id, { groupId: mainGroup, pos: 'center' })
        }
      }
    })

    expect(await ran.resetLayoutFromRanMode()).toBe(true)
    expect(window.localStorage.getItem(dismissedKey)).toBeNull()
    expect(window.localStorage.getItem('hermes.desktop.layoutTree.v2')).toBe(JSON.stringify(tree.$layoutTree.get()))
    expect(findGroupOfPane(tree.$layoutTree.get()!, 'session-tile:one')).not.toBeNull()
    expect(findGroupOfPane(tree.$layoutTree.get()!, 'session-tile:two')).not.toBeNull()
    expect(tree.$activePresetId.get()).toBe('default')
    expect(tree.$userPlacedPanes.get()).toEqual(new Set())
    expect(window.localStorage.getItem('hermes.desktop.userPlacedPanes.v1')).toBeNull()
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()

    unregisterReset()
    unregisterPanes.forEach(unregister => unregister())
    vi.resetModules()

    const restarted = await loadHarness()
    const restartedModel = await import('@/components/pane-shell/tree/model')

    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(restartedModel.findGroupOfPane(restarted.tree.$layoutTree.get()!, 'session-tile:one')).not.toBeNull()
    expect(restartedModel.findGroupOfPane(restarted.tree.$layoutTree.get()!, 'session-tile:two')).not.toBeNull()
    expect(restarted.tree.$activePresetId.get()).toBe('default')
    expect(restarted.tree.$userPlacedPanes.get()).toEqual(new Set())
    expect(window.localStorage.getItem('hermes.desktop.userPlacedPanes.v1')).toBeNull()
    expect(restarted.tree.$dismissedPanes.get()).toEqual(new Set())
  })

  it('keeps reset incomplete when dismissed-pane removal is swallowed and completes it after restart', async () => {
    const { ran, tree } = await loadHarness()
    const dismissedKey = 'hermes.desktop.dismissedPanes.v1'
    const staleDismissed = JSON.stringify(['pre-dismissed-pane'])

    tree.$dismissedPanes.set(new Set(['pre-dismissed-pane']))
    window.localStorage.setItem(dismissedKey, staleDismissed)
    expect(await ran.enableRanMode()).toBe(true)

    const nativeRemoveItem = Storage.prototype.removeItem

    const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(function (this: Storage, key) {
      if (key === dismissedKey) {
        throw new DOMException('dismissed persistence unavailable', 'SecurityError')
      }

      nativeRemoveItem.call(this, key)
    })

    expect(await ran.resetLayoutFromRanMode()).toBe(false)
    expect(window.localStorage.getItem(dismissedKey)).toBe(staleDismissed)
    expect(JSON.parse(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY) ?? '{}')).toMatchObject({
      completed: false,
      enabled: false,
      exit: { kind: 'reset' }
    })

    remove.mockRestore()
    vi.resetModules()

    const restarted = await loadHarness()
    expect(await restarted.ran.initializeRanMode()).toBe(false)
    expect(window.localStorage.getItem(dismissedKey)).toBeNull()
    expect(window.localStorage.getItem(restarted.ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })

  it('treats Reset Layout as an explicit choice that exits Ran Mode', async () => {
    const { ran, statusbar, tools, tree } = await loadHarness()
    statusbar.$statusbarVisible.set(true)
    tools.$toolViewMode.set('technical')
    expect(await ran.enableRanMode()).toBe(true)

    expect(await ran.resetLayoutFromRanMode()).toBe(true)
    expect(ran.$ranModeEnabled.get()).toBe(false)
    expect(tree.$activePresetId.get()).toBe('default')
    expect(statusbar.$statusbarVisible.get()).toBe(true)
    expect(tools.$toolViewMode.get()).toBe('technical')
    expect(window.localStorage.getItem(ran.RAN_MODE_STORAGE_KEY)).toBeNull()
  })
})
