import { describe, expect, it } from 'vitest'

import { resolveWakeVoiceRoute } from './wake-voice-routing'

interface SessionFixture {
  archived?: boolean
  id: string
  preview?: string
  profile?: string
  title?: string
}

const sessions: SessionFixture[] = [
  { id: 'toss', profile: 'default', title: 'Toss résumé' },
  { id: 'gmail', profile: 'default', title: 'Gmail integration' },
  { id: 'gmail-tests', profile: 'default', title: 'Gmail integration tests' },
  { id: 'other-profile', profile: 'work', title: 'Payroll' },
  { archived: true, id: 'archived', profile: 'default', title: 'Archived project' }
]

describe('resolveWakeVoiceRoute', () => {
  it('passes ordinary wake transcripts through unchanged', () => {
    expect(resolveWakeVoiceRoute('Review the experience section', sessions, 'default')).toEqual({ kind: 'none' })
    expect(resolveWakeVoiceRoute('Send the report to the recruiter', sessions, 'default')).toEqual({ kind: 'none' })
  })

  it('resolves an exact title and preserves the prompt text', () => {
    expect(
      resolveWakeVoiceRoute(
        'Send to the Toss résumé session: Review the Experience section — keep all metrics.',
        sessions,
        'default'
      )
    ).toEqual({
      kind: 'match',
      prompt: 'Review the Experience section — keep all metrics.',
      sessionId: 'toss',
      title: 'Toss résumé'
    })
  })

  it('requires an explicit destination delimiter so STT whitespace cannot guess a boundary', () => {
    expect(
      resolveWakeVoiceRoute('continue in Toss resume session review the summary', sessions, 'default')
    ).toMatchObject({ kind: 'invalid', reason: 'ambiguous_syntax' })

    expect(
      resolveWakeVoiceRoute('continue in Toss resume session and review the summary', sessions, 'default')
    ).toMatchObject({ kind: 'match', prompt: 'review the summary', sessionId: 'toss' })
  })

  it('uses the final session delimiter when a title itself contains the word session', () => {
    const titledSessions = [
      { id: 'short', profile: 'default', title: 'Voice' },
      { id: 'intended', profile: 'default', title: 'Voice session routing' }
    ]

    expect(
      resolveWakeVoiceRoute(
        'send to Voice session routing session: do the safe thing',
        titledSessions,
        'default'
      )
    ).toMatchObject({ kind: 'match', prompt: 'do the safe thing', sessionId: 'intended' })
  })

  it('keeps a later session-and phrase inside the prompt when only the first boundary resolves', () => {
    expect(
      resolveWakeVoiceRoute(
        'send to Toss resume session and review the session and summarize the result',
        sessions,
        'default'
      )
    ).toMatchObject({
      kind: 'match',
      prompt: 'review the session and summarize the result',
      sessionId: 'toss'
    })
  })

  it('rejects competing valid boundaries even when one is exact and one is a unique prefix', () => {
    const titledSessions = [
      { id: 'short', profile: 'default', title: 'Voice' },
      { id: 'intended', profile: 'default', title: 'Voice session and routing tests' }
    ]

    expect(
      resolveWakeVoiceRoute(
        'send to Voice session and routing session: do the safe thing',
        titledSessions,
        'default'
      )
    ).toMatchObject({ kind: 'ambiguous' })
  })

  it('lets an ambiguous boundary veto a unique match from another boundary', () => {
    const titledSessions = [
      { id: 'short', profile: 'default', title: 'Voice' },
      { id: 'candidate-a', profile: 'default', title: 'Voice session and routing tests' },
      { id: 'candidate-b', profile: 'default', title: 'Voice session and routing checks' }
    ]

    expect(
      resolveWakeVoiceRoute(
        'send to Voice session and routing session: do the safe thing',
        titledSessions,
        'default'
      )
    ).toMatchObject({ kind: 'ambiguous' })
  })

  it('normalizes Unicode accents, case, punctuation, and whitespace', () => {
    expect(
      resolveWakeVoiceRoute('SEND TO toss   resume SESSION, verify the claims', sessions, 'default')
    ).toMatchObject({ kind: 'match', sessionId: 'toss' })
  })

  it('allows only a unique title prefix', () => {
    expect(resolveWakeVoiceRoute('send to Toss session: check formatting', sessions, 'default')).toMatchObject({
      kind: 'match',
      sessionId: 'toss'
    })

    expect(resolveWakeVoiceRoute('send to Gmail session: rerun it', sessions, 'default')).toEqual({
      candidates: ['Gmail integration', 'Gmail integration tests'],
      destination: 'Gmail',
      kind: 'ambiguous'
    })
  })

  it('treats duplicate exact titles as ambiguous instead of picking the newest', () => {
    const duplicated = [...sessions, { id: 'toss-copy', profile: 'default', title: 'Toss résumé' }]

    expect(resolveWakeVoiceRoute('send to Toss resume session: check formatting', duplicated, 'default')).toEqual({
      candidates: ['Toss résumé', 'Toss résumé'],
      destination: 'Toss resume',
      kind: 'ambiguous'
    })
  })

  it('rejects a missing destination without falling back to the active session', () => {
    expect(resolveWakeVoiceRoute('send to Calendar session: inspect tomorrow', sessions, 'default')).toEqual({
      destination: 'Calendar',
      kind: 'missing'
    })
  })

  it('excludes archived sessions and sessions owned by another profile', () => {
    expect(resolveWakeVoiceRoute('send to Archived project session: revive it', sessions, 'default')).toMatchObject({
      kind: 'missing'
    })
    expect(resolveWakeVoiceRoute('send to Payroll session: calculate totals', sessions, 'default')).toMatchObject({
      kind: 'missing'
    })
    expect(resolveWakeVoiceRoute('send to Payroll session: calculate totals', sessions, 'work')).toMatchObject({
      kind: 'match',
      sessionId: 'other-profile'
    })
  })

  it('uses the visible preview when a session has no title', () => {
    const untitled = [{ id: 'preview', profile: 'default', preview: 'Release planning' }]

    expect(resolveWakeVoiceRoute('route to Release planning session: list blockers', untitled, 'default')).toMatchObject({
      kind: 'match',
      sessionId: 'preview',
      title: 'Release planning'
    })
  })

  it('rejects an explicit routing command with no prompt', () => {
    expect(resolveWakeVoiceRoute('send to Toss résumé session', sessions, 'default')).toEqual({
      destination: 'Toss résumé',
      kind: 'invalid',
      reason: 'missing_prompt'
    })
    expect(resolveWakeVoiceRoute('send to Toss résumé session:', sessions, 'default')).toEqual({
      destination: 'Toss résumé',
      kind: 'invalid',
      reason: 'missing_prompt'
    })
  })
})
