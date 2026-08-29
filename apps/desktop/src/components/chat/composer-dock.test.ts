import { describe, expect, it } from 'vitest'

import { composerDockCard } from './composer-dock'

describe('composerDockCard', () => {
  it('does not shrink inside the capped status-stack scroll container', () => {
    expect(composerDockCard('top').split(/\s+/)).toContain('shrink-0')
  })
})
