import { act, cleanup, render } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $primarySessionOwnerIntent } from '@/store/session'
import type { SessionOwnerRoute } from '@/store/session-request-router'
import { $sessionTiles, setSessionTileDelegate } from '@/store/session-states'

const menuCapture = vi.hoisted(() => ({ props: null as null | Record<string, unknown> }))

vi.mock('./sidebar/session-actions-menu', () => ({
  SessionContextMenu: (props: Record<string, unknown> & { children: React.ReactNode }) => {
    menuCapture.props = props

    return props.children
  }
}))

import { SessionTabMenu } from './session-tile'

const ownerRoute: SessionOwnerRoute = {
  connectionId: 'source-b',
  mode: 'remote',
  profile: 'worker'
}

function delegate() {
  return {
    archiveSession: vi.fn(async () => undefined),
    branchSession: vi.fn(async () => undefined),
    deleteSession: vi.fn(async () => undefined),
    executeSlash: vi.fn(async () => undefined),
    interruptSession: vi.fn(async () => undefined),
    resumeTile: vi.fn(async () => 'runtime'),
    submitToSession: vi.fn(async () => undefined),
    updateSession: vi.fn()
  }
}

afterEach(() => {
  cleanup()
  menuCapture.props = null
  $primarySessionOwnerIntent.set(null)
  $sessionTiles.set([])
})

describe('SessionTabMenu exact-owner action transport', () => {
  it('carries a duplicate tile owner through archive, branch, and delete', () => {
    const actions = delegate()
    setSessionTileDelegate(actions as never)
    $sessionTiles.set([{ ownerRoute, storedSessionId: 'duplicate-id' }])

    render(
      <SessionTabMenu ownerRoute={ownerRoute} storedSessionId="duplicate-id" tabPaneId="qualified-pane">
        <button type="button">Tab</button>
      </SessionTabMenu>
    )

    act(() => {
      ;(menuCapture.props?.onArchive as () => void)()
      ;(menuCapture.props?.onBranch as () => void)()
      ;(menuCapture.props?.onDelete as () => void)()
    })

    expect(actions.archiveSession).toHaveBeenCalledWith('duplicate-id', ownerRoute)
    expect(actions.branchSession).toHaveBeenCalledWith('duplicate-id', ownerRoute)
    expect(actions.deleteSession).toHaveBeenCalledWith('duplicate-id', ownerRoute)
  })

  it('preserves the legacy ownerless delegate call shape', () => {
    const actions = delegate()
    setSessionTileDelegate(actions as never)

    render(
      <SessionTabMenu storedSessionId="legacy-id" tabPaneId="legacy-pane">
        <button type="button">Tab</button>
      </SessionTabMenu>
    )

    act(() => {
      ;(menuCapture.props?.onArchive as () => void)()
      ;(menuCapture.props?.onBranch as () => void)()
      ;(menuCapture.props?.onDelete as () => void)()
    })

    expect(actions.archiveSession).toHaveBeenCalledWith('legacy-id')
    expect(actions.branchSession).toHaveBeenCalledWith('legacy-id')
    expect(actions.deleteSession).toHaveBeenCalledWith('legacy-id')
  })
})
