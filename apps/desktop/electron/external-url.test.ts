import assert from 'node:assert/strict'

import { test } from 'vitest'

import { isAllowedShellOpenExternalUrl, SHELL_OPEN_EXTERNAL_PROTOCOLS } from './external-url'

test('allowlist keeps the original web + mail schemes', () => {
  assert.equal(SHELL_OPEN_EXTERNAL_PROTOCOLS.has('http:'), true)
  assert.equal(SHELL_OPEN_EXTERNAL_PROTOCOLS.has('https:'), true)
  assert.equal(SHELL_OPEN_EXTERNAL_PROTOCOLS.has('mailto:'), true)
})

test('isAllowedShellOpenExternalUrl accepts http(s) and mailto', () => {
  assert.equal(isAllowedShellOpenExternalUrl('https://example.com/path'), true)
  assert.equal(isAllowedShellOpenExternalUrl('http://127.0.0.1:5174/'), true)
  assert.equal(isAllowedShellOpenExternalUrl('mailto:user@example.com'), true)
})

test('isAllowedShellOpenExternalUrl accepts Obsidian vault deep links', () => {
  // Regression: Desktop chat previously threw "Invalid external URL" for
  // obsidian:// because openExternalUrl only allowed http/https/mailto.
  const vaultLink =
    'obsidian://open?vault=b9dec633969aa228&file=Policy%2FExample.md'

  assert.equal(isAllowedShellOpenExternalUrl(vaultLink), true)
  assert.equal(isAllowedShellOpenExternalUrl('obsidian://open?vault=Lead%20Maine'), true)
  assert.equal(isAllowedShellOpenExternalUrl('OBSIDIAN://open?vault=x'), true)
})

test('isAllowedShellOpenExternalUrl rejects empty and unparseable input', () => {
  assert.equal(isAllowedShellOpenExternalUrl(''), false)
  assert.equal(isAllowedShellOpenExternalUrl('   '), false)
  assert.equal(isAllowedShellOpenExternalUrl(null), false)
  assert.equal(isAllowedShellOpenExternalUrl(undefined), false)
  assert.equal(isAllowedShellOpenExternalUrl('not a url'), false)
  assert.equal(isAllowedShellOpenExternalUrl('/relative/path.md'), false)
})

test('isAllowedShellOpenExternalUrl rejects dangerous or unlisted schemes', () => {
  // file: is handled via openPath, not this allowlist
  assert.equal(isAllowedShellOpenExternalUrl('file:///C:/Users/x/note.md'), false)
  assert.equal(isAllowedShellOpenExternalUrl('javascript:alert(1)'), false)
  assert.equal(isAllowedShellOpenExternalUrl('data:text/html,hi'), false)
  assert.equal(isAllowedShellOpenExternalUrl('vbscript:msgbox(1)'), false)
  assert.equal(isAllowedShellOpenExternalUrl('chrome://settings'), false)
  assert.equal(isAllowedShellOpenExternalUrl('about:blank'), false)
  assert.equal(isAllowedShellOpenExternalUrl('ms-settings:bluetooth'), false)
  // Not yet allowlisted — add deliberately if a product surface needs them
  assert.equal(isAllowedShellOpenExternalUrl('vscode://file/tmp/x'), false)
  assert.equal(isAllowedShellOpenExternalUrl('cursor://file/tmp/x'), false)
})
