import { createRequire } from 'node:module'

import { expect, test } from '@playwright/test'

import { findElectron } from './fixtures'

test('findElectron resolves the workspace Electron package binary', () => {
  const require_ = createRequire(import.meta.url)
  const expected = require_('electron') as string

  expect(findElectron()).toBe(expected)
})
