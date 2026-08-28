import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { SessionsSwitchStatus } from './sessions-switch-status'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      composer: {
        sessionsSwitchingSendBlocked: "Can't send while Sessions is switching."
      }
    }
  })
}))

afterEach(cleanup)

describe('SessionsSwitchStatus', () => {
  it('shows one concise polite status while Sessions is switching', () => {
    render(<SessionsSwitchStatus blocked />)

    const status = screen.getByRole('status')
    expect(status.getAttribute('aria-live')).toBe('polite')
    expect(status.textContent).toBe("Can't send while Sessions is switching.")
    expect(status.getAttribute('role')).toBe('status')
  })

  it('hides the status when sending is allowed', () => {
    render(<SessionsSwitchStatus blocked={false} />)

    expect(screen.queryByRole('status')).toBeNull()
  })
})
