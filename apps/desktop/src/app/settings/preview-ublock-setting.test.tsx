// @vitest-environment jsdom
import { act, cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { notifyError } from '@/store/notifications'
import { $previewUblock } from '@/store/preview-ublock'

import { PreviewUblockSetting } from './preview-ublock-setting'

const copy = {
  description:
    'Downloads uBlock Origin Lite from its official GitHub release, blocks ads and trackers only in Preview, and keeps it locally for later use.',
  downloading: 'Downloading and enabling uBlock Origin Lite…',
  failure: 'Could not update the Preview content-blocking setting.',
  label: 'Enable uBlock Origin Lite in Preview'
}

function state(enabled: boolean) {
  return {
    enabled,
    available: enabled,
    dashboardUrl: enabled ? 'chrome-extension://ublock/dashboard.html' : null,
    extensionId: enabled ? 'ublock' : null,
    rulesetsReady: enabled,
    version: enabled ? '2026.825.1619' : null
  }
}

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      settings: {
        config: {
          previewUblockDescription: copy.description,
          previewUblockDownloading: copy.downloading,
          previewUblockFailure: copy.failure,
          previewUblockTitle: copy.label
        }
      }
    }
  })
}))

vi.mock('@/store/notifications', () => ({ notifyError: vi.fn() }))

describe('PreviewUblockSetting', () => {
  beforeEach(() => {
    $previewUblock.set(state(false))
    window.hermesDesktop = {
      previewUblock: {
        getState: vi.fn().mockResolvedValue(state(false)),
        setEnabled: vi.fn().mockResolvedValue(state(false))
      }
    } as any
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders disabled by default', () => {
    render(<PreviewUblockSetting />)

    expect(screen.getByRole('switch', { name: copy.label }).getAttribute('data-state')).toBe('unchecked')
    expect(screen.getByText(copy.description)).toBeTruthy()
  })

  it('shows the download status while enabling', async () => {
    let resolve: ((value: ReturnType<typeof state>) => void) | undefined
    $previewUblock.set(state(false))
    window.hermesDesktop.previewUblock.setEnabled = vi.fn().mockImplementation(
      () =>
        new Promise<ReturnType<typeof state>>(nextResolve => {
          resolve = nextResolve
        })
    )

    render(<PreviewUblockSetting />)
    const toggle = screen.getByRole('switch', { name: copy.label })
    fireEvent.click(toggle)

    expect(toggle.hasAttribute('disabled')).toBe(true)
    expect(screen.getByText(copy.downloading)).toBeTruthy()
    expect(toggle.getAttribute('data-state')).toBe('unchecked')

    await act(async () => resolve?.(state(true)))
    expect(toggle.getAttribute('data-state')).toBe('checked')
  })

  it('disables the switch while turning uBlock off and mirrors the result', async () => {
    let resolve: ((value: ReturnType<typeof state>) => void) | undefined

    const setEnabled = vi.fn().mockImplementation(
      () =>
        new Promise<ReturnType<typeof state>>(nextResolve => {
          resolve = nextResolve
        })
    )

    $previewUblock.set(state(true))
    window.hermesDesktop.previewUblock.getState = vi.fn().mockResolvedValue(state(true))
    window.hermesDesktop.previewUblock.setEnabled = setEnabled

    render(<PreviewUblockSetting />)
    const toggle = screen.getByRole('switch', { name: copy.label })

    fireEvent.click(toggle)
    expect(setEnabled).toHaveBeenCalledWith(false)
    expect(toggle.hasAttribute('disabled')).toBe(true)

    await act(async () => {
      resolve?.(state(false))
    })

    expect(toggle.getAttribute('data-state')).toBe('unchecked')
    expect(toggle.hasAttribute('disabled')).toBe(false)
    expect($previewUblock.get()).toMatchObject({ enabled: false, available: false })
  })

  it('reports a failed toggle without changing the enabled state', async () => {
    const error = new Error('IPC unavailable')
    $previewUblock.set(state(true))
    window.hermesDesktop.previewUblock.getState = vi.fn().mockResolvedValue(state(true))
    window.hermesDesktop.previewUblock.setEnabled = vi.fn().mockRejectedValue(error)

    render(<PreviewUblockSetting />)
    fireEvent.click(screen.getByRole('switch', { name: copy.label }))

    await act(async () => {
      await Promise.resolve()
    })

    expect(screen.getByRole('switch', { name: copy.label }).getAttribute('data-state')).toBe('checked')
    expect(notifyError).toHaveBeenCalledWith(error, copy.failure)
  })
})
