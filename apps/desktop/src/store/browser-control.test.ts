import { beforeEach, describe, expect, it } from 'vitest'

import {
  $browserAgentActingTabId,
  $browserControlModes,
  browserControlMode,
  forgetBrowserControl,
  markBrowserAgentActing,
  setBrowserControlMode,
  toggleBrowserControlMode
} from './browser-control'

beforeEach(() => {
  $browserControlModes.set({})
  $browserAgentActingTabId.set(null)
})

describe('browser control mode', () => {
  // The whole point of the default: Browser Use keeps working for anyone who
  // never touches the new switch.
  it('defaults to agent control for an unknown tab', () => {
    expect(browserControlMode('url:browser-1')).toBe('agent')
    expect(browserControlMode(null)).toBe('agent')
    expect(browserControlMode(undefined)).toBe('agent')
  })

  it('records manual control and toggles back', () => {
    setBrowserControlMode('url:browser-1', 'manual')
    expect(browserControlMode('url:browser-1')).toBe('manual')

    toggleBrowserControlMode('url:browser-1')
    expect(browserControlMode('url:browser-1')).toBe('agent')
  })

  // Absence IS the default, so returning to `agent` must delete the row rather
  // than store it — otherwise the record grows one entry per tab ever opened.
  it('stores nothing for the default mode', () => {
    setBrowserControlMode('url:browser-1', 'manual')
    setBrowserControlMode('url:browser-1', 'agent')

    expect($browserControlModes.get()).toEqual({})
  })

  it('keeps modes per tab', () => {
    setBrowserControlMode('url:browser-1', 'manual')

    expect(browserControlMode('url:browser-2')).toBe('agent')
  })

  it('forgets a closed tab', () => {
    setBrowserControlMode('url:browser-1', 'manual')
    forgetBrowserControl('url:browser-1')

    expect(browserControlMode('url:browser-1')).toBe('agent')
  })
})

describe('agent activity', () => {
  it('marks and clears the acting tab', () => {
    const done = markBrowserAgentActing('url:browser-1')
    expect($browserAgentActingTabId.get()).toBe('url:browser-1')

    done()
    expect($browserAgentActingTabId.get()).toBeNull()
  })

  // An out-of-order disposer from a superseded action must not clear the badge
  // for the action that is actually running now.
  it('a stale disposer does not clear a newer action', () => {
    const stale = markBrowserAgentActing('url:browser-1')
    markBrowserAgentActing('url:browser-2')

    stale()

    expect($browserAgentActingTabId.get()).toBe('url:browser-2')
  })

  // Taking the wheel mid-action would otherwise leave "Hermes is driving" lit
  // on a tab it is no longer allowed to touch.
  it('taking manual control clears the driving badge', () => {
    markBrowserAgentActing('url:browser-1')
    setBrowserControlMode('url:browser-1', 'manual')

    expect($browserAgentActingTabId.get()).toBeNull()
  })

  it('marking with no tab is a no-op', () => {
    markBrowserAgentActing(null)()

    expect($browserAgentActingTabId.get()).toBeNull()
  })
})
