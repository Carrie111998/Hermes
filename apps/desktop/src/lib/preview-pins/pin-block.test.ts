import { describe, expect, it } from 'vitest'

import { openPins, orderedShots, pinAttachmentLabel, pinCommentBlock } from './pin-block'
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

describe('pinCommentBlock across pages', () => {
  const HOME = 'http://localhost:5178/en/index.html'
  const ABOUT = 'http://localhost:5178/en/about.html'

  it('grows a heading per page once the review left the first one', () => {
    const block = pinCommentBlock(JSON.stringify([
      pin({ comment: 'hero is cramped', createdAt: 1, id: 'a', pageUrl: HOME }),
      pin({ comment: 'team photos are stretched', createdAt: 2, id: 'b', pageUrl: ABOUT })
    ]))

    expect(block).toContain(HOME)
    expect(block).toContain(ABOUT)
    expect(block).toContain('hero is cramped')
    expect(block).toContain('team photos are stretched')
  })

  it('keeps the one-page fence header, which is shorter and says the same', () => {
    const block = pinCommentBlock(JSON.stringify([pin({ pageUrl: HOME })]))
    expect(block).toContain(`\`\`\`preview-comments ${HOME}`)
  })

  it('numbers straight through the pages, because those are the image numbers', () => {
    const block = pinCommentBlock(JSON.stringify([
      pin({ comment: 'one', createdAt: 1, id: 'a', pageUrl: HOME }),
      pin({ comment: 'two', createdAt: 2, id: 'b', pageUrl: ABOUT }),
      pin({ comment: 'three', createdAt: 3, id: 'c', pageUrl: ABOUT })
    ]))

    expect(block).toContain('1.')
    expect(block).toContain('2.')
    expect(block).toContain('3.')
  })

  it('keeps each page\'s pins together even when interleaved in time', () => {
    const block = pinCommentBlock(JSON.stringify([
      pin({ comment: 'home first', createdAt: 1, id: 'a', pageUrl: HOME }),
      pin({ comment: 'about', createdAt: 2, id: 'b', pageUrl: ABOUT }),
      pin({ comment: 'home again', createdAt: 3, id: 'c', pageUrl: HOME })
    ]))

    // Going back to a page mid-review must not split it into two sections.
    expect(block!.indexOf('home first')).toBeLessThan(block!.indexOf('home again'))
    expect(block!.indexOf('home again')).toBeLessThan(block!.indexOf('about'))
  })
})

describe('pinCommentBlock with images', () => {
  const shot = (id: string) => ({ h: 40, id, thumb: 'data:image/jpeg;base64,x', w: 60 })

  it('names the image on the line that owns it', () => {
    const block = pinCommentBlock(JSON.stringify([
      pin({ comment: 'should look like this', shots: [shot('s1')] })
    ]))

    // Attachments arrive as bare pictures; without the marker the model cannot
    // tell which comment each one illustrates.
    expect(block).toContain('[image 1]')
  })

  it('numbers images across pins in the order they will be attached', () => {
    const block = pinCommentBlock(JSON.stringify([
      pin({ comment: 'first', createdAt: 1, id: 'a', shots: [shot('s1')] }),
      pin({ comment: 'second', createdAt: 2, id: 'b', shots: [shot('s2'), shot('s3')] })
    ]))

    expect(block).toContain('[image 1]')
    expect(block).toContain('[image 2] [image 3]')
    expect(block!.indexOf('[image 1]')).toBeLessThan(block!.indexOf('[image 2]'))
  })

  it('says nothing about images when there are none', () => {
    expect(pinCommentBlock(JSON.stringify([pin()]))).not.toContain('[image')
  })
})

describe('orderedShots', () => {
  const shot = (id: string) => ({ h: 40, id, thumb: 'data:image/jpeg;base64,x', w: 60 })

  it('walks pins in block order so image N is the same N in both', () => {
    const pins = openPins(JSON.stringify([
      pin({ createdAt: 2, id: 'b', shots: [shot('s2')] }),
      pin({ createdAt: 1, id: 'a', shots: [shot('s1')] })
    ]))

    expect(orderedShots(pins).map(entry => entry.shot.id)).toEqual(['s1', 's2'])
  })

  it('skips pins carrying no image', () => {
    const pins = openPins(JSON.stringify([pin(), pin({ id: 'b', shots: [shot('s1')] })]))
    expect(orderedShots(pins)).toHaveLength(1)
  })

  it('leaves out resolved pins, matching the block', () => {
    const pins = openPins(JSON.stringify([pin({ resolved: true, shots: [shot('s1')] })]))
    expect(orderedShots(pins)).toHaveLength(0)
  })
})

describe('pinAttachmentLabel', () => {
  it('counts only open pins', () => {
    expect(pinAttachmentLabel([pin(), pin({ id: 'b', resolved: true })])).toBe('1 comment')
    expect(pinAttachmentLabel([pin(), pin({ id: 'b' })])).toBe('2 comments')
    expect(pinAttachmentLabel([])).toBe('0 comments')
  })
})
