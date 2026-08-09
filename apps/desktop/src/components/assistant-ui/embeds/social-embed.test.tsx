import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import type { TweetEmbed } from './providers/types'
import SocialEmbedRenderer from './social-embed'

const tweet: TweetEmbed = {
  id: 'twitter:20',
  label: 'X',
  maxWidth: 480,
  provider: 'twitter',
  renderer: 'tweet',
  sourceUrl: 'https://x.com/jack/status/20',
  tweetId: '20'
}

describe('social provider renderer', () => {
  it('uses an explicit link card without injecting provider scripts', () => {
    const { container } = render(<SocialEmbedRenderer descriptor={tweet} />)

    expect(container.querySelectorAll('script')).toHaveLength(0)
    expect(container.querySelector('iframe')).toBeNull()
    expect(container.querySelector('a')?.getAttribute('href')).toBe(tweet.sourceUrl)
    expect(container.textContent).toContain('Open X post')
  })
})
