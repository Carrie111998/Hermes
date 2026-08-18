import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it, vi } from 'vitest'

import {
  nativeNotificationDedupeKey,
  sendNativeNotificationAction,
  sendNativeNotificationFocus
} from './native-notification-action'

describe('sendNativeNotificationAction', () => {
  it('wires the Electron notification action to its IPC event sender', () => {
    const main = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'main.ts'), 'utf8')
    const handlerStart = main.indexOf("ipcMain.handle('hermes:notify'")
    const handler = main.slice(handlerStart, main.indexOf('notification.show()', handlerStart))

    expect(handler).toContain('sendNativeNotificationAction(event.sender, payload ?? {}, actions, index)')
  })

  it('returns the action to the originating renderer with exact approval authority', () => {
    const send = vi.fn()
    const sender = { isDestroyed: () => false, send }

    sendNativeNotificationAction(
      sender,
      {
        approvalConnectionId: 'remote-a',
        approvalProfile: 'research',
        approvalRequestId: 'request-a',
        sessionId: 'session-a'
      },
      [{ id: 'approve', text: 'Run' }],
      0
    )

    expect(send).toHaveBeenCalledWith('hermes:notification-action', {
      actionId: 'approve',
      connectionId: 'remote-a',
      profile: 'research',
      requestId: 'request-a',
      sessionId: 'session-a'
    })
  })

  it('does not send through a destroyed originating renderer', () => {
    const send = vi.fn()

    sendNativeNotificationAction(
      { isDestroyed: () => true, send },
      { approvalRequestId: 'request-a', sessionId: 'session-a' },
      [{ id: 'approve', text: 'Run' }],
      0
    )

    expect(send).not.toHaveBeenCalled()
  })

  it('ignores an action index without an opaque action id', () => {
    const send = vi.fn()

    sendNativeNotificationAction(
      { isDestroyed: () => false, send },
      { approvalRequestId: 'request-a', sessionId: 'session-a' },
      [{ id: '', text: 'Run' }],
      0
    )

    expect(send).not.toHaveBeenCalled()
  })
})

describe('nativeNotificationDedupeKey', () => {
  it('keeps same-session approvals distinct by source and request authority', () => {
    const sourceA = nativeNotificationDedupeKey({
      approvalConnectionId: 'remote-a',
      approvalProfile: 'research',
      approvalRequestId: 'request-a',
      kind: 'approval',
      sessionId: 'shared'
    })

    const sourceB = nativeNotificationDedupeKey({
      approvalConnectionId: 'remote-b',
      approvalProfile: 'research',
      approvalRequestId: 'request-b',
      kind: 'approval',
      sessionId: 'shared'
    })

    expect(sourceA).not.toBe(sourceB)
  })

  it('is used at the real Electron notification composition seam', () => {
    const main = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'main.ts'), 'utf8')
    const handlerStart = main.indexOf("ipcMain.handle('hermes:notify'")
    const handler = main.slice(handlerStart, main.indexOf('notification.show()', handlerStart))

    expect(handler).toContain('isDuplicateNotification(nativeNotificationDedupeKey(payload ?? {}))')
  })
})

describe('sendNativeNotificationFocus', () => {
  it('returns complete source authority to the renderer that created the notification', () => {
    const send = vi.fn()

    sendNativeNotificationFocus(
      { isDestroyed: () => false, send },
      {
        approvalConnectionId: 'remote-a',
        approvalProfile: 'research',
        approvalRequestId: 'request-a',
        sessionId: 'shared'
      }
    )

    expect(send).toHaveBeenCalledWith('hermes:focus-session', {
      connectionId: 'remote-a',
      profile: 'research',
      requestId: 'request-a',
      sessionId: 'shared'
    })
  })

  it('is called at the real Electron notification click seam with the original sender', () => {
    const main = fs.readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'main.ts'), 'utf8')
    const clickStart = main.indexOf("notification.on('click'")
    const clickHandler = main.slice(clickStart, main.indexOf("notification.on('action'", clickStart))

    expect(clickHandler).toContain('BrowserWindow.fromWebContents(event.sender)')
    expect(clickHandler).toContain('sendNativeNotificationFocus(event.sender, payload ?? {})')
    expect(clickHandler).not.toContain('mainWindow.webContents.send')
  })
})

describe('approval source composition', () => {
  const desktopRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')

  it('uses the active-source selector at inline and grouped fallback seams', () => {
    const approval = fs.readFileSync(
      path.join(desktopRoot, 'src/components/assistant-ui/tool/approval.tsx'),
      'utf8'
    )

    const fallback = fs.readFileSync(
      path.join(desktopRoot, 'src/components/assistant-ui/tool/fallback.tsx'),
      'utf8'
    )

    expect(approval.match(/activeSessionApprovalRequest\(sessionId\)/g)).toHaveLength(2)
    expect(fallback).toContain('activeSessionApprovalRequest(sessionId)')
  })

  it('uses source-scoped prompt cleanup at both interrupt seams', () => {
    const tileActions = fs.readFileSync(path.join(desktopRoot, 'src/app/chat/session-tile-actions.ts'), 'utf8')

    const primaryActions = fs.readFileSync(
      path.join(desktopRoot, 'src/app/session/hooks/use-prompt-actions/index.ts'),
      'utf8'
    )

    expect(tileActions).toContain('clearAllPromptsForActiveSource(sessionId)')
    expect(primaryActions).toContain('clearAllPromptsForActiveSource(sessionId)')
  })
})
