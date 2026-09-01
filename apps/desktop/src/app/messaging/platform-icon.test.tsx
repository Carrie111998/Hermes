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
    ['simplex', 'SimpleX Chat'],
    ['raft', 'Raft'],
    ['teams', 'Microsoft Teams'],
    ['bluebubbles', 'BlueBubbles'],
    ['yuanbao', 'Yuanbao'],
    ['feishu', 'Feishu / Lark'],
    ['slack', 'Slack'],
    ['whatsapp_cloud', 'WhatsApp Cloud'],
    ['irc', 'IRC'],
    ['a2a', 'A2A'],
    ['buzz', 'Buzz'],
    ['relay', 'Relay'],
    ['msgraph_webhook', 'Microsoft Graph webhook']
  ])('renders a real mark for %s', (platformId, platformName) => {
    const { container } = render(<PlatformAvatar platformId={platformId} platformName={platformName} />)

    expect(container.querySelector('svg, img[data-platform-glyph="asset"]')).toBeTruthy()
  })

  it('keeps the official Raft two-tone icon-only geometry', () => {
    const { container } = render(<PlatformAvatar platformId="raft" platformName="Raft" />)
    const icon = container.querySelector('svg')

    expect(icon?.getAttribute('viewBox')).toBe('0 0 113 104')
    expect(icon?.querySelectorAll('path')).toHaveLength(3)
    expect(icon?.querySelector('[fill="#141111"]')).toBeTruthy()
    expect(icon?.querySelectorAll('[fill="#FFFAEF"]')).toHaveLength(2)
  })

  it('preserves Slack as the original four-color asset', () => {
    const { container } = render(<PlatformAvatar platformId="slack" platformName="Slack" />)
    const icon = container.querySelector('img[data-platform-glyph="asset"]')

    expect(icon).toBeTruthy()
    expect(icon?.getAttribute('src')).toMatch(/^data:image\/svg\+xml|slack-logo\.svg/)
  })

  it('keeps the initial fallback for an unknown platform', () => {
    const { container } = render(<PlatformAvatar platformId="custom_chat" platformName="Custom Chat" />)

    expect(container.querySelector('svg, img')).toBeNull()
    expect(screen.getByText('C')).toBeTruthy()
  })

  it('renders the original DingTalk asset without a mask or color override', () => {
    const { container } = render(<PlatformAvatar platformId="dingtalk" platformName="DingTalk" />)
    const icon = container.querySelector('img[data-platform-glyph="asset"]')

    expect(icon).toBeTruthy()
    expect(icon?.getAttribute('src')).toMatch(/dingtalk-icon\.png/)
    expect(container.querySelector('[data-platform-glyph="mask"]')).toBeNull()
  })

  it('keeps Matrix on the original Simple Icons SVG', () => {
    const { container } = render(<PlatformAvatar platformId="matrix" platformName="Matrix" />)
    const icon = container.querySelector('svg')

    expect(icon).toBeTruthy()
    expect(container.querySelector('img[data-platform-glyph="asset"]')).toBeNull()
  })

  it('keeps Email on the original Gmail preset', () => {
    const { container } = render(<PlatformAvatar platformId="email" platformName="Email" />)

    expect(container.querySelector('svg')).toBeTruthy()
    expect(container.querySelector('img[data-platform-glyph="asset"]')).toBeNull()
  })
})
