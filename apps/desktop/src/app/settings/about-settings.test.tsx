import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $updateOverlayOpen, $updateOverlayTarget } from '@/store/updates'

afterEach(() => {
  cleanup()
  $updateOverlayOpen.set(false)
  Reflect.deleteProperty(window, 'hermesDesktop')
  vi.restoreAllMocks()
})

describe('AboutSettings', () => {
  it('keeps the Electric Sheep client updater manually reachable in managed mode', async () => {
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        eva: {
          status: vi.fn().mockResolvedValue({
            account: null,
            enrollment: null,
            signedIn: true,
            updateChannel: 'managed-beta'
          })
        },
        getVersion: vi.fn().mockResolvedValue({
          appVersion: '2026.7.20-es.11',
          commit: 'release',
          electron: 'test',
          hermes: null,
          node: 'test'
        })
      },
      writable: true
    })

    const { AboutSettings } = await import('./about-settings')
    render(<AboutSettings />)

    fireEvent.click(screen.getByRole('button', { name: 'Check now' }))

    expect($updateOverlayOpen.get()).toBe(true)
    expect($updateOverlayTarget.get()).toBe('client')
  })
})
