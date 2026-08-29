/**
 * CONSOLE DIGEST — what the agent is told about a page's console.
 *
 * The console panel has always captured every message; the agent has never
 * been able to see any of it. So a page that was throwing on every render, or
 * warning about a missing translation key on every string, looked perfectly
 * fine to the model — it read the rendered text and reported success.
 *
 * Two shapes, because the agent needs two different things:
 *
 *   - THE DIGEST (`consoleDigest`) — the full picture, for `read_preview`.
 *     Counts by level plus the messages themselves, newest last.
 *   - THE BREADCRUMB (`consoleSince`) — counts ONLY, of what has arrived since
 *     the last time this tab was asked. It rides along on results the agent is
 *     already receiving, so noticing an error costs no extra call and no
 *     per-request tokens. Seeing a non-zero count is what tells it to go and
 *     read the details.
 *
 * WARNINGS COUNT. The motivating case was i18n — "missing translation for
 * key X" is a `console.warn`, and a breadcrumb that only counted errors would
 * have stayed silent through exactly the bug it was built to catch.
 *
 * The watermark lives in the console store itself (`drainUnreported`), because
 * only that store knows when the log restarts: a navigation resets it and the
 * user can clear it, and after either the message count returns to where it
 * already was — so a counter kept out here could not tell a cleared console
 * from a quiet one, and would go silent over the next real error.
 */

import type { ConsoleEntry } from './preview-console-state'
import { existingPreviewConsole } from './preview-console-store'

/** Chromium's console levels, as the panel already names them. */
const LEVEL_NAME: Record<number, string> = { 0: 'log', 1: 'info', 2: 'warn', 3: 'error' }

/** Cap on messages returned in one digest. A page in a render loop can emit
 *  thousands of identical errors, and this crosses into model context. The
 *  NEWEST are kept: the last error is the one that describes where the page
 *  ended up. */
const MAX_ENTRIES = 60

/** Cap on a single message. Stack traces and serialized objects run long. */
const MAX_MESSAGE_CHARS = 600

export interface ConsoleCounts {
  errors: number
  warnings: number
}

export interface ConsoleDigestEntry {
  level: string
  line?: number
  message: string
  source?: string
}

export interface ConsoleDigest extends ConsoleCounts {
  /** True when the console has overflowed its buffer, so `errors`, `warnings`
   *  and `total` are floors rather than totals — the page logged AT LEAST this
   *  many. Distinct from `truncated`, which is only about `entries`. */
  buffer_overflowed: boolean
  entries: ConsoleDigestEntry[]
  /** True when `entries` is a tail of what the buffer holds. */
  truncated: boolean
  /** Messages the buffer holds, of every level. NOT the number the page has
   *  logged: the store keeps a bounded tail, so a page in a render loop reports
   *  the cap. See `buffer_overflowed`. */
  total: number
}

function clamp(text: string): string {
  return text.length > MAX_MESSAGE_CHARS ? `${text.slice(0, MAX_MESSAGE_CHARS)}…` : text
}

function count(logs: readonly ConsoleEntry[]): ConsoleCounts {
  return {
    errors: logs.filter(log => log.level === 3).length,
    warnings: logs.filter(log => log.level === 2).length
  }
}

/** Everything the console holds for a tab, shaped for a tool result. Reading
 *  the details also settles the breadcrumb — the agent has now seen them. */
export function consoleDigest(tabId: string): ConsoleDigest {
  const state = existingPreviewConsole(tabId)

  if (!state) {
    return { buffer_overflowed: false, entries: [], errors: 0, total: 0, truncated: false, warnings: 0 }
  }

  const logs = state.$logs.get()

  state.drainUnreported()

  return {
    ...count(logs),
    // A page that has overflowed the buffer has logged more than these counts
    // say, and by an unbounded amount. Reporting `errors: 200` for a page
    // throwing five thousand times reads as "a few problems" rather than
    // "this page is on fire", so the overflow is stated rather than implied.
    buffer_overflowed: state.overflowed(),
    entries: logs.slice(-MAX_ENTRIES).map(log => ({
      level: LEVEL_NAME[log.level] ?? 'log',
      line: log.line,
      message: clamp(log.message),
      source: log.source
    })),
    total: logs.length,
    truncated: logs.length > MAX_ENTRIES
  }
}

/** Errors and warnings logged since this tab was last asked, and null when
 *  there were none — callers omit the field entirely rather than decorating
 *  every result with a pair of zeroes. */
export function consoleSince(tabId: string): ConsoleCounts | null {
  const state = existingPreviewConsole(tabId)

  if (!state) {
    return null
  }

  const counts = count(state.drainUnreported())

  return counts.errors === 0 && counts.warnings === 0 ? null : counts
}
