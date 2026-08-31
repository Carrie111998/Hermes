import { describe, expect, it } from 'vitest'

import { allPins, forgetPage, mergeReport, normalizePageUrl, otherPages, pinsForPage } from './pin-book'
import type { PreviewPin } from './types'

function pin(pageUrl: string, overrides: Partial<PreviewPin> = {}): PreviewPin {
  return {
    comment: 'c',
    createdAt: 1,
    id: `pin-${pageUrl}-${overrides.comment ?? 'c'}`,
    kind: 'element',
    pageUrl,
    resolved: false,
    target: 'Save',
    ...overrides
  }
}

const HOME = 'http://localhost:5178/en/index.html'
const ABOUT = 'http://localhost:5178/en/about.html'

describe('normalizePageUrl', () => {
  it('folds a bare trailing hash, which an anchor click leaves behind', () => {
    expect(normalizePageUrl(`${HOME}#`)).toBe(HOME)
  })

  it('keeps a real fragment — it is the route in a hash-routed app', () => {
    expect(normalizePageUrl(`${HOME}#/settings`)).toBe(`${HOME}#/settings`)
  })

  it('keeps the query — ?id=2 is a different page to comment on', () => {
    expect(normalizePageUrl(`${HOME}?id=2`)).toBe(`${HOME}?id=2`)
  })
})

describe('mergeReport', () => {
  it('files a page\'s pins under that page', () => {
    const book = mergeReport({}, HOME, [pin(HOME)])
    expect(pinsForPage(book, HOME)).toHaveLength(1)
    expect(pinsForPage(book, ABOUT)).toHaveLength(0)
  })

  it('leaves the other page alone — this is the whole point', () => {
    let book = mergeReport({}, HOME, [pin(HOME, { comment: 'home note' })])
    book = mergeReport(book, ABOUT, [pin(ABOUT, { comment: 'about note' })])

    // Walking to a second page used to replay the first page's pins into it and
    // bring them back detached. Two pages, two buckets, both intact.
    expect(pinsForPage(book, HOME)[0].comment).toBe('home note')
    expect(pinsForPage(book, ABOUT)[0].comment).toBe('about note')
  })

  it('replaces a page\'s bucket rather than appending to it', () => {
    let book = mergeReport({}, HOME, [pin(HOME), pin(HOME, { comment: 'b', id: 'b' })])
    book = mergeReport(book, HOME, [pin(HOME)])
    expect(pinsForPage(book, HOME)).toHaveLength(1)
  })

  it('drops a page that has nothing left, so nothing counts an empty page', () => {
    let book = mergeReport({}, HOME, [pin(HOME)])
    book = mergeReport(book, HOME, [])
    expect(Object.keys(book)).toHaveLength(0)
  })

  it('ignores a report with no url — about:blank between navigations', () => {
    const book = mergeReport({}, '', [pin(HOME)])
    expect(Object.keys(book)).toHaveLength(0)
  })

  it('does not mutate the book it was handed', () => {
    const before = mergeReport({}, HOME, [pin(HOME)])
    mergeReport(before, ABOUT, [pin(ABOUT)])
    expect(Object.keys(before)).toEqual([HOME])
  })
})

describe('otherPages', () => {
  it('counts open comments waiting elsewhere', () => {
    let book = mergeReport({}, HOME, [pin(HOME)])
    book = mergeReport(book, ABOUT, [pin(ABOUT), pin(ABOUT, { comment: 'b', id: 'b2' })])

    expect(otherPages(book, HOME)).toEqual({ count: 2, pages: 1 })
    expect(otherPages(book, ABOUT)).toEqual({ count: 1, pages: 1 })
  })

  it('ignores resolved ones — they are not waiting on anybody', () => {
    const book = mergeReport({}, ABOUT, [pin(ABOUT, { resolved: true })])
    expect(otherPages(book, HOME)).toEqual({ count: 0, pages: 0 })
  })
})

describe('allPins', () => {
  it('returns every page\'s pins, oldest first within a page', () => {
    let book = mergeReport({}, ABOUT, [
      pin(ABOUT, { comment: 'second', createdAt: 20, id: 'x' }),
      pin(ABOUT, { comment: 'first', createdAt: 10, id: 'y' })
    ])

    book = mergeReport(book, HOME, [pin(HOME, { comment: 'home', createdAt: 5 })])

    expect(allPins(book).map(entry => entry.comment)).toEqual(['home', 'first', 'second'])
  })
})

describe('forgetPage', () => {
  it('clears one page without touching the rest of the review', () => {
    let book = mergeReport({}, HOME, [pin(HOME)])
    book = mergeReport(book, ABOUT, [pin(ABOUT)])
    expect(Object.keys(forgetPage(book, HOME))).toEqual([ABOUT])
  })
})
