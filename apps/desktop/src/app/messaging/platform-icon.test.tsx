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
    ['whatsapp_cloud', 'WhatsApp Cloud']
  ])('renders a local brand glyph for %s', (platformId, platformName) => {
    const { container } = render(<PlatformAvatar platformId={platformId} platformName={platformName} />)

    expect(container.querySelector('svg')).toBeTruthy()
    expect(screen.queryByText(platformName.charAt(0))).toBeNull()
  })

  it.each([
    ['irc', 'IRC', 'IRC'],
    ['raft', 'Raft', 'R'],
    ['teams', 'Microsoft Teams', 'T'],
    ['bluebubbles', 'BlueBubbles', 'BB'],
    ['yuanbao', 'Yuanbao', 'YB'],
    ['a2a', 'A2A', 'A2A'],
    ['buzz', 'Buzz', 'B'],
    ['feishu', 'Feishu / Lark', 'F'],
    ['relay', 'Relay', 'R'],
    ['msgraph_webhook', 'Microsoft Graph webhook', 'M']
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

  it('uses the compact visual treatments for DingTalk and Matrix', () => {
    const { container: dingtalk } = render(<PlatformAvatar platformId="dingtalk" platformName="DingTalk" />)
    expect(dingtalk.querySelector('span')?.getAttribute('style')).toContain('background-color: rgb(0, 137, 255)')

    cleanup()
    const { container: matrix } = render(<PlatformAvatar platformId="matrix" platformName="Matrix" />)
    expect(matrix.querySelector('span')?.getAttribute('style')).toContain('background-color: rgb(247, 247, 245)')
    expect(matrix.querySelector('span')?.getAttribute('style')).toContain('color: rgb(0, 0, 0)')
  })

  it('keeps the real brand glyphs as SVGs instead of replacing them with text', () => {
    for (const [platformId, platformName] of [
      ['telegram', 'Telegram'],
      ['discord', 'Discord'],
      ['matrix', 'Matrix'],
      ['dingtalk', 'DingTalk']
    ]) {
      const { container } = render(<PlatformAvatar platformId={platformId} platformName={platformName} />)
      expect(container.querySelector('svg')).toBeTruthy()
      cleanup()
    }
  })
})
