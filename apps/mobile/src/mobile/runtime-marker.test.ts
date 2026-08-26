import { describe, expect, it } from 'vitest'

import { markNativeMobileRenderer } from './runtime-marker'

describe('markNativeMobileRenderer', () => {
  it('sets the renderer marker before Desktop modules choose their layout mode', () => {
    const attrs = new Map<string, string>()
    const documentLike = {
      documentElement: {
        setAttribute: (name: string, value: string) => attrs.set(name, value)
      }
    } as unknown as Document

    markNativeMobileRenderer(documentLike)

    expect(attrs.get('data-hermes-mobile')).toBe('')
  })
})
