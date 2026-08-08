import { formatDisplayTimestamp } from '@hermes/shared/display-timestamp'
import { describe, expect, it } from 'vitest'

describe('formatDisplayTimestamp', () => {
  it('preserves the configured Python strftime date-time format', () => {
    const local = new Date(2026, 7, 8, 15, 4, 5)

    expect(
      formatDisplayTimestamp(local, {
        enabled: true,
        format: '%Y-%m-%d %H:%M:%S'
      })
    ).toBe('2026-08-08 15:04:05')
  })

  it('returns no label when timestamps are disabled', () => {
    const local = new Date(2026, 7, 8, 15, 4, 5)

    expect(formatDisplayTimestamp(local, { enabled: false, format: '%Y-%m-%d %H:%M:%S' })).toBe('')
  })

  it('preserves literal percent escapes', () => {
    const local = new Date(2026, 7, 8, 15, 4, 5)

    expect(formatDisplayTimestamp(local, { enabled: true, format: '%% %H:%M' })).toBe('% 15:04')
  })

  it('treats numeric message timestamps as Unix seconds', () => {
    const local = new Date(2026, 7, 8, 15, 4, 5)

    expect(formatDisplayTimestamp(local.getTime() / 1000, { enabled: true, format: '%Y-%m-%d %H:%M:%S' })).toBe(
      '2026-08-08 15:04:05'
    )
  })
})