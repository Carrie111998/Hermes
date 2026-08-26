import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { StrictMode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { TitlebarControls } from '@/app/shell/titlebar-controls'
import { PANE_TOGGLE_REVEAL_EVENT } from '@/components/pane-shell'
import { registry } from '@/contrib/registry'
import { I18nProvider } from '@/i18n'
import { FILES_PANE_ID, setFileBrowserOpen } from '@/store/layout'
import { stubResizeObserver } from '@/test/jsdom'

import { group, split } from '../model'
import { $narrowOverlayReveal } from '../narrow-overlay-state'
import { $hiddenTreePanes, $layoutTree, $narrowViewport, declareDefaultTree, setTreePaneHidden } from '../store'

import { NarrowOverlays } from './narrow-overlays'

// Ground truth for "the Bots tab is still visible when the sessions sidebar
// collapses on a narrow window". A collapsible pane DOCKED into the sessions
// zone (SESSIONS | BOTS) must leave the grid with the zone, and the narrow
// edge overlay must mirror the zone's tab strip so the docked pane stays
// reachable — not just the zone's first pane.

beforeAll(() => {
  stubResizeObserver()
})

const disposers: (() => void)[] = []
const originalMatchMedia = window.matchMedia

const registerPane = (id: string, title: string, data: Record<string, unknown>, body: string) => {
  disposers.push(
    registry.register({
      area: 'panes',
      data,
      id,
      render: () => <div data-testid={`${id}-body`}>{body}</div>,
      title
    })
  )
}

beforeEach(() => {
  window.localStorage.clear()
  $hiddenTreePanes.set(new Set())
  Object.defineProperty(window, 'matchMedia', {
    configurable: true,
    value: vi.fn(() => ({
      addEventListener: vi.fn(),
      matches: true,
      removeEventListener: vi.fn()
    }))
  })

  registerPane('sessions', 'sessions', { collapsible: true, placement: 'left', width: '237px' }, 'session rows')
  registerPane('bots', 'Bots', { collapsible: true, placement: 'left', width: '260px' }, 'bot roster')
  registerPane('workspace', 'workspace', { placement: 'main', uncloseable: true }, 'chat')
  registerPane(
    FILES_PANE_ID,
    'files',
    { collapsible: true, placement: 'right', revealAliases: ['file-browser'], width: '256px' },
    'file rows'
  )

  declareDefaultTree(split('row', [group(['sessions', 'bots']), group(['workspace']), group([FILES_PANE_ID])]))
  $narrowViewport.set(true)
  setFileBrowserOpen(true)
})

afterEach(() => {
  cleanup()
  Object.defineProperty(window, 'matchMedia', { configurable: true, value: originalMatchMedia })
  $narrowViewport.set(false)
  $layoutTree.set(null)
  disposers.splice(0).forEach(dispose => dispose())
})

const revealPane = (id: string) => {
  act(() => {
    window.dispatchEvent(new CustomEvent(PANE_TOGGLE_REVEAL_EVENT, { detail: { id, mode: 'open' } }))
  })
}

const overlayTab = (paneId: string) =>
  globalThis.document.querySelector<HTMLElement>(`[data-narrow-overlay-tab="${paneId}"]`)

const renderShellControls = () =>
  render(
    <StrictMode>
      <I18nProvider configClient={null} initialLocale="en">
        <MemoryRouter>
          <TitlebarControls onOpenSettings={() => {}} />
          <NarrowOverlays />
        </MemoryRouter>
      </I18nProvider>
    </StrictMode>
  )

describe('narrow Files effective visibility', () => {
  it('tracks closed, clicked, escaped, and toggled-closed overlay transitions in the titlebar label', () => {
    renderShellControls()

    const filesToggle = screen.getByRole('button', { name: 'Show files' })
    expect(screen.queryByTestId('files-body')).toBeNull()

    fireEvent.click(filesToggle)
    expect(screen.getByTestId('files-body')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Hide files' })).toBe(filesToggle)

    fireEvent.keyDown(window, { key: 'Escape' })
    expect(screen.queryByTestId('files-body')).toBeNull()
    expect(screen.getByRole('button', { name: 'Show files' })).toBe(filesToggle)

    fireEvent.click(filesToggle)
    expect(screen.getByTestId('files-body')).toBeTruthy()
    fireEvent.click(filesToggle)
    expect(screen.queryByTestId('files-body')).toBeNull()
    expect(screen.getByRole('button', { name: 'Show files' })).toBe(filesToggle)
  })

  it('fails closed and clears a Files reveal when the tree visibility gate hides it', () => {
    renderShellControls()

    const filesToggle = screen.getByRole('button', { name: 'Show files' })
    fireEvent.click(filesToggle)
    expect(screen.getByTestId('files-body')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Hide files' })).toBe(filesToggle)

    act(() => setTreePaneHidden(FILES_PANE_ID, true))

    expect(screen.queryByTestId('files-body')).toBeNull()
    expect(screen.getByRole('button', { name: 'Show files' })).toBe(filesToggle)
    expect($narrowOverlayReveal.get()).toBeNull()

    act(() => setTreePaneHidden(FILES_PANE_ID, false))

    expect(screen.queryByTestId('files-body')).toBeNull()
    expect(screen.getByRole('button', { name: 'Show files' })).toBe(filesToggle)

    fireEvent.click(filesToggle)
    expect(screen.getByTestId('files-body')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Hide files' })).toBe(filesToggle)
  })

  it('fails closed and clears a Files reveal when Files leaves the tree', () => {
    renderShellControls()

    const filesToggle = screen.getByRole('button', { name: 'Show files' })
    fireEvent.click(filesToggle)
    expect(screen.getByTestId('files-body')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Hide files' })).toBe(filesToggle)

    act(() => {
      $layoutTree.set(split('row', [group(['sessions', 'bots']), group(['workspace'])]))
    })

    expect(screen.queryByTestId('files-body')).toBeNull()
    expect(screen.getByRole('button', { name: 'Show files' })).toBe(filesToggle)
    expect($narrowOverlayReveal.get()).toBeNull()

    act(() => {
      $layoutTree.set(split('row', [group(['sessions', 'bots']), group(['workspace']), group([FILES_PANE_ID])]))
    })

    expect(screen.queryByTestId('files-body')).toBeNull()
    expect(screen.getByRole('button', { name: 'Show files' })).toBe(filesToggle)

    fireEvent.click(filesToggle)
    expect(screen.getByTestId('files-body')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Hide files' })).toBe(filesToggle)
  })

  it('removes reveal listeners and clears transient state after unmount', () => {
    const { getByTestId, unmount } = render(<NarrowOverlays />)

    revealPane(FILES_PANE_ID)
    expect(getByTestId('files-body')).toBeTruthy()

    unmount()
    expect($narrowOverlayReveal.get()).toBeNull()

    revealPane(FILES_PANE_ID)
    expect($narrowOverlayReveal.get()).toBeNull()
  })
})

describe('narrow overlay of a stacked zone', () => {
  it('mirrors the zone tab strip so every stacked collapsible stays reachable', () => {
    const { getByTestId, queryByTestId } = render(<NarrowOverlays />)

    revealPane('sessions')

    // Both zone-mates surface as tabs; the revealed pane's body is on screen.
    expect(overlayTab('sessions')).toBeTruthy()
    expect(overlayTab('bots')).toBeTruthy()
    expect(getByTestId('sessions-body')).toBeTruthy()
    expect(queryByTestId('bots-body')).toBeNull()

    // Clicking the BOTS tab swaps the overlay to the docked pane.
    fireEvent.pointerDown(overlayTab('bots')!, { button: 0 })
    expect(getByTestId('bots-body')).toBeTruthy()
    expect(queryByTestId('sessions-body')).toBeNull()
  })

  it('keeps the stripless form for a zone with a single collapsible', () => {
    // Direct set: declareDefaultTree only ADOPTS into an existing tree — it
    // would keep the beforeEach zone (with bots) instead of replacing it.
    $layoutTree.set(split('row', [group(['sessions']), group(['workspace'])]))

    const { getByTestId } = render(<NarrowOverlays />)

    revealPane('sessions')

    expect(getByTestId('sessions-body')).toBeTruthy()
    expect(overlayTab('sessions')).toBeNull()
  })
})
