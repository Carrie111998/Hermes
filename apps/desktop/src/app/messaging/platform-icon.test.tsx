import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { PlatformAvatar } from './platform-icon'

afterEach(cleanup)

describe('PlatformAvatar brand glyphs', () => {
  it.each([
    ['dingtalk', 'DingTalk'],
    ['wecom', 'WeCom'],
    ['wecom_callback', 'WeCom (app)']
  ])('renders a brand glyph for %s instead of a letter fallback', (platformId, platformName) => {
    const { container } = render(<PlatformAvatar platformId={platformId} platformName={platformName} />)

    expect(container.querySelector('svg')).toBeTruthy()
    expect(screen.queryByText(platformName.charAt(0))).toBeNull()
  })

  it('keeps the initial fallback for an unknown platform', () => {
    const { container } = render(<PlatformAvatar platformId="custom_chat" platformName="Custom Chat" />)

    expect(container.querySelector('svg')).toBeNull()
    expect(screen.getByText('C')).toBeTruthy()
  })
})
