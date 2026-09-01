import { describe, expect, it } from 'vitest'

import type { ComposerToken } from '../app/interfaces.js'
import { droppedTokens, expandTokens, imageToken, looksLikeOrphanedPasteToken, nextImageIndex } from '../domain/attachments.js'

const paste = (label: string, text: string): ComposerToken => ({ kind: 'paste', label, text })

const image = (index: number, path = `/tmp/img${index}.png`): ComposerToken => ({
  index,
  kind: 'image',
  label: imageToken(index),
  path
})

describe('expandTokens (what the agent actually receives)', () => {
  it('replaces a collapsed paste label with its full content', () => {
    const label = '[[ hello.. [3 lines] .. world ]]'
    const result = expandTokens([paste(label, 'hello\nfoo\nworld')])(` here: ${label} done`)

    expect(result.expanded).toBe('here: hello\nfoo\nworld done')
    expect(result.unresolved).toEqual([])
  })

  it('is a no-op for already-expanded / token-free text (recall round-trip)', () => {
    const expanded = 'hello\nfoo\nworld'
    const result = expandTokens([])(expanded)

    expect(result.expanded).toBe(expanded)
    expect(result.unresolved).toEqual([])
  })

  it('expands repeated identical labels in submission order', () => {
    const label = '[[ x [1 lines] ]]'
    const result = expandTokens([paste(label, 'first'), paste(label, 'second')])(`${label} then ${label}`)

    expect(result.expanded).toBe('first then second')
    expect(result.unresolved).toEqual([])
  })

  it('leaves an unmatched label intact when it does not look like a paste token', () => {
    const label = '[[ orphan ]]'
    const result = expandTokens([])(label)

    expect(result.expanded).toBe(label)
    expect(result.unresolved).toEqual([])
  })

  it('reports an unmatched paste token with [N lines] signature as unresolved', () => {
    const label = '[[ lost.. [5k lines] .. data ]]'
    const result = expandTokens([])(`submit: ${label} now`)

    expect(result.expanded).toBe(`submit: ${label} now`)
    expect(result.unresolved).toEqual([label])
  })

  it('reports multiple orphaned paste tokens', () => {
    const a = '[[ chunk1 [135 lines] ]]'
    const b = '[[ chunk2.. [9.2k lines] .. tail ]]'
    const result = expandTokens([])(`${a} and ${b}`)

    expect(result.expanded).toBe(`${a} and ${b}`)
    expect(result.unresolved).toEqual([a, b])
  })

  it('drops an image token from the text — the gateway already holds the file', () => {
    const result = expandTokens([image(1)])(`what is in ${imageToken(1)}`)

    expect(result.expanded).toBe('what is in')
    expect(result.unresolved).toEqual([])
  })

  it('leaves no double space where an image token sat mid-sentence', () => {
    const result = expandTokens([image(1)])(`before ${imageToken(1)} after`)

    expect(result.expanded).toBe('before after')
    expect(result.unresolved).toEqual([])
  })

  it('resolves an image-only message to empty text', () => {
    const result = expandTokens([image(1)])(imageToken(1))

    expect(result.expanded).toBe('')
    expect(result.unresolved).toEqual([])
  })

  it('resolves pastes and images in one pass', () => {
    const label = '[[ log.. [9 lines] ]]'
    const result = expandTokens([paste(label, 'stack\ntrace'), image(2)])(`${label} and ${imageToken(2)}`)

    expect(result.expanded).toBe('stack\ntrace and')
    expect(result.unresolved).toEqual([])
  })
})

describe('looksLikeOrphanedPasteToken (detection of real paste tokens)', () => {
  it('recognizes a paste token with [N lines] signature', () => {
    expect(looksLikeOrphanedPasteToken('[[ spec text [135 lines] ]]')).toBe(true)
  })

  it('recognizes a paste token with [N.Nk lines] suffix (fmtK output)', () => {
    expect(looksLikeOrphanedPasteToken('[[ heading.. [5.3k lines] .. tail ]]')).toBe(true)
  })

  it('recognizes lowercase k/m/g suffix from fmtK', () => {
    expect(looksLikeOrphanedPasteToken('[[ [9.9m lines] ]]')).toBe(true)
  })

  it('rejects user-typed [[ ... ]] without a [N lines] signature', () => {
    expect(looksLikeOrphanedPasteToken('[[ orphan ]]')).toBe(false)
  })

  it('rejects [[ some text ]] that lacks the line-count marker', () => {
    expect(looksLikeOrphanedPasteToken('[[ I typed this ]]')).toBe(false)
  })
})

describe('nextImageIndex (user-facing numbering)', () => {
  it('starts at 1', () => {
    expect(nextImageIndex([])).toBe(1)
  })

  it('counts past existing images', () => {
    expect(nextImageIndex([image(1), image(2)])).toBe(3)
  })

  it('ignores paste tokens', () => {
    expect(nextImageIndex([paste('[[ x ]]', 'y')])).toBe(1)
  })

  it('does not reuse an index after an earlier image is deleted', () => {
    // [[ Image 1 ]] was erased; the next attach must not become Image 1 again
    // or expandTokens would resolve two different files to one label.
    expect(nextImageIndex([image(2)])).toBe(3)
  })
})

describe('droppedTokens (deleting the token unattaches the thing)', () => {
  it('reports an image whose token was erased from the text', () => {
    expect(droppedTokens([image(1)], 'just text now')).toEqual([image(1)])
  })

  it('reports nothing while the token is still present', () => {
    expect(droppedTokens([image(1)], `look at ${imageToken(1)}`)).toEqual([])
  })

  it('keeps a surviving token when a sibling is erased', () => {
    expect(droppedTokens([image(1), image(2)], imageToken(2))).toEqual([image(1)])
  })
})

