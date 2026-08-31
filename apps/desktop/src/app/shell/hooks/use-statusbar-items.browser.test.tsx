/**
 * The Browser had a keybind and a ⌘K row and no button, so the only
 * discoverable way in was to ask the agent to open it. These are the two facts
 * that make the button a door: it is produced, and it is not hidden by default.
 */

import { renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { useStatusbarItems } from '@/app/shell/hooks/use-statusbar-items'
import { STATUSBAR_HIDDEN_BY_DEFAULT } from '@/store/statusbar-prefs'

const openBrowserTab = vi.fn()

vi.mock('@/store/preview', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  openBrowserTab: () => openBrowserTab()
}))

function items(chatOpen = true) {
  const { result } = renderHook(
    () =>
      useStatusbarItems({
        agentsOpen: false,
        chatOpen,
        commandCenterOpen: false,
        extraLeftItems: [],
        extraRightItems: [],
        freshDraftReady: false,
        gatewayState: 'open',
        inferenceStatus: null,
        openAgents: () => {},
        openCommandCenterSection: () => {},
        requestGateway: async () => ({}) as never,
        statusSnapshot: null,
        toggleCommandCenter: () => {}
      }),
    { wrapper: ({ children }) => <MemoryRouter>{children}</MemoryRouter> }
  )

  return [...result.current.leftStatusbarItems, ...result.current.statusbarItems]
}

afterEach(() => vi.clearAllMocks())

describe('the Browser button', () => {
  it('is on the status bar', () => {
    expect(items().find(item => item.id === 'browser')).toBeTruthy()
  })

  it('opens the browser when clicked', () => {
    items()
      .find(item => item.id === 'browser')
      ?.onSelect?.({ shiftKey: false })
    expect(openBrowserTab).toHaveBeenCalled()
  })

  it('carries the action, so the tooltip teaches the shortcut', () => {
    expect(items().find(item => item.id === 'browser')?.actionId).toBe('view.showBrowser')
  })

  it('is visible by default, unlike the terminal pill beside it', () => {
    // A button nobody can find is the bug being fixed; shipping it hidden would
    // reproduce it exactly.
    expect(STATUSBAR_HIDDEN_BY_DEFAULT).not.toContain('browser')
  })

  it('stays out of the bar when there is no chat on screen', () => {
    expect(items(false).find(item => item.id === 'browser')?.hidden).toBe(true)
  })
})
