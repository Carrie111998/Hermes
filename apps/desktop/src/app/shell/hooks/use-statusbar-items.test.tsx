import { cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $connection, $currentCwd } from '@/store/session'

import { useStatusbarItems } from './use-statusbar-items'

afterEach(() => {
  cleanup()
  $connection.set(null)
  $currentCwd.set('')
})

const options = {
  agentsOpen: false,
  chatOpen: true,
  commandCenterOpen: false,
  extraLeftItems: [],
  extraRightItems: [],
  freshDraftReady: true,
  gatewayState: 'open',
  inferenceStatus: null,
  openAgents: vi.fn(),
  openCommandCenterSection: vi.fn(),
  requestGateway: vi.fn(() => new Promise<never>(() => undefined)),
  statusSnapshot: null,
  toggleCommandCenter: vi.fn()
} as const

function workspaceMenuIds() {
  const { result } = renderHook(() => useStatusbarItems(options))
  const workspace = result.current.leftStatusbarItems.find(item => item.id === 'workspace-cwd')

  return workspace?.menuItems?.map(item => item.id)
}

describe('useStatusbarItems workspace menu', () => {
  it('keeps OS reveal, copy path, and in-app reveal in local mode', () => {
    $connection.set({ mode: 'local' } as never)
    $currentCwd.set('/repo')

    expect(workspaceMenuIds()).toEqual(['copy-workspace-path', 'reveal-workspace-finder', 'reveal-workspace-sidebar'])
  })

  it('hides OS reveal but keeps copy path and in-app reveal in remote mode', () => {
    $connection.set({ mode: 'remote' } as never)
    $currentCwd.set('/remote/repo')

    expect(workspaceMenuIds()).toEqual(['copy-workspace-path', 'reveal-workspace-sidebar'])
  })
})
