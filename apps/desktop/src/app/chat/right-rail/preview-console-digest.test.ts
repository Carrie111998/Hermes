/**
 * The agent could never see a page's console. These cover the two shapes that
 * change that, and in particular the case the whole thing exists for: a
 * console.warn (a missing translation key) has to register, because an
 * errors-only counter would stay silent through exactly that bug.
 */

import { beforeEach, describe, expect, it } from 'vitest'

import { consoleDigest, consoleSince } from './preview-console-digest'
import { consoleLevel } from './preview-console-state'
import { previewConsoleState } from './preview-console-store'

const LOG = 0
const WARN = 2
const ERROR = 3

let tab = 0
let id = ''

function log(level: number, message: string) {
  previewConsoleState(id).append({ level, message })
}

beforeEach(() => {
  tab += 1
  id = `url:browser-digest-${tab}`
})

describe('consoleSince', () => {
  it('says nothing when the page has been quiet', () => {
    log(LOG, 'ready')

    expect(consoleSince(id)).toBeNull()
  })

  // The motivating case. i18n reports missing keys with console.warn.
  it('counts warnings, not just errors', () => {
    log(WARN, 'missing translation for key "checkout.title"')

    expect(consoleSince(id)).toEqual({ errors: 0, warnings: 1 })
  })

  it('reports only what arrived since the last look', () => {
    log(ERROR, 'first')
    expect(consoleSince(id)).toEqual({ errors: 1, warnings: 0 })

    // Nothing new — the agent already knows about `first`.
    expect(consoleSince(id)).toBeNull()

    log(ERROR, 'second')
    log(WARN, 'third')
    expect(consoleSince(id)).toEqual({ errors: 1, warnings: 1 })
  })

  // A navigation clears the console. Counting from the old high-water mark
  // would skip the new page's first messages entirely.
  it('starts over when the console is cleared', () => {
    log(ERROR, 'old page')
    consoleSince(id)

    previewConsoleState(id).clear()
    log(ERROR, 'new page')

    expect(consoleSince(id)).toEqual({ errors: 1, warnings: 0 })
  })

  it('keeps tabs apart', () => {
    const other = `${id}-other`

    log(ERROR, 'mine')
    previewConsoleState(other).append({ level: ERROR, message: 'theirs' })

    expect(consoleSince(id)).toEqual({ errors: 1, warnings: 0 })
    expect(consoleSince(other)).toEqual({ errors: 1, warnings: 0 })
  })
})

describe('consoleDigest', () => {
  it('carries the messages, with levels named', () => {
    log(ERROR, 'TypeError: undefined is not a function')
    log(WARN, 'slow resource')

    const digest = consoleDigest(id)

    expect(digest.errors).toBe(1)
    expect(digest.warnings).toBe(1)
    expect(digest.total).toBe(2)
    expect(digest.truncated).toBe(false)
    expect(digest.entries.map(e => e.level)).toEqual(['error', 'warn'])
    expect(digest.entries[0]?.message).toContain('TypeError')
  })

  // A render loop emits thousands of identical errors; the newest describe
  // where the page actually ended up.
  it('keeps the newest when a page floods, and says it truncated', () => {
    for (let i = 0; i < 200; i += 1) {
      log(ERROR, `boom ${i}`)
    }

    const digest = consoleDigest(id)

    expect(digest.truncated).toBe(true)
    expect(digest.entries).toHaveLength(60)
    expect(digest.entries.at(-1)?.message).toBe('boom 199')
    // The buffer holds 200, so that is what `total` and the counts report.
    expect(digest.total).toBe(200)
    expect(digest.errors).toBe(200)
  })

  // The counts are computed over a bounded buffer. Reporting 200 for a page
  // that threw five thousand times reads as "a few problems" rather than
  // "this page is on fire", so overflow has to be stated, not implied.
  it('admits when the counts are floors, not totals', () => {
    for (let i = 0; i < 199; i += 1) {
      log(ERROR, `boom ${i}`)
    }

    expect(consoleDigest(id).buffer_overflowed).toBe(false)

    for (let i = 0; i < 5_000; i += 1) {
      log(ERROR, `flood ${i}`)
    }

    const digest = consoleDigest(id)

    expect(digest.buffer_overflowed).toBe(true)
    // Still 200 — the point is that the flag says so rather than the number.
    expect(digest.errors).toBe(200)
    expect(digest.entries.at(-1)?.message).toBe('flood 4999')
  })

  // Clearing empties the log without resetting the id counter, so an overflow
  // flag derived from ids alone would stick forever.
  it('stops claiming overflow once the console is cleared', () => {
    for (let i = 0; i < 5_000; i += 1) {
      log(ERROR, `boom ${i}`)
    }

    expect(consoleDigest(id).buffer_overflowed).toBe(true)

    previewConsoleState(id).clear()
    log(ERROR, 'fresh')

    expect(consoleDigest(id).buffer_overflowed).toBe(false)
  })

  // The digest is asked about whatever tab the agent resolved to, which can be
  // a file tab or an artifact. Answering must not leave a store behind.
  it('answers empty for a tab with no console, without creating one', () => {
    const digest = consoleDigest('file:/nowhere.ts')

    expect(digest).toEqual({
      buffer_overflowed: false,
      entries: [],
      errors: 0,
      total: 0,
      truncated: false,
      warnings: 0
    })
    expect(consoleSince('file:/nowhere.ts')).toBeNull()
  })

  it('clamps a single runaway message', () => {
    log(ERROR, 'x'.repeat(5_000))

    const message = consoleDigest(id).entries[0]?.message ?? ''

    expect(message.length).toBeLessThan(700)
    expect(message.endsWith('…')).toBe(true)
  })

  // Reading the detail settles the breadcrumb: the agent has now seen them.
  it('marks the console seen', () => {
    log(ERROR, 'seen')
    consoleDigest(id)

    expect(consoleSince(id)).toBeNull()
  })
})

/**
 * `<webview>` still emits an Integer level; `webContents` moved to a string in
 * Electron 35. If that divergence is ever settled, a string level would stop
 * equalling 3 and every error would silently count as a `log` — the agent would
 * go back to being told a broken page is fine, with nothing throwing.
 */
describe('consoleLevel', () => {
  it('passes the <webview> integers through', () => {
    expect([0, 1, 2, 3].map(consoleLevel)).toEqual([0, 1, 2, 3])
  })

  it('understands the string names too', () => {
    expect(consoleLevel('error')).toBe(3)
    // Chromium says 'warning'; console.warn is what i18n reports keys with.
    expect(consoleLevel('warning')).toBe(2)
    expect(consoleLevel('warn')).toBe(2)
    expect(consoleLevel('info')).toBe(1)
    expect(consoleLevel('log')).toBe(0)
  })

  it('is not case-sensitive', () => {
    expect(consoleLevel('ERROR')).toBe(3)
  })

  // Unknown or absent reads as chatter, never as an error — a breadcrumb that
  // cried wolf on every log line would be ignored within a turn.
  it('falls back to log rather than inventing an error', () => {
    expect(consoleLevel(undefined)).toBe(0)
    expect(consoleLevel('something-new')).toBe(0)
    expect(consoleLevel(Number.NaN)).toBe(0)
  })

  it('counts a string-level error the same as a numeric one', () => {
    const id = 'url:browser-digest-levels'

    previewConsoleState(id).append({ level: consoleLevel('error'), message: 'boom' })
    previewConsoleState(id).append({ level: consoleLevel('warning'), message: 'missing key' })

    expect(consoleSince(id)).toEqual({ errors: 1, warnings: 1 })
  })
})
