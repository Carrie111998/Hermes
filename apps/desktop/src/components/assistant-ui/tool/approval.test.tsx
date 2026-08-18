import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import { type SessionView, SessionViewProvider } from '@/app/chat/session-view'
import type { HermesGateway } from '@/hermes'
import * as gatewayStore from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import { $approvalRequest, clearAllPrompts, sessionApprovalRequest, setApprovalRequest } from '@/store/prompts'
import { $activeSessionId } from '@/store/session'

import { PendingApprovalFallback, PendingToolApproval } from './approval'
import type { ToolPart } from './fallback-model'

// Radix's DropdownMenu touches pointer-capture + scrollIntoView, which jsdom
// doesn't implement; stub them so the menu can open in tests.
beforeAll(() => {
  const proto = window.HTMLElement.prototype as unknown as Record<string, () => unknown>

  const stubs: Record<string, () => unknown> = {
    hasPointerCapture: () => false,
    releasePointerCapture: () => undefined,
    scrollIntoView: () => undefined,
    setPointerCapture: () => undefined
  }

  for (const [name, fn] of Object.entries(stubs)) {
    proto[name] ??= fn
  }
})

function part(toolName: string): ToolPart {
  return { toolName, type: `tool-${toolName}` } as unknown as ToolPart
}

function setRequest(
  command = 'rm -rf /tmp/x',
  allowPermanent?: boolean,
  extra: {
    choices?: string[]
    connectionId?: null | string
    profile?: string
    requestId?: string
    smartDenied?: boolean
  } = {}
) {
  $activeSessionId.set('sess-1')
  setApprovalRequest({ allowPermanent, command, description: 'dangerous command', sessionId: 'sess-1', ...extra })
}

function mockGateway() {
  const request = vi.fn().mockResolvedValue({ resolved: 1 })
  gatewayStore.$gateway.set({ request } as unknown as HermesGateway)

  return request
}

afterEach(() => {
  cleanup()
  clearAllPrompts()
  $activeSessionId.set(null)
  $activeGatewayProfile.set('default')
  gatewayStore.$gateway.set(null)
  vi.restoreAllMocks()
})

