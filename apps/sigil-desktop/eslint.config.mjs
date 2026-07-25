import shared from '../../eslint.config.shared.mjs'
import globals from 'globals'

export default [
  ...shared,
  {
    files: ['**/*.{ts,tsx,mjs}'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } }
  }
]
