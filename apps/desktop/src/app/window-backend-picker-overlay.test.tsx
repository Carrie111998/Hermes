import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import {
  closeWindowBackendPicker,
  openWindowBackendPicker
} from '@/store/window-backend-picker'
import { listWindowBackendTargets, openNewWindow } from '@/store/windows'

import { WindowBackendPickerOverlay } from './window-backend-picker-overlay'

vi.mock('@/store/windows', () => ({
  listWindowBackendTargets: vi.fn(),
  openNewWindow: vi.fn()
}))

class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)

Element.prototype.scrollIntoView = function scrollIntoView() {}

const listTargets = vi.mocked(listWindowBackendTargets)
const openWindow = vi.mocked(openNewWindow)

function renderOverlay() {
  return render(
    <I18nProvider configClient={null} initialLocale="en">
      <WindowBackendPickerOverlay />
    </I18nProvider>
  )
}

beforeEach(() => {
  listTargets.mockReset()
  openWindow.mockReset()
  closeWindowBackendPicker()
})

afterEach(() => {
  cleanup()
  closeWindowBackendPicker()
})

describe('WindowBackendPickerOverlay', () => {
  it('renders safe main-process labels without interpreting opaque target ids', async () => {
    listTargets.mockResolvedValue([
      { current: true, description: 'Main-selected primary description', id: 'primary', label: 'Main primary' },
      {
        current: false,
        description: 'Main-selected local description',
        id: 'forced-local-profile:coder',
        label: 'Main opaque choice'
      }
    ])

    renderOverlay()
    act(() => openWindowBackendPicker())

    expect(await screen.findByText('Main primary')).toBeTruthy()
    expect(screen.getByText('Main opaque choice')).toBeTruthy()
    expect(screen.queryByText('Local — coder')).toBeNull()
    expect(screen.getByText('Current')).toBeTruthy()
  })

  it('opens the selected target and closes only on success', async () => {
    listTargets.mockResolvedValue([
      { current: true, description: 'Primary description', id: 'primary', label: 'Main primary' }
    ])
    openWindow.mockResolvedValue(true)

    renderOverlay()
    act(() => openWindowBackendPicker())
    fireEvent.click(await screen.findByText('Main primary'))

    await waitFor(() => expect(openWindow).toHaveBeenCalledWith('primary'))
    await waitFor(() => expect(screen.queryByText('Choose a backend')).toBeNull())
  })

  it('stays open when the main process rejects the target', async () => {
    listTargets.mockResolvedValue([
      { current: true, description: 'Primary description', id: 'primary', label: 'Main primary' }
    ])
    openWindow.mockResolvedValue(false)

    renderOverlay()
    act(() => openWindowBackendPicker())
    fireEvent.click(await screen.findByText('Main primary'))

    await waitFor(() => expect(openWindow).toHaveBeenCalledWith('primary'))
    expect(screen.getByText('Choose a backend')).toBeTruthy()
  })

  it('keeps a dismissible error state when choices fail to load', async () => {
    listTargets.mockRejectedValue(new Error('offline'))

    renderOverlay()
    act(() => openWindowBackendPicker())

    expect(await screen.findByText('Could not load backend choices.')).toBeTruthy()
    expect(screen.getByText('Cancel')).toBeTruthy()
  })
})
