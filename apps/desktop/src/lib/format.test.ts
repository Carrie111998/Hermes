import { describe, expect, it } from 'vitest'
import { compactNumber } from '@/lib/format'

describe('compactNumber', () => {
  it('returns "0" for null/undefined/negative', () => {
    expect(compactNumber(null)).toBe('0')
    expect(compactNumber(undefined)).toBe('0')
    expect(compactNumber(-5)).toBe('0')
  })

  it('returns "0" for non-finite values', () => {
    expect(compactNumber(NaN)).toBe('0')
    expect(compactNumber(Infinity)).toBe('0')
  })

  it('returns raw number below 1000', () => {
    expect(compactNumber(0)).toBe('0')
    expect(compactNumber(1)).toBe('1')
    expect(compactNumber(999)).toBe('999')
  })

  it('formats thousands with k suffix', () => {
    expect(compactNumber(1000)).toBe('1k')
    expect(compactNumber(1230)).toBe('1.2k')
    expect(compactNumber(10000)).toBe('10k')
  })

  it('formats millions with M suffix', () => {
    expect(compactNumber(1_500_000)).toBe('1.5M')
    expect(compactNumber(2_000_000)).toBe('2M')
  })

  it('does not produce 1000k or 1000k boundary', () => {
    expect(compactNumber(999_950)).toBe('1M')
    expect(compactNumber(999_400)).toBe('999.4k')
  })
})
