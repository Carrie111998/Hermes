import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { HermesGateway } from '@/hermes'

import { $gateway, setPrimaryGateway } from './gateway'
import {
  dispatchNativeNotification,
  NATIVE_NOTIFICATION_KINDS,
  respondToApprovalAction,
  sendTestNativeNotification,
  setNativeNotifyEnabled,
  setNativeNotifyKind
} from './native-notifications'
import { __resetNativeNotifyBaselineForTests, markNativeNotifyBaseline } from './notify-baseline'
import { $approvalRequest, clearAllPrompts, setApprovalRequest } from './prompts'
import { $activeSessionId, setActiveSessionId } from './session'

const desktopWindow = window as unknown as { hermesDesktop?: Window['hermesDesktop'] }
const initialHermesDesktop = desktopWindow.hermesDesktop

const notify = vi.fn().mockResolvedValue(true)

function approval(approvalId: string, command: string, profile = 'default') {
  return { approvalId, command, description: command, profile, sessionId: 'bg' }
}

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
  clearAllPrompts()
  setPrimaryGateway(null)

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

describe('dispatchNativeNotification throttle', () => {
  it('collapses duplicate kind+session within the throttle window', () => {
    const sessionId = freshSession()
    setActiveSessionId(sessionId)
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done' })
    dispatchNativeNotification({ kind: 'turnDone', sessionId, title: 'done again' })
    expect(notify).toHaveBeenCalledTimes(1)
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
  const request = vi.fn().mockResolvedValue({ resolved: true })

  beforeEach(() => {
    request.mockClear()
    setPrimaryGateway({ connectionState: 'open', request } as unknown as HermesGateway)
  })

  afterEach(() => {
    setPrimaryGateway(null)
  })

  it('approves via approval.respond {choice: "once"} and clears the prompt', async () => {
    setActiveSessionId('bg')
    setApprovalRequest(approval('approval-a', 'rm -rf /'))

    await respondToApprovalAction('bg', 'approval-a', 'approve')

    expect(request).toHaveBeenCalledWith('approval.respond', {
      approval_id: 'approval-a',
      choice: 'once',
      session_id: 'bg'
    })
    expect($approvalRequest.get()).toBeNull()
  })

  it('rejects via approval.respond {choice: "deny"}', async () => {
    setApprovalRequest(approval('approval-a', 'first'))
    await respondToApprovalAction('bg', 'approval-a', 'reject')
    expect(request).toHaveBeenCalledWith('approval.respond', {
      approval_id: 'approval-a',
      choice: 'deny',
      session_id: 'bg'
    })
  })

  it('ignores unknown action ids', async () => {
    await respondToApprovalAction('bg', 'approval-a', 'snooze')
    expect(request).not.toHaveBeenCalled()
  })

  it('no-ops without a gateway', async () => {
    setApprovalRequest(approval('approval-a', 'first'))
    setPrimaryGateway(null)
    await respondToApprovalAction('bg', 'approval-a', 'approve')
    expect(request).not.toHaveBeenCalled()
  })

  it('does not resolve a queued approval before the queue head', async () => {
    setActiveSessionId('bg')
    setApprovalRequest(approval('approval-a', 'first'))
    setApprovalRequest(approval('approval-b', 'second'))

    await respondToApprovalAction('bg', 'approval-b', 'approve')

    expect(request).not.toHaveBeenCalled()
    expect($approvalRequest.get()?.approvalId).toBe('approval-a')
  })

  it('does not clear a newer request after a stale notification action completes', async () => {
    let finish: ((value: { resolved: number }) => void) | undefined
    request.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          finish = resolve
        })
    )
    setActiveSessionId('bg')
    setApprovalRequest(approval('approval-a', 'first'))

    const response = respondToApprovalAction('bg', 'approval-a', 'approve')
    setApprovalRequest(approval('approval-b', 'second'))
    finish?.({ resolved: 0 })
    await response

    expect($approvalRequest.get()?.approvalId).toBe('approval-b')
  })

  it('rejects a notification action whose profile does not match the request', async () => {
    setActiveSessionId('bg')
    setApprovalRequest(approval('approval-a', 'first'))

    await respondToApprovalAction('bg', 'approval-a', 'approve', 'other')

    expect(request).not.toHaveBeenCalled()
    expect($approvalRequest.get()?.approvalId).toBe('approval-a')
  })

  it('uses the approval profile gateway instead of the active gateway', async () => {
    const activeRequest = vi.fn()
    setPrimaryGateway({ connectionState: 'open', request } as unknown as HermesGateway, 'work')
    $gateway.set({ connectionState: 'open', request: activeRequest } as unknown as HermesGateway)
    setApprovalRequest(approval('approval-profile', 'first', 'work'))

    await respondToApprovalAction('bg', 'approval-profile', 'approve', 'work')

    expect(request).toHaveBeenCalledWith('approval.respond', {
      approval_id: 'approval-profile',
      choice: 'once',
      session_id: 'bg'
    })
    expect(activeRequest).not.toHaveBeenCalled()
  })

  it('notifies the next request only after the queue head resolves', async () => {
    setActiveSessionId('bg')
    setApprovalRequest(approval('approval-promote-a', 'first'))
    setApprovalRequest(approval('approval-promote-b', 'second'))
    notify.mockClear()

    await respondToApprovalAction('bg', 'approval-promote-a', 'approve')

    expect(request).toHaveBeenCalledTimes(1)
    expect($approvalRequest.get()?.approvalId).toBe('approval-promote-b')
    expect(notify).toHaveBeenCalledWith(
      expect.objectContaining({
        actions: expect.arrayContaining([expect.objectContaining({ approvalId: 'approval-promote-b' })]),
        body: 'second',
        profile: 'default',
        sessionId: 'bg'
      })
    )
  })
})
