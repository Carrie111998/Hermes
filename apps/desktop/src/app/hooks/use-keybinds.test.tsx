import { cleanup, fireEvent, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type * as TreeStore from '@/components/pane-shell/tree/store'
import type * as LayoutStore from '@/store/layout'

const { toggleFileBrowserOpen, togglePaneVisible } = vi.hoisted(() => ({
  toggleFileBrowserOpen: vi.fn(),
  togglePaneVisible: vi.fn()
}))

vi.mock('@/components/pane-shell/tree/store', async importOriginal => {
  const actual = await importOriginal<typeof TreeStore>()

  return {
    ...actual,
    togglePaneVisible
  }
})

vi.mock('@/store/layout', async importOriginal => {
  const actual = await importOriginal<typeof LayoutStore>()

  return {
    ...actual,
    toggleFileBrowserOpen
  }
})

vi.mock('@/themes/context', () => ({
  useTheme: () => ({ resolvedMode: 'dark', setMode: vi.fn() })
}))

import { type KeybindRuntimeDeps, useKeybinds } from '@/app/hooks/use-keybinds'
import { group } from '@/components/pane-shell/tree/model'
import { $layoutTree } from '@/components/pane-shell/tree/store'
import { resetBinding } from '@/store/keybinds'
import { FILES_PANE_ID } from '@/store/layout'

function KeybindHarness({ deps }: { deps: KeybindRuntimeDeps }) {
  useKeybinds(deps)

  return null
}

function renderKeybinds() {
  const deps: KeybindRuntimeDeps = {
    toggleCommandCenter: vi.fn(),
    startFreshSession: vi.fn(),
    openNewSessionTab: vi.fn(),
    toggleSelectedPin: vi.fn(),
    archiveSelectedSession: vi.fn()
  }

  render(
    <MemoryRouter>
      <KeybindHarness deps={deps} />
    </MemoryRouter>
  )
}

function pressFilesKeybind() {
  fireEvent.keyDown(window, { key: 'j', metaKey: true })
}

beforeEach(() => {
  window.localStorage.clear()
  vi.clearAllMocks()
  resetBinding('view.toggleRightSidebar')
})

afterEach(() => {
  cleanup()
  $layoutTree.set(null)
  resetBinding('view.toggleRightSidebar')
})

describe('view.toggleRightSidebar keybind', () => {
  it('routes to Files when Files is stacked with workspace and there is no right root side', () => {
    $layoutTree.set(group(['workspace', FILES_PANE_ID], { active: 'workspace', id: 'g-workspace-files' }))
    renderKeybinds()

    pressFilesKeybind()

    expect(togglePaneVisible).not.toHaveBeenCalledWith('terminal')
    expect(toggleFileBrowserOpen).toHaveBeenCalledTimes(1)
  })

  it('routes fail-closed to Files when Files is missing and there is no right root side', () => {
    $layoutTree.set(group(['workspace'], { active: 'workspace', id: 'g-workspace' }))
    renderKeybinds()

    pressFilesKeybind()

    expect(togglePaneVisible).not.toHaveBeenCalledWith('terminal')
    expect(toggleFileBrowserOpen).toHaveBeenCalledTimes(1)
  })
})
