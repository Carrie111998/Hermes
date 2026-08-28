import { describe, expect, it } from 'vitest'

import { pinAttachmentLabel, pinCommentBlock } from './pin-block'
import type { PreviewPin } from './types'

function pin(overrides: Partial<PreviewPin> = {}): PreviewPin {
  return {
    anchor: {
      label: 'Save',
      ordinal: 0,
      path: '#panel>button',
      rect: { h: 0.05, w: 0.1, x: 0.2, y: 0.3 },
      role: 'button',
      selector: '#save',
      text: 'Save'
    },
    comment: 'too much space under this',
    createdAt: 1,
    id: 'pin-1',
    kind: 'element',
    pageUrl: 'http://localhost:8080/',
    resolved: false,
    target: 'Save',
    ...overrides
  }
}

describe('pinCommentBlock', () => {
  it('renders open pins as a fenced block carrying the page url', () => {
    const block = pinCommentBlock(JSON.stringify([pin()]))
    expect(block).toContain('```preview-comments http://localhost:8080/')
    expect(block).toContain('1. button "Save" — #save')
    expect(block).toContain('too much space under this')
    expect(block?.endsWith('```')).toBe(true)
  })

  it('prefers the selector and falls back to the path', () => {
    const withoutSelector = pin({
      anchor: { ...pin().anchor!, selector: '' }
    })
    expect(pinCommentBlock(JSON.stringify([withoutSelector]))).toContain('#panel>button')
  })

  it('drops resolved pins — "address my comments" means the open ones', () => {
    const block = pinCommentBlock(JSON.stringify([
      pin({ comment: 'still open' }),
      pin({ comment: 'already done', id: 'pin-2', resolved: true })
    ]))
    expect(block).toContain('still open')
    expect(block).not.toContain('already done')
  })

  it('returns null when every pin is resolved, so the caller can fall through', () => {
    expect(pinCommentBlock(JSON.stringify([pin({ resolved: true })]))).toBeNull()
  })

  it('numbers pins in the order they were placed, not array order', () => {
    const block = pinCommentBlock(JSON.stringify([
      pin({ comment: 'second', createdAt: 20, id: 'b' }),
      pin({ comment: 'first', createdAt: 10, id: 'a' })
    ]))
    expect(block!.indexOf('first')).toBeLessThan(block!.indexOf('second'))
  })

  it('warns the agent when a pin no longer resolves', () => {
    const block = pinCommentBlock(JSON.stringify([pin({ orphaned: true })]))
    // Without this the agent trusts a selector the ladder already gave up on.
    expect(block).toContain('no longer on the page')
  })

  it('describes a region pin by where it is, since it names no element', () => {
    const block = pinCommentBlock(JSON.stringify([
      pin({
        anchor: undefined,
        comment: 'this chart axis is unreadable',
        kind: 'region',
        region: { h: 0.2, w: 0.4, x: 0.1, y: 0.5 }
      })
    ]))
    expect(block).toContain('region at 10%,50% sized 40%×20%')
    expect(block).toContain('this chart axis is unreadable')
  })

  it('keeps a pin the user left empty rather than dropping it silently', () => {
    expect(pinCommentBlock(JSON.stringify([pin({ comment: '   ' })]))).toContain('(no comment)')
  })

  it('returns null on a malformed payload instead of throwing', () => {
    // Matches reviewCommentBlock: a bad detail must never break the send.
    expect(pinCommentBlock('not json')).toBeNull()
    expect(pinCommentBlock('{}')).toBeNull()
    expect(pinCommentBlock('[]')).toBeNull()
    expect(pinCommentBlock(JSON.stringify({ pins: 'nope' }))).toBeNull()
  })

  it('accepts both a bare array and a {pins} envelope', () => {
    expect(pinCommentBlock(JSON.stringify({ pins: [pin()] }))).toContain('button "Save"')
  })

  it('tolerates a hole in the array', () => {
    // Session switches can leave undefined holes in composer attachments
    // (#49624); the same defensiveness applies to what they carry.
    expect(() => pinCommentBlock(JSON.stringify([null, pin()]))).not.toThrow()
    expect(pinCommentBlock(JSON.stringify([null, pin()]))).toContain('button "Save"')
  })
})

describe('pinAttachmentLabel', () => {
  it('counts only open pins', () => {
    expect(pinAttachmentLabel([pin(), pin({ id: 'b', resolved: true })])).toBe('1 comment')
    expect(pinAttachmentLabel([pin(), pin({ id: 'b' })])).toBe('2 comments')
    expect(pinAttachmentLabel([])).toBe('0 comments')
  })
})
