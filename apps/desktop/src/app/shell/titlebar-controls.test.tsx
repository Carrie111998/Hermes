import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as LayoutStore from '@/store/layout'
import type { SessionInfo } from '@/types/hermes'

const { toggleFileBrowserOpen, toggleSidebarOpen } = vi.hoisted(() => ({
  toggleFileBrowserOpen: vi.fn(),
  toggleSidebarOpen: vi.fn()
}))

vi.mock('@/store/layout', async importOriginal => {
  const actual = await importOriginal<typeof LayoutStore>()

  return {
    ...actual,
    toggleFileBrowserOpen: () => {
      toggleFileBrowserOpen()
      actual.toggleFileBrowserOpen()
    },
    toggleSidebarOpen
  }
})

import { group, split } from '@/components/pane-shell/tree/model'
import {
  $dismissedPanes,
  $hiddenTreePanes,
  $layoutTree,
  isPaneVisible,
  setTreePaneHidden
} from '@/components/pane-shell/tree/store'
import { registry } from '@/contrib/registry'
import { I18nProvider } from '@/i18n'
import { formatCombo } from '@/lib/keybinds/combo'
import { resetBinding, setBinding } from '@/store/keybinds'
import { $panesFlipped, FILES_PANE_ID, setFileBrowserOpen, setSidebarOpen } from '@/store/layout'
import { $sessions } from '@/store/session'

import { TitlebarControls } from './titlebar-controls'

const unreadSession = {
  id: 'unread-session',
  message_count: 1,
  source: 'cli',
  started_at: 0,
  title: 'Unread session',
  unread: true
} as SessionInfo

const paneDisposers: (() => void)[] = []

function setFilesVisible(visible: boolean) {
  setFileBrowserOpen(visible)
  setTreePaneHidden(FILES_PANE_ID, !visible)
}

function renderTitlebar() {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <MemoryRouter>
        <TitlebarControls onOpenSettings={vi.fn()} />
      </MemoryRouter>
    </I18nProvider>
  )
}

beforeEach(() => {
  window.localStorage.clear()
  $dismissedPanes.set(new Set())
  $hiddenTreePanes.set(new Set())
  paneDisposers.push(
    registry.register({
      area: 'panes',
      data: { placement: 'main', uncloseable: true },
      id: 'workspace',
      render: () => null,
      title: 'workspace'
    }),
    registry.register({
      area: 'panes',
      data: { placement: 'right' },
      id: FILES_PANE_ID,
      render: () => null,
      title: FILES_PANE_ID
    }),
    registry.register({
      area: 'panes',
      data: { placement: 'right' },
      id: 'review',
      render: () => null,
      title: 'review'
    })
  )
  $layoutTree.set(
    split('row', [
      group(['workspace'], { active: 'workspace', id: 'g-main' }),
      group([FILES_PANE_ID], { active: FILES_PANE_ID, id: 'g-files' })
    ])
  )
  setFilesVisible(true)
  setSidebarOpen(true)
  $panesFlipped.set(false)
  $sessions.set([])
  vi.clearAllMocks()
})

afterEach(() => {
  cleanup()
  paneDisposers.splice(0).forEach(dispose => dispose())
  $layoutTree.set(null)
  $hiddenTreePanes.set(new Set())
  $dismissedPanes.set(new Set())
  $panesFlipped.set(false)
  $sessions.set([])
  resetBinding('view.toggleRightSidebar')
})

describe('TitlebarControls Files toggle', () => {
  it('labels the visible Files control precisely', async () => {
    renderTitlebar()

    const filesToggle = screen.getByRole('button', { name: 'Hide files' })

    fireEvent.pointerMove(filesToggle, { pointerType: 'mouse' })
    expect((await screen.findByRole('tooltip')).textContent).toContain('Hide files')
  })

  it('labels the hidden Files control precisely', async () => {
    setFilesVisible(false)
    renderTitlebar()

    const filesToggle = screen.getByRole('button', { name: 'Show files' })

    fireEvent.pointerMove(filesToggle, { pointerType: 'mouse' })
    expect((await screen.findByRole('tooltip')).textContent).toContain('Show files')
  })

  it('shows Files when it is stacked behind the active sibling tab', async () => {
    $layoutTree.set(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'g-main' }),
        group([FILES_PANE_ID, 'review'], { active: 'review', id: 'g-right' })
      ])
    )
    setFileBrowserOpen(true)
    renderTitlebar()

    const filesToggle = screen.getByRole('button', { name: 'Show files' })

    fireEvent.pointerMove(filesToggle, { pointerType: 'mouse' })
    expect((await screen.findByRole('tooltip')).textContent).toContain('Show files')

    fireEvent.click(filesToggle)

    expect(isPaneVisible(FILES_PANE_ID)).toBe(true)
    expect(screen.getByRole('button', { name: 'Hide files' })).toBe(filesToggle)
  })

  it('shows Files when its zone is minimized', async () => {
    $layoutTree.set(
      split('row', [
        group(['workspace'], { active: 'workspace', id: 'g-main' }),
        group([FILES_PANE_ID], { active: FILES_PANE_ID, id: 'g-files', minimized: true })
      ])
    )
    setFileBrowserOpen(true)
    renderTitlebar()

    const filesToggle = screen.getByRole('button', { name: 'Show files' })

    fireEvent.pointerMove(filesToggle, { pointerType: 'mouse' })
    expect((await screen.findByRole('tooltip')).textContent).toContain('Show files')

    fireEvent.click(filesToggle)

    expect(isPaneVisible(FILES_PANE_ID)).toBe(true)
    expect(screen.getByRole('button', { name: 'Hide files' })).toBe(filesToggle)
  })

  it.each([
    { filesLabel: 'Hide files', open: true },
    { filesLabel: 'Show files', open: false }
  ])('keeps unread state on Sessions when panes are flipped and Files is $filesLabel', async ({ filesLabel, open }) => {
    setFilesVisible(open)
    $panesFlipped.set(true)
    $sessions.set([unreadSession])
    setBinding('view.toggleRightSidebar', ['alt+shift+9'])
    renderTitlebar()

    const filesToggle = screen.getByRole('button', { name: filesLabel })
    const sessionsToggle = screen.getByRole('button', { name: 'Hide sidebar · 1 unread session' })

    expect(within(filesToggle).queryByText('1')).toBeNull()
    expect(within(sessionsToggle).queryByText('1')).not.toBeNull()

    fireEvent.pointerMove(filesToggle, { pointerType: 'mouse' })
    expect((await screen.findByRole('tooltip')).textContent).toBe(`${filesLabel}${formatCombo('alt+shift+9')}`)
  })

  it('toggles Files without toggling the Sessions sidebar', () => {
    renderTitlebar()

    fireEvent.click(screen.getByRole('button', { name: 'Hide files' }))

    expect(toggleFileBrowserOpen).toHaveBeenCalledTimes(1)
    expect(toggleSidebarOpen).not.toHaveBeenCalled()
  })

  it('keeps the right-sidebar action id for stored keybindings', async () => {
    setBinding('view.toggleRightSidebar', ['alt+shift+9'])
    renderTitlebar()

    fireEvent.pointerMove(screen.getByRole('button', { name: 'Hide files' }), { pointerType: 'mouse' })
    expect((await screen.findByRole('tooltip')).textContent).toBe(`Hide files${formatCombo('alt+shift+9')}`)
  })
})
