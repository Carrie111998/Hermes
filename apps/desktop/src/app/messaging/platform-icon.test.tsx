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

    expect(container.querySelector('svg, img, [data-dingtalk-wing]')).toBeTruthy()
    expect(screen.queryByText(platformName.charAt(0))).toBeNull()
  })

  it('keeps the initial fallback for an unknown platform', () => {
    const { container } = render(<PlatformAvatar platformId="custom_chat" platformName="Custom Chat" />)

    expect(container.querySelector('svg, img')).toBeNull()
    expect(screen.getByText('C')).toBeTruthy()
  })

  it('uses a solid blue field and only the DingTalk wing', () => {
    const { container } = render(<PlatformAvatar platformId="dingtalk" platformName="DingTalk" />)
    const chip = container.querySelector('span')

    expect(chip?.getAttribute('style')).toContain('background-color: rgb(0, 137, 255)')
    expect(chip?.querySelector('span')).toBeTruthy()
    expect(chip?.querySelector('img')).toBeNull()
  })

  it('uses a pale field and black Matrix mark', () => {
    const { container } = render(<PlatformAvatar platformId="matrix" platformName="Matrix" />)
    const chip = container.querySelector('span')

    expect(chip?.getAttribute('style')).toContain('background-color: rgb(247, 247, 245)')
    expect(chip?.querySelector('img')).toBeTruthy()
  })
})
