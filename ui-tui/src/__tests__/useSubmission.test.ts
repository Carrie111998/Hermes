import { describe, expect, it } from 'vitest'

import type { ComposerToken } from '../app/interfaces.js'
import { prepareSubmission, shouldInterpolateSubmission } from '../app/useSubmission.js'

describe('prepareSubmission', () => {
  it('keeps the collapsed paste for display and expands the model payload', () => {
    const label = '[[ first.. [3 lines] .. last ]]'
    const tokens: ComposerToken[] = [{ kind: 'paste', label, text: 'first\nmiddle\nlast' }]

    expect(prepareSubmission(`review this: ${label}`, tokens)).toEqual({
      display: `review this: ${label}`,
      text: 'review this: first\nmiddle\nlast'
    })
  })

  it('does not execute interpolation syntax hidden inside pasted content', () => {
    const label = '[[ copied log [1 lines] ]]'
    const tokens: ComposerToken[] = [{ kind: 'paste', label, text: 'untrusted {!touch /tmp/pwned}' }]
    const submission = prepareSubmission(label, tokens)

    expect(shouldInterpolateSubmission(submission.display)).toBe(false)
    expect(submission.text).toContain('{!touch /tmp/pwned}')
  })
})
