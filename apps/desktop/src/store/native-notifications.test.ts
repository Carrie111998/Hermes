import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('./gateway', async () => {
  const { atom } = await import('nanostores')

  return { $gateway: atom(null), activeGatewayConnectionId: vi.fn(() => null), requestGatewayForAgent: vi.fn() }
})

import { requestGatewayForAgent } from './gateway'
import {
  dispatchNativeNotification,
  dispatchPluginNativeNotification,
  NATIVE_NOTIFICATION_KINDS,
  respondToApprovalAction,
  sendTestNativeNotification,
  setNativeNotifyEnabled,
  setNativeNotifyKind
} from './native-notifications'
import { __resetNativeNotifyBaselineForTests, markNativeNotifyBaseline } from './notify-baseline'
import { $activeGatewayProfile } from './profile'
import { clearAllPrompts, sessionApprovalRequest, setApprovalRequest } from './prompts'
import { $activeSessionId, setActiveSessionId } from './session'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

const notify = vi.fn().mockResolvedValue(true)

function setWindowState({ focused = true, hidden = false }: { focused?: boolean; hidden?: boolean }) {
  Object.defineProperty(document, 'hidden', { configurable: true, value: hidden })
  Object.defineProperty(document, 'hasFocus', { configurable: true, value: () => focused })
}

let counter = 0

// Unique session id per call dodges the per-(kind,session) throttle so each
// assertion starts clean.
function freshSession(): string {
  counter += 1

  return `session-${counter}`
}

beforeEach(() => {
  notify.mockClear()
  desktopWindow.hermesDesktop = { notify } as unknown as Window['hermesDesktop']
  setNativeNotifyEnabled(true)

  for (const kind of NATIVE_NOTIFICATION_KINDS) {
    setNativeNotifyKind(kind, true)
  }

  setActiveSessionId(null)
  setWindowState({ focused: false, hidden: true })
  __resetNativeNotifyBaselineForTests()
})

afterEach(() => {
  if (initialHermesDesktop) {
    desktopWindow.hermesDesktop = initialHermesDesktop
  } else {
    delete desktopWindow.hermesDesktop
  }
})

