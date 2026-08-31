import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { PlatformAvatar } from './platform-icon'

afterEach(cleanup)

describe('PlatformAvatar brand glyphs', () => {
  it.each([
    ['dingtalk', 'DingTalk'],
    ['wecom', 'WeCom'],
    ['wecom_callback', 'WeCom (app)'],
    ['matrix', 'Matrix'],
    ['google_chat', 'Google Chat'],
    ['line', 'LINE'],
    ['ntfy', 'ntfy'],
    ['simplex', 'SimpleX Chat']
  ])('renders a local brand glyph for %s', (platformId, platformName) => {
    const { container } = render(<PlatformAvatar platformId={platformId} platformName={platformName} />)

    expect(container.querySelector('svg')).toBeTruthy()
    expect(screen.queryByText(platformName.charAt(0))).toBeNull()
  })

  it.each([
    ['irc', 'IRC', 'IRC'],
    ['raft', 'Raft', 'R'],
    ['teams', 'Microsoft Teams', 'T']
  ])('renders a stable brand monogram for %s', (platformId, platformName, monogram) => {
    const { container } = render(<PlatformAvatar platformId={platformId} platformName={platformName} />)

    expect(container.querySelector('svg')).toBeNull()
    expect(screen.getByText(monogram)).toBeTruthy()
  })

  it('keeps the initial fallback for an unknown platform', () => {
    const { container } = render(<PlatformAvatar platformId="custom_chat" platformName="Custom Chat" />)

    expect(container.querySelector('svg')).toBeNull()
    expect(screen.getByText('C')).toBeTruthy()
  })
})
