import { beforeEach, describe, expect, it, vi } from 'vitest'

import {
  $newWindowBackendPolicy,
  $windowBackendPickerOpen,
  closeWindowBackendPicker,
  requestNewWindow,
  setNewWindowBackendPolicy
} from './window-backend-picker'
import { openNewWindow } from './windows'

vi.mock('./windows', () => ({
  openNewWindow: vi.fn().mockResolvedValue(true)
}))

const openWindow = vi.mocked(openNewWindow)

describe('new window backend policy', () => {
  beforeEach(() => {
    window.localStorage.clear()
    openWindow.mockClear()
    closeWindowBackendPicker()
    setNewWindowBackendPolicy('inherit')
  })

  it('inherits the current window backend by default', async () => {
    expect($newWindowBackendPolicy.get()).toBe('inherit')

    await requestNewWindow()

    expect(openWindow).toHaveBeenCalledWith()
    expect($windowBackendPickerOpen.get()).toBe(false)
  })

  it('can always open on the primary backend', async () => {
    setNewWindowBackendPolicy('primary')

    await requestNewWindow()

    expect(openWindow).toHaveBeenCalledWith('primary')
  })

  it('can ask for a backend on every ordinary new-window request', async () => {
    setNewWindowBackendPolicy('ask')

    await requestNewWindow()

    expect(openWindow).not.toHaveBeenCalled()
    expect($windowBackendPickerOpen.get()).toBe(true)
  })

  it('persists the selected policy for other windows', () => {
    setNewWindowBackendPolicy('ask')

    expect(window.localStorage.getItem('hermes.desktop.newWindowBackendPolicy')).toBe('ask')
  })

  it('adopts policy changes made by another desktop window', () => {
    window.dispatchEvent(new StorageEvent('storage', {
      key: 'hermes.desktop.newWindowBackendPolicy',
      newValue: 'primary'
    }))

    expect($newWindowBackendPolicy.get()).toBe('primary')
  })
})