describe('dispatchNativeNotification focus gating', () => {
  it('fires a completion notification for the active session when the window is hidden', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('fires a completion notification when the window is visible but unfocused (alt-tab)', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setWindowState({ focused: false, hidden: false })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses a completion notification when the window is focused', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setWindowState({ focused: true, hidden: false })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('suppresses a completion notification for a non-active background session (no gateway spam)', () => {
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'turnDone', sessionId: 'busy-bot-session', title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('fires an attention notification for an off-screen session even when focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'approval', sessionId: 'background', title: 'approve' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses an attention notification for the active session when focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    dispatchNativeNotification({ kind: 'approval', sessionId: 'on-screen', title: 'approve' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('fires a foreign-source approval sharing the focused active session id', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('shared-session')
    $activeGatewayProfile.set('default')

    dispatchNativeNotification({
      approvalConnectionId: 'remote-b',
      approvalProfile: 'research',
      approvalRequestId: 'request-b',
      kind: 'approval',
      sessionId: 'shared-session',
      title: 'approve'
    })

    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('fires a global completion notification while away with no active session (pet gen)', () => {
    setActiveSessionId(null)
    dispatchNativeNotification({ global: true, kind: 'backgroundDone', title: 'Your pet hatched' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('suppresses a global notification when the window is focused', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId(null)
    dispatchNativeNotification({ global: true, kind: 'backgroundDone', title: 'Your pet hatched' })
    expect(notify).not.toHaveBeenCalled()
  })
})

describe('dispatchNativeNotification preferences', () => {
  it('suppresses everything when the master switch is off', () => {
    setNativeNotifyEnabled(false)
    dispatchNativeNotification({ kind: 'approval', sessionId: freshSession(), title: 'approve' })
    dispatchNativeNotification({ kind: 'turnDone', sessionId: freshSession(), title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('suppresses only the disabled kind', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    setNativeNotifyKind('turnDone', false)
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).not.toHaveBeenCalled()

    dispatchNativeNotification({ kind: 'turnError', sessionId, title: 'boom' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('forwards kind and sessionId to the bridge', () => {
    setActiveSessionId('abc')
    dispatchNativeNotification({ body: 'hi', kind: 'turnError', sessionId: 'abc', title: 'boom' })
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ body: 'hi', kind: 'turnError', sessionId: 'abc', title: 'boom' })
    )
  })

  it('forwards the opaque approval request id to the native action context', () => {
    const background = freshSession()
    setActiveSessionId('foreground')
    dispatchNativeNotification({
      approvalConnectionId: 'remote-a',
      approvalProfile: 'research',
      approvalRequestId: 'request-a',
      kind: 'approval',
      sessionId: background,
      title: 'approve'
    })
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        approvalConnectionId: 'remote-a',
        approvalProfile: 'research',
        approvalRequestId: 'request-a'
      })
    )
  })
})

describe('dispatchNativeNotification post-connect baseline', () => {
  it('suppresses a prompt replayed right after a socket opens', () => {
    markNativeNotifyBaseline()
    dispatchNativeNotification({ kind: 'approval', sessionId: freshSession(), title: 'approve' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('suppresses a completion replayed right after a socket opens', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    markNativeNotifyBaseline()
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('fires again once the window has passed', () => {
    vi.useFakeTimers()

    try {
      markNativeNotifyBaseline()
      vi.advanceTimersByTime(5000)
      dispatchNativeNotification({ kind: 'approval', sessionId: freshSession(), title: 'approve' })
      expect(notify).toHaveBeenCalledTimes(1)
    } finally {
      vi.useRealTimers()
    }
  })
})

describe('dispatchPluginNativeNotification', () => {
  it('fires while the user is away and tags the plugin id for dedupe', () => {
    dispatchPluginNativeNotification('index-network', { body: 'New match', title: 'Opportunity' })
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({ body: 'New match', kind: 'plugin', tag: 'index-network', title: 'Opportunity' })
    )
  })

  it('suppresses while the window is focused (the in-app toast covers foreground)', () => {
    setWindowState({ focused: true, hidden: false })
    dispatchPluginNativeNotification('focused-plugin', { title: 'Opportunity' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('is gated by the "plugin" kind preference', () => {
    setNativeNotifyKind('plugin', false)
    dispatchPluginNativeNotification('muted-plugin', { title: 'Opportunity' })
    expect(notify).not.toHaveBeenCalled()
  })

  it('throttles per plugin, so two plugins cannot collapse each other', () => {
    dispatchPluginNativeNotification('plugin-a', { title: 'a' })
    dispatchPluginNativeNotification('plugin-a', { title: 'a again' })
    dispatchPluginNativeNotification('plugin-b', { title: 'b' })
    expect(notify).toHaveBeenCalledTimes(2)
  })
})

describe('dispatchNativeNotification throttle', () => {
  it('collapses duplicate kind+session within the throttle window', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done again' })
    expect(notify).toHaveBeenCalledTimes(1)
  })

  it('does not collapse approvals from distinct source authorities sharing a session id', () => {
    const sessionId = freshSession()
    setActiveSessionId('another-session')

    dispatchNativeNotification({
      approvalConnectionId: 'remote-a',
      approvalProfile: 'research',
      approvalRequestId: 'request-a',
      kind: 'approval',
      sessionId,
      title: 'A'
    })
    dispatchNativeNotification({
      approvalConnectionId: 'remote-b',
      approvalProfile: 'research',
      approvalRequestId: 'request-b',
      kind: 'approval',
      sessionId,
      title: 'B'
    })

    expect(notify).toHaveBeenCalledTimes(2)
  })
})

describe('sendTestNativeNotification', () => {
  it('fires regardless of focus or active session', () => {
    setWindowState({ focused: true, hidden: false })
    setActiveSessionId('on-screen')
    sendTestNativeNotification('Hermes', 'works')
    expect(notify).toHaveBeenCalledTimes(1)
  })
})

describe('$activeSessionId wiring', () => {
  it('reflects the setter used for gating', () => {
    setActiveSessionId('xyz')
    expect($activeSessionId.get()).toBe('xyz')
  })
})

describe('respondToApprovalAction', () => {
  const routedRequest = vi.mocked(requestGatewayForAgent)
  const source = { connectionId: 'remote-a', profile: 'research' }

  beforeEach(() => {
    clearAllPrompts()
    routedRequest.mockReset()
    routedRequest.mockImplementation(async (_connectionId, _profile, method) =>
      method === 'approval.respond' ? ({ resolved: 1 } as never) : ({ approvals: [] } as never)
    )
    setActiveSessionId('bg')
  })

  it('approves the exact notification request id and clears only that prompt', async () => {
    setActiveSessionId('bg')
    setApprovalRequest({ ...source, command: '', description: 'redacted', requestId: 'test-request-id', sessionId: 'bg' })

    await respondToApprovalAction('bg', 'test-request-id', 'approve', source)

    expect(routedRequest).toHaveBeenNthCalledWith(1, 'remote-a', 'research', 'approval.respond', {
      choice: 'once',
      request_id: 'test-request-id',
      session_id: 'bg'
    })
    expect(routedRequest).toHaveBeenNthCalledWith(2, 'remote-a', 'research', 'approval.pending', {
      session_id: 'bg'
    })
    expect(sessionApprovalRequest('bg', source).get()).toBeNull()
  })

  it('parks the replayed approval under the original notification source', async () => {
    const sessionId = freshSession()

    $activeGatewayProfile.set('default')
    routedRequest.mockImplementation(async (_connectionId, _profile, method) => {
      if (method === 'approval.respond') {
        return { resolved: 1 } as never
      }

      return {
        approvals: [{ description: 'next approval', request_id: 'next-request-id' }]
      } as never
    })
    setApprovalRequest({ ...source, command: '', description: 'current', requestId: 'current-request-id', sessionId })

    await respondToApprovalAction(sessionId, 'current-request-id', 'approve', source)

    expect(sessionApprovalRequest(sessionId, source).get()?.requestId).toBe('next-request-id')
    expect(sessionApprovalRequest(sessionId, { connectionId: null, profile: 'default' }).get()).toBeNull()
  })

  it('does not let a stale notification action clear a newer prompt', async () => {
    setApprovalRequest({ ...source, command: '', description: 'newer', requestId: 'new-request-id', sessionId: 'bg' })

    await respondToApprovalAction('bg', 'old-request-id', 'approve', source)

    expect(routedRequest).toHaveBeenNthCalledWith(1, 'remote-a', 'research', 'approval.respond', {
      choice: 'once',
      request_id: 'old-request-id',
      session_id: 'bg'
    })
    expect(sessionApprovalRequest('bg', source).get()?.requestId).toBe('new-request-id')
  })

  it('keeps the exact prompt parked when the backend resolves nothing', async () => {
    routedRequest.mockResolvedValueOnce({ resolved: 0 } as never)
    setApprovalRequest({ ...source, command: '', description: 'redacted', requestId: 'test-request-id', sessionId: 'bg' })

    await respondToApprovalAction('bg', 'test-request-id', 'reject', source)

    expect(sessionApprovalRequest('bg', source).get()?.requestId).toBe('test-request-id')
    expect(routedRequest).toHaveBeenCalledTimes(1)
  })

  it('rejects with the exact notification request id', async () => {
    await respondToApprovalAction('bg', 'test-request-id', 'reject', source)

    expect(routedRequest).toHaveBeenNthCalledWith(1, 'remote-a', 'research', 'approval.respond', {
      choice: 'deny',
      request_id: 'test-request-id',
      session_id: 'bg'
    })
  })

  it('ignores unknown action ids', async () => {
    await respondToApprovalAction('bg', 'test-request-id', 'snooze', source)
    expect(routedRequest).not.toHaveBeenCalled()
  })

  it('leaves the prompt parked when the source-scoped gateway rejects', async () => {
    routedRequest.mockRejectedValueOnce(new Error('source unavailable'))
    setApprovalRequest({ ...source, command: '', description: 'redacted', requestId: 'test-request-id', sessionId: 'bg' })

    await respondToApprovalAction('bg', 'test-request-id', 'approve', source)

    expect(sessionApprovalRequest('bg', source).get()?.requestId).toBe('test-request-id')
  })
})
