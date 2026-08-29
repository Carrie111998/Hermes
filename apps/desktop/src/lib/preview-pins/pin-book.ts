/**
 * PIN BOOK — one review spanning several pages.
 *
 * A pin lives in the page it was placed on and dies with it, so the app has
 * always been the durable side. The book is that durable side made explicit:
 * a bucket of pins per page, so walking from the home page to a product page
 * and back is an ordinary thing to do in the middle of a review rather than a
 * way to lose the comments you already wrote.
 *
 * Before this, everything the panel held was replayed into whatever page
 * loaded next — so page A's pins landed on page B, resolved against nothing,
 * and came back marked detached. Bucketing fixes the write side; the seed
 * filter in `preview-pins.ts` fixes the read side, using the guest page's own
 * `location` rather than what the pane believes it is showing.
 *
 * Pure and dependency-free, like pin-block.ts, so it can be tested without a
 * page.
 */

import type { PreviewPin } from './types'

export type PinBook = Record<string, PreviewPin[]>

/**
 * The key a page's pins are filed under.
 *
 * A bare trailing `#` is what a page picks up from an anchor click that went
 * nowhere; treating `index.html` and `index.html#` as two pages would split one
 * review into two buckets for no reason the user could ever see. Anything more
 * clever — dropping the query, folding `/` and `/index.html` — would merge
 * pages that really are different, which is the worse mistake.
 */
export function normalizePageUrl(url: string): string {
  return String(url || '').replace(/#$/, '')
}

/** File a fresh report from the page under the page it actually came from. */
export function mergeReport(book: PinBook, url: string, pins: PreviewPin[]): PinBook {
  const key = normalizePageUrl(url)

  if (!key) {return book}

  const next = { ...book }

  if (pins.length) {next[key] = pins}
  // An empty page is dropped rather than kept as an empty bucket, so "pins on
  // 2 other pages" never counts a page with nothing on it.
  else {delete next[key]}

  return next
}

/** What belongs to one page — what the engine should be holding right now. */
export function pinsForPage(book: PinBook, url: string): PreviewPin[] {
  return book[normalizePageUrl(url)] ?? []
}

/**
 * Every pin in the book, page by page, oldest first inside each page.
 *
 * Pages come in the order the user started commenting on them, not
 * alphabetically: the review is a walk through the site, and reading it back in
 * the order it was made is the only ordering that carries any meaning.
 */
export function allPins(book: PinBook): PreviewPin[] {
  const earliest = (pins: PreviewPin[]) => Math.min(...pins.map(pin => pin.createdAt))

  return Object.keys(book)
    .sort((a, b) => earliest(book[a]) - earliest(book[b]))
    .flatMap(key => book[key].slice().sort((a, b) => a.createdAt - b.createdAt))
}

/** Open comments waiting on pages other than this one. */
export function otherPages(book: PinBook, url: string): { count: number; pages: number } {
  const here = normalizePageUrl(url)
  let count = 0
  let pages = 0

  for (const key of Object.keys(book)) {
    if (key === here) {continue}
    const open = book[key].filter(pin => !pin.resolved).length

    if (!open) {continue}
    count += open
    pages += 1
  }

  return { count, pages }
}

/** Drop one page's pins — "clear" while standing on it. */
export function forgetPage(book: PinBook, url: string): PinBook {
  const next = { ...book }
  delete next[normalizePageUrl(url)]

  return next
}
