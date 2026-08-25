// @vitest-environment jsdom

import { describe, expect, it } from 'vitest'

import { isPortalHost } from './portal-chat-pane'

describe('Foundrly portal host allowlist', () => {
  it('keeps admin and brand hosts in the guest, not the system browser', () => {
    expect(isPortalHost('https://admin.intelli-verse-x.ai/admin/portal/chat')).toBe(true)
    expect(isPortalHost('https://foundrly.intelli-verse-x.ai/app')).toBe(true)
    expect(isPortalHost('https://stripe.com/pay')).toBe(false)
    expect(isPortalHost('not-a-url')).toBe(false)
  })
})
