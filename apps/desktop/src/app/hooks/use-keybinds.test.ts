import { afterEach, describe, expect, it } from 'vitest'

import { terminalKeybindHandlers } from './use-keybinds'

const originalDesktop = window.hermesDesktop

afterEach(() => {
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: originalDesktop,
    writable: true
  })
})

describe('terminalKeybindHandlers', () => {
  it('omits every terminal handler for a managed agent', () => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { eva: {} },
      writable: true
    })

    expect(terminalKeybindHandlers()).toEqual({})
  })

  it('keeps the complete unmanaged terminal handler set', () => {
    expect(Object.keys(terminalKeybindHandlers(false)).sort()).toEqual([
      'view.closeTerminal',
      'view.newTerminal',
      'view.nextTerminal',
      'view.prevTerminal',
      'view.showTerminal'
    ])
  })
})
