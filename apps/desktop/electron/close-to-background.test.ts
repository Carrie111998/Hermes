import { describe, expect, it } from 'vitest'

import { shouldHideMainWindowOnClose } from './close-to-background'

describe('shouldHideMainWindowOnClose', () => {
  it('keeps the desktop running for a normal close when the preference is enabled', () => {
    expect(
      shouldHideMainWindowOnClose({
        enabled: true,
        quitRequested: false,
        updateHandoff: false
      })
    ).toBe(true)
  })

  it('does not intercept close when the preference is disabled', () => {
    expect(
      shouldHideMainWindowOnClose({
        enabled: false,
        quitRequested: false,
        updateHandoff: false
      })
    ).toBe(false)
  })

  it('does not intercept an explicit application quit', () => {
    expect(
      shouldHideMainWindowOnClose({
        enabled: true,
        quitRequested: true,
        updateHandoff: false
      })
    ).toBe(false)
  })

  it('does not intercept an updater handoff', () => {
    expect(
      shouldHideMainWindowOnClose({
        enabled: true,
        quitRequested: false,
        updateHandoff: true
      })
    ).toBe(false)
  })
})
