import { renderHook } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { refreshFleetRoster } from '@/store/fleet-roster'

import { useFleetRoster } from './use-fleet-roster'

vi.mock('@/store/fleet-roster', () => ({
  refreshFleetRoster: vi.fn(async () => undefined)
}))

afterEach(() => {
  vi.clearAllMocks()
  delete (window as { hermesDesktop?: unknown }).hermesDesktop
})

it('force-refreshes when Electron reports a completed startup roster dial', () => {
  const rosterChanged = { current: null as null | (() => void) }

  const unsubscribe = vi.fn()

  ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
    onAgentRosterChanged: vi.fn(callback => {
      rosterChanged.current = callback

      return unsubscribe
    })
  }

  const view = renderHook(() => useFleetRoster(true))

  vi.mocked(refreshFleetRoster).mockClear()
  expect(rosterChanged.current).not.toBeNull()
  rosterChanged.current?.()
  expect(refreshFleetRoster).toHaveBeenCalledWith({ force: true })

  view.unmount()
  expect(unsubscribe).toHaveBeenCalledOnce()
})
