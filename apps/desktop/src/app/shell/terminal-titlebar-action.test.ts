import { beforeEach, describe, expect, it, vi } from 'vitest'

import { resetTerminalTitlebarActionForTests, toggleTerminalFromTitlebar } from './terminal-titlebar-action'

describe('terminal titlebar action', () => {
  beforeEach(resetTerminalTitlebarActionForTests)

  it('creates one fresh terminal the first time a hidden pane is opened', () => {
    const create = vi.fn()
    const toggle = vi.fn()

    toggleTerminalFromTitlebar({ create, isVisible: () => false, toggle })
    toggleTerminalFromTitlebar({ create, isVisible: () => false, toggle })

    expect(create).toHaveBeenCalledOnce()
    expect(toggle).toHaveBeenCalledTimes(2)
  })

  it('does not create a terminal when the first action only hides a visible pane', () => {
    const create = vi.fn()
    const toggle = vi.fn()

    toggleTerminalFromTitlebar({ create, isVisible: () => true, toggle })

    expect(create).not.toHaveBeenCalled()
    expect(toggle).toHaveBeenCalledOnce()
  })
})
