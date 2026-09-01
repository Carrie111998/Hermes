import { cleanup, renderHook } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { useStatusbarItems } from '@/app/shell/hooks/use-statusbar-items'
import {
  $selectedStoredSessionId,
  $sessions,
  releaseWorkspaceCwdOwner,
  setCurrentCwdTransient,
  setWorkspaceCwdOwner
} from '@/store/session'
import { stubMenuDomApis, stubResizeObserver } from '@/test/jsdom'

beforeAll(() => {
  stubResizeObserver()
  stubMenuDomApis()
})

const row = (id: string, cwd: null | string) =>
  ({
    archived: false,
    cwd,
    ended_at: null,
    id,
    input_tokens: 0,
    is_active: true,
    last_active: 0,
    message_count: 1,
    model: null,
    output_tokens: 0,
    started_at: 0,
    title: id
  }) as never

/** The workspace item's rendered label, or undefined when the item is hidden. */
function workspaceLabel() {
  const { result } = renderHook(
    () =>
      useStatusbarItems({
        agentsOpen: false,
        chatOpen: true,
        commandCenterOpen: false,
        extraLeftItems: [],
        extraRightItems: [],
        freshDraftReady: false,
        gatewayState: 'ready',
        inferenceStatus: null,
        openAgents: vi.fn(),
        openCommandCenterSection: vi.fn(),
        requestGateway: vi.fn(),
        statusSnapshot: null,
        toggleCommandCenter: vi.fn()
      }),
    { wrapper: ({ children }) => <MemoryRouter>{children}</MemoryRouter> }
  )

  const items = [...result.current.leftStatusbarItems, ...result.current.statusbarItems]
  const workspace = items.find(entry => entry.id === 'workspace-cwd')

  return workspace?.hidden ? undefined : workspace?.label
}

describe('useStatusbarItems workspace item', () => {
  beforeEach(() => {
    setCurrentCwdTransient('')
    $selectedStoredSessionId.set(null)
    $sessions.set([])
    setWorkspaceCwdOwner(null)
  })

  afterEach(() => {
    cleanup()
    setCurrentCwdTransient('')
    $selectedStoredSessionId.set(null)
    $sessions.set([])
    setWorkspaceCwdOwner(null)
  })

  // The regression. `$currentCwd` is a shared singleton: during a conversation
  // switch it still holds the PREVIOUS chat's folder until `session.resume`
  // settles, which is exactly what the ownership marker exists to say
  // (ae6eb578bb). The Files pane and `$repoStatus` ask that question; this hook
  // did not, so it labelled the new conversation with the old project's name
  // while the pane beside it correctly showed nothing.
  it('does not label an un-owned singleton path (the previous conversation folder)', () => {
    setCurrentCwdTransient('/previous-project')
    $selectedStoredSessionId.set('sess-switching')
    $sessions.set([])
    releaseWorkspaceCwdOwner()

    expect(workspaceLabel()).toBeUndefined()
  })

  it('labels the singleton path once the selected conversation owns it', () => {
    setCurrentCwdTransient('/owned-project')
    $selectedStoredSessionId.set('sess-settled')
    $sessions.set([])
    setWorkspaceCwdOwner('sess-settled')

    expect(workspaceLabel()).toBe('owned-project')
  })

  it('labels a fresh draft, whose null owner matches its null selection', () => {
    setCurrentCwdTransient('/draft-target')
    $selectedStoredSessionId.set(null)
    setWorkspaceCwdOwner(null)

    expect(workspaceLabel()).toBe('draft-target')
  })

  // Ownership gates ONLY the singleton rung. A released marker says nothing
  // about the session's own stored row, so dropping the row too would blank a
  // workspace that is known — the failure mode 416e025c46 warns about.
  it('still labels from the stored row while ownership is released', () => {
    setCurrentCwdTransient('/previous-project')
    $selectedStoredSessionId.set('sess-known')
    $sessions.set([row('sess-known', '/its-own-project')])
    releaseWorkspaceCwdOwner()

    expect(workspaceLabel()).toBe('its-own-project')
  })
})
