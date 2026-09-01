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

    expect(container.querySelector('svg, [data-platform-glyph="mask"]')).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })

  it('keeps the official Raft two-tone icon-only geometry', () => {
    const { container } = render(<PlatformAvatar platformId="raft" platformName="Raft" />)
    const icon = container.querySelector('svg')

    expect(icon?.getAttribute('viewBox')).toBe('0 0 113 104')
    expect(icon?.querySelectorAll('path')).toHaveLength(3)
    expect(icon?.querySelector('[fill="#141111"]')).toBeTruthy()
    expect(icon?.querySelectorAll('[fill="#FFFAEF"]')).toHaveLength(2)
  })

  it('preserves Slack as a muted four-color mark', () => {
    const { container } = render(<PlatformAvatar platformId="slack" platformName="Slack" />)
    const fills = Array.from(container.querySelectorAll('svg path')).map(path => path.getAttribute('fill'))

    expect(fills).toEqual(['#B56A7A', '#B56A7A', '#6E9FB5', '#6E9FB5', '#6E9A82', '#6E9A82', '#B59A5C', '#B59A5C'])
  })

  it('keeps the initial fallback for an unknown platform', () => {
    const { container } = render(<PlatformAvatar platformId="custom_chat" platformName="Custom Chat" />)

    expect(container.querySelector('svg, img')).toBeNull()
    expect(screen.getByText('C')).toBeTruthy()
  })

  it('uses the shared low-saturation field and colored DingTalk mark', () => {
    const { container } = render(<PlatformAvatar platformId="dingtalk" platformName="DingTalk" />)
    const chip = container.querySelector('span')

    expect(chip?.getAttribute('style')).not.toContain('background-color: rgb(0, 137, 255)')
    expect(chip?.querySelector('[data-platform-glyph="mask"]')).toBeTruthy()
    expect(chip?.querySelector('img')).toBeNull()
  })

  it('uses a pale field and black Matrix mark', () => {
    const { container } = render(<PlatformAvatar platformId="matrix" platformName="Matrix" />)
    const chip = container.querySelector('span')
    const icon = chip?.querySelector('svg')

    expect(chip?.getAttribute('style')).toContain('background-color: rgb(247, 247, 245)')
    expect(icon).toBeTruthy()
    expect(icon?.getAttribute('fill')).toBe('currentColor')
  })
})
