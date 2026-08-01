import { beforeEach, describe, expect, it, vi } from 'vitest'

const storage = new Map<string, string>()

describe('terminal takeover state', () => {
  beforeEach(() => {
    storage.clear()
    vi.resetModules()
  })

  it('starts closed even when a stale persisted takeover flag exists', async () => {
    storage.set('hermes.desktop.terminalTakeover', 'true')

    const { $terminalTakeover } = await import('./store')

    expect($terminalTakeover.get()).toBe(false)
  })

  it('changes only the current session state', async () => {
    const { $terminalTakeover, setTerminalTakeover } = await import('./store')

    setTerminalTakeover(true)

    expect($terminalTakeover.get()).toBe(true)
    expect(storage.has('hermes.desktop.terminalTakeover')).toBe(false)
  })
})
