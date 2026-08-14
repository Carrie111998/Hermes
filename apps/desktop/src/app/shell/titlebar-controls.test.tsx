import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { TitlebarControls } from './titlebar-controls'

const actions = vi.hoisted(() => ({
  equalize: vi.fn(),
  reset: vi.fn()
}))

vi.mock('@/app/hud/handoff', () => ({ hudTargetSessionId: vi.fn() }))

vi.mock('@/components/pane-shell/tree/store', () => ({
  equalizeVisibleSessionPanes: actions.equalize,
  resetLayoutTree: actions.reset
}))

vi.mock('@/components/ui/tooltip', () => ({
  Tip: ({ children }: { children: ReactNode }) => <>{children}</>,
  TipKeybindLabel: ({ text }: { text: string }) => <>{text}</>
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      shell: {
        appControls: 'App controls',
        paneControls: 'Pane controls',
        windowControls: 'Window controls'
      },
      titlebar: {
        enterHud: 'HUD mode',
        equalizeConversationPanes: 'Equalize conversation panes',
        hideRightSidebar: 'Hide right sidebar',
        hideSidebar: 'Hide sidebar',
        layoutEditor: 'Layout editor',
        layoutEditorTitle: 'Layout editor — ⌘-click resets the layout',
        muteHaptics: 'Mute haptics',
        openSettings: 'Open settings',
        showRightSidebar: 'Show right sidebar',
        showSidebar: 'Show sidebar',
        swapSidebarSides: 'Swap sidebar sides',
        unmuteHaptics: 'Unmute haptics'
      }
    }
  })
}))

vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('TitlebarControls conversation layout action', () => {
  it('places equalize immediately after the layout editor and invokes it', () => {
    render(
      <MemoryRouter>
        <TitlebarControls onOpenSettings={vi.fn()} />
      </MemoryRouter>
    )

    const appControls = screen.getByLabelText('App controls')

    const firstTwoLabels = within(appControls)
      .getAllByRole('button')
      .slice(0, 2)
      .map(button => button.getAttribute('aria-label'))

    expect(firstTwoLabels).toEqual(['Layout editor', 'Equalize conversation panes'])

    fireEvent.click(within(appControls).getByRole('button', { name: 'Equalize conversation panes' }))

    expect(actions.equalize).toHaveBeenCalledOnce()
  })
})