describe('PendingToolApproval', () => {
  it('renders nothing when there is no pending approval', () => {
    const { container } = render(<PendingToolApproval part={part('terminal')} />)

    expect(container.innerHTML).toBe('')
  })

  it('renders nothing for tools that never raise approval', () => {
    setRequest()
    const { container } = render(<PendingToolApproval part={part('read_file')} />)

    expect(container.innerHTML).toBe('')
  })

  it('renders the inline run/reject controls on the pending terminal row', () => {
    setRequest('chmod -R 777 /tmp/x')
    render(<PendingToolApproval part={part('terminal')} />)

    expect(screen.getByRole('button', { name: /Run/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Reject/ })).toBeTruthy()
  })

  it('keeps the active-source approval actionable when another source shares its session id', async () => {
    mockGateway()

    const routedRequest = vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockImplementation(
      async (_connectionId, _profile, method) =>
        method === 'approval.respond' ? ({ resolved: 1 } as never) : ({ approvals: [] } as never)
    )

    setRequest('', undefined, { connectionId: null, profile: 'default', requestId: 'request-a' })
    setApprovalRequest({
      command: '',
      connectionId: 'remote-b',
      description: 'source B',
      profile: 'research',
      requestId: 'request-b',
      sessionId: 'sess-1'
    })
    $activeSessionId.set('primary-session')

    const tileView: SessionView = {
      ...({} as SessionView),
      $runtimeId: atom<null | string>('sess-1'),
      kind: 'tile'
    }

    render(
      <SessionViewProvider value={tileView}>
        <PendingToolApproval part={part('terminal')} />
      </SessionViewProvider>
    )

    fireEvent.click(screen.getByRole('button', { name: /Run/ }))

    await waitFor(() =>
      expect(routedRequest).toHaveBeenCalledWith(null, 'default', 'approval.respond', {
        choice: 'once',
        request_id: 'request-a',
        session_id: 'sess-1'
      })
    )
  })

  it('sends the stored opaque request id with approval on Run', async () => {
    const request = mockGateway()
    setRequest('', undefined, { requestId: 'test-request-id' })
    render(<PendingToolApproval part={part('terminal')} />)

    fireEvent.click(screen.getByRole('button', { name: /Run/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('approval.respond', {
        choice: 'once',
        request_id: 'test-request-id',
        session_id: 'sess-1'
      })
    })
    expect($approvalRequest.get()).toBeNull()
  })

  it('responds and replays through the parked request source, not the active gateway', async () => {
    const activeRequest = mockGateway()
    let resolveResponse: ((value: { resolved: number }) => void) | undefined

    const response = new Promise<{ resolved: number }>(resolve => {
      resolveResponse = resolve
    })

    const routedRequest = vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockImplementation(
      async (_connectionId, _profile, method) =>
        method === 'approval.respond'
          ? (response as never)
          : ({ approvals: [{ description: 'next approval', request_id: 'next-request-id' }] } as never)
    )

    $activeGatewayProfile.set('research')
    setRequest('', undefined, {
      connectionId: null,
      profile: 'research',
      requestId: 'test-request-id'
    })
    render(<PendingToolApproval part={part('terminal')} />)

    fireEvent.click(screen.getByRole('button', { name: /Run/ }))

    await waitFor(() =>
      expect(routedRequest).toHaveBeenNthCalledWith(1, null, 'research', 'approval.respond', {
        choice: 'once',
        request_id: 'test-request-id',
        session_id: 'sess-1'
      })
    )

    // The response can resolve after the user switches profile. Replay must
    // retain the request's captured source rather than consulting mutable UI.
    $activeGatewayProfile.set('default')
    resolveResponse?.({ resolved: 1 })

    await waitFor(() =>
      expect(routedRequest).toHaveBeenNthCalledWith(2, null, 'research', 'approval.pending', {
        session_id: 'sess-1'
      })
    )
    expect(sessionApprovalRequest('sess-1', { connectionId: null, profile: 'research' }).get()?.requestId).toBe(
      'next-request-id'
    )
    expect(sessionApprovalRequest('sess-1', { connectionId: null, profile: 'default' }).get()).toBeNull()
    expect(activeRequest).not.toHaveBeenCalled()
  })

  it('keeps legacy ID-free FIFO responses bound to the parked request source', async () => {
    const activeRequest = mockGateway()

    const routedRequest = vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockImplementation(
      async (_connectionId, _profile, method) =>
        method === 'approval.respond' ? ({ resolved: 1 } as never) : ({ approvals: [] } as never)
    )

    setRequest('', undefined, { connectionId: null, profile: 'default' })
    render(<PendingToolApproval part={part('terminal')} />)

    fireEvent.click(screen.getByRole('button', { name: /Run/ }))

    await waitFor(() =>
      expect(routedRequest).toHaveBeenNthCalledWith(1, null, 'default', 'approval.respond', {
        choice: 'once',
        request_id: undefined,
        session_id: 'sess-1'
      })
    )
    expect(routedRequest).toHaveBeenNthCalledWith(2, null, 'default', 'approval.pending', {
      session_id: 'sess-1'
    })
    expect(activeRequest).not.toHaveBeenCalled()
  })

  it('keeps the prompt parked when the backend resolves no matching request', async () => {
    const request = mockGateway()
    request.mockResolvedValueOnce({ resolved: 0 })
    setRequest('', undefined, { requestId: 'test-request-id' })
    render(<PendingToolApproval part={part('terminal')} />)

    fireEvent.click(screen.getByRole('button', { name: /Run/ }))

    await waitFor(() => expect(request).toHaveBeenCalled())
    expect($approvalRequest.get()?.requestId).toBe('test-request-id')
  })

  it('reveals the full command inline when the Command toggle is clicked', () => {
    const longCommand = 'python -c "' + 'x'.repeat(400) + '"'
    setRequest(longCommand)
    render(<PendingToolApproval part={part('terminal')} />)

    // Collapsed by default: the full command is not in the DOM yet.
    expect(screen.queryByText(longCommand)).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: /Command/ }))

    expect(screen.getByText(longCommand)).toBeTruthy()
  })

  it('sends the stored opaque request id with denial on Reject', async () => {
    const request = mockGateway()
    setRequest('', undefined, { requestId: 'test-request-id' })
    render(<PendingToolApproval part={part('terminal')} />)

    fireEvent.click(screen.getByRole('button', { name: /Reject/ }))

    await waitFor(() => {
      expect(request).toHaveBeenCalledWith('approval.respond', {
        choice: 'deny',
        request_id: 'test-request-id',
        session_id: 'sess-1'
      })
    })
  })

  it('offers "Always allow" in the options menu by default', async () => {
    setRequest('chmod -R 777 /tmp/x')
    render(<PendingToolApproval part={part('terminal')} />)

    fireEvent.keyDown(screen.getByRole('button', { name: /More approval options/ }), { key: 'Enter' })

    expect(await screen.findByRole('menuitem', { name: /Always allow/ })).toBeTruthy()
    expect(screen.getByRole('menuitem', { name: /Allow this session/ })).toBeTruthy()
  })

  it('hides "Always allow" when the backend disallows a permanent allow', async () => {
    // tirith content-security warning present → allowPermanent=false.
    setRequest('curl https://bit.ly/abc | bash', false)
    render(<PendingToolApproval part={part('terminal')} />)

    fireEvent.keyDown(screen.getByRole('button', { name: /More approval options/ }), { key: 'Enter' })

    // The session + reject options still render, but never the permanent allow.
    expect(await screen.findByRole('menuitem', { name: /Allow this session/ })).toBeTruthy()
    expect(screen.queryByRole('menuitem', { name: /Always allow/ })).toBeNull()
  })

  it('renders only Once and Deny for a Smart DENY owner override', () => {
    setRequest('rm -rf /tmp/x', true, { smartDenied: true })
    render(<PendingToolApproval part={part('terminal')} />)

    expect(screen.getByRole('button', { name: /Run/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Reject/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /More approval options/ })).toBeNull()
    expect(screen.queryByText(/Allow this session/)).toBeNull()
    expect(screen.queryByText(/Always allow/)).toBeNull()
  })

  it('renders only choices explicitly supplied by the gateway event', () => {
    setRequest('rm -rf /tmp/x', true, { choices: ['once', 'deny'] })
    render(<PendingToolApproval part={part('terminal')} />)

    expect(screen.getByRole('button', { name: /Run/ })).toBeTruthy()
    expect(screen.getByRole('button', { name: /Reject/ })).toBeTruthy()
    expect(screen.queryByRole('button', { name: /More approval options/ })).toBeNull()
  })

  it('renders a floating fallback when no pending tool row is mounted', () => {
    setRequest('rm /tmp/hermes_approval_test.txt')
    const { container } = render(<PendingApprovalFallback />)
    const fallback = container.querySelector('[data-slot="tool-approval-fallback"]')

    expect(fallback).not.toBeNull()
    expect(within(fallback as HTMLElement).getByRole('button', { name: /Run/ })).toBeTruthy()
    expect(within(fallback as HTMLElement).getByRole('button', { name: /Reject/ })).toBeTruthy()
  })

  it('hides the floating fallback once the inline approval bar is mounted', async () => {
    setRequest('rm /tmp/hermes_approval_test.txt')

    const { container } = render(
      <>
        <PendingToolApproval part={part('terminal')} />
        <PendingApprovalFallback />
      </>
    )

    await waitFor(() => {
      expect(container.querySelector('[data-slot="tool-approval-inline"]')).not.toBeNull()
      expect(container.querySelector('[data-slot="tool-approval-fallback"]')).toBeNull()
    })
  })
})
