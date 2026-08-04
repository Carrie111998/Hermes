import { describe, expect, it } from 'vitest'

import { TRANSLATIONS } from './catalog'

describe('boot failure locale coverage', () => {
  it('ships localized managed assignment and retry copy for every supported locale', () => {
    const english = TRANSLATIONS.en.boot.failure

    for (const [locale, translations] of Object.entries(TRANSLATIONS)) {
      const failure = translations.boot.failure

      expect(failure.managedAssignmentHint.trim()).not.toBe('')
      expect(failure.managedSignInFailed.trim()).not.toBe('')

      if (locale !== 'en') {
        expect(failure.managedAssignmentHint).not.toBe(english.managedAssignmentHint)
        expect(failure.managedSignInFailed).not.toBe(english.managedSignInFailed)
      }
    }
  })
})
