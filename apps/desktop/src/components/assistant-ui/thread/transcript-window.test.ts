import { renderHook } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { resolveShowEarlierAction, useTranscriptWindow } from './transcript-window'

describe('useTranscriptWindow', () => {
  it('defaults expandingWindow to false outside a Provider', () => {
    // A "Show earlier" click never shows a loading state before the boundary
    // above sets up the real value — no Provider must never read as "loading".
    const { result } = renderHook(() => useTranscriptWindow())

    expect(result.current.olderAvailable).toBe(false)
    expect(result.current.expandingWindow).toBe(false)
  })
})

describe('resolveShowEarlierAction', () => {
  it('spends the already-materialized DOM page first', () => {
    expect(resolveShowEarlierAction(3, true)).toBe('dom')
    expect(resolveShowEarlierAction(3, false)).toBe('dom')
  })

  it('expands the store window once the DOM page is exhausted', () => {
    expect(resolveShowEarlierAction(0, true)).toBe('window')
  })

  it('is a no-op when neither DOM nor store has older content', () => {
    expect(resolveShowEarlierAction(0, false)).toBe(null)
  })
})
