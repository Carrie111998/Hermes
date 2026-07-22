import { describe, expect, it } from 'vitest'

import { firstCandidateText, ghostVisible } from '../hooks/useGhostSuggestion.js'

describe('firstCandidateText', () => {
  it('returns the first candidate from a result envelope', () => {
    const raw = {
      candidates: [
        { kind: 'path', text: '~/Downloads/report.xlsx' },
        { kind: 'confirm', text: 'Yes, go ahead' }
      ],
      history_version: 12
    }

    expect(firstCandidateText(raw)).toBe('~/Downloads/report.xlsx')
  })

  it('returns empty string for empty or malformed results', () => {
    expect(firstCandidateText({ candidates: [] })).toBe('')
    expect(firstCandidateText({})).toBe('')
    expect(firstCandidateText(null)).toBe('')
    expect(firstCandidateText({ candidates: [{ kind: 'path' }] })).toBe('')
  })

  it('trims candidate whitespace', () => {
    expect(firstCandidateText({ candidates: [{ kind: 'confirm', text: '  yes  ' }] })).toBe('yes')
  })
})

describe('ghostVisible', () => {
  const base = { blocked: false, dismissed: false, ghost: 'Yes, go ahead', input: '' }

  it('shows only in an idle, empty, undismissed composer with a ghost', () => {
    expect(ghostVisible(base)).toBe(true)
  })

  it('hides while the agent is busy', () => {
    expect(ghostVisible({ ...base, blocked: true })).toBe(false)
  })

  it('hides as soon as the user has typed anything', () => {
    expect(ghostVisible({ ...base, input: 'n' })).toBe(false)
  })

  it('stays hidden after an explicit dismiss', () => {
    expect(ghostVisible({ ...base, dismissed: true })).toBe(false)
  })

  it('never shows an empty ghost', () => {
    expect(ghostVisible({ ...base, ghost: '' })).toBe(false)
  })
})
