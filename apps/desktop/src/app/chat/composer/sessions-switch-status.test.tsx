import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SessionsSwitchStatus } from './sessions-switch-status'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      composer: {
        sessionsSwitchingSendBlocked: "Can't send while Sessions is switching. Your draft is saved.",
        workspaceAuthRequiredSendBlocked:
          "Can't send until this workspace is connected and authorized. Your draft is saved.",
        workspaceRouteInvalidSendBlocked:
          "Can't send because this chat's workspace route is unavailable. Your draft is saved.",
        workspaceSwitchFailedSendBlocked:
          "Can't send because the Sessions switch failed. You're still on the previous workspace, and your draft is saved.",
        workspaceUnreachableSendBlocked: "Can't send because this workspace is unreachable. Your draft is saved.",
        workspaceUnsupportedBuildSendBlocked:
          "Can't send because this workspace needs a compatible Hermes Desktop build. Your draft is saved."
      }
    }
  })
}))

afterEach(cleanup)

describe('SessionsSwitchStatus', () => {
  it('shows one concise polite status while Sessions is switching', () => {
    render(<SessionsSwitchStatus state="switching" />)

    const status = screen.getByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toBe("Can't send while Sessions is switching. Your draft is saved.")
    expect(status.getAttribute('role')).toBe('status')
  })

  it.each([
    ['route_invalid', "Can't send because this chat's workspace route is unavailable. Your draft is saved."],
    ['auth_required', "Can't send until this workspace is connected and authorized. Your draft is saved."],
    ['unreachable', "Can't send because this workspace is unreachable. Your draft is saved."],
    [
      'unsupported_build',
      "Can't send because this workspace needs a compatible Hermes Desktop build. Your draft is saved."
    ],
    [
      'switch_failed',
      "Can't send because the Sessions switch failed. You're still on the previous workspace, and your draft is saved."
    ]
  ] as const)('explains the %s send block without discarding the draft', (state, message) => {
    render(<SessionsSwitchStatus state={state} />)

    expect(screen.getByRole('status').textContent).toBe(message)
  })

  it('hides the status when sending is allowed', () => {
    render(<SessionsSwitchStatus state={null} />)

    expect(screen.queryByRole('status')).toBeNull()
  })
})
