import { describe, expect, it } from 'vitest'

import { formatDesktopLogLine } from './desktop-log-line'

describe('formatDesktopLogLine', () => {
  it('prefixes each line with an ISO-8601 timestamp and the orion tag', () => {
    const line = formatDesktopLogLine('[boot] Resolving Orion backend')

    // Shape contract (not a snapshot): every desktop log line starts with
    // an ISO timestamp so multi-surface logs are chronologically readable.
    // See #84405.
    expect(line).toMatch(
      /^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] \[orion\] \[boot\] Resolving Orion backend$/
    )
  })

  it('keeps the message verbatim after the prefix', () => {
    const line = formatDesktopLogLine('Orion backend exited (0)')

    expect(line).toMatch(/^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z\] \[orion\] Orion backend exited \(0\)$/)
  })
})
