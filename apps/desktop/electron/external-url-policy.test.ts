import assert from 'node:assert/strict'

import { test } from 'vitest'

import { classifyExternalUrl } from './external-url-policy'

test('classifyExternalUrl accepts validated local file URLs', () => {
  const result = classifyExternalUrl('file:///C:/Users/example/My%20Note.md')

  assert.equal(result?.kind, 'file')
  assert.equal(result?.url.protocol, 'file:')
})

test('classifyExternalUrl accepts Obsidian open-note URLs', () => {
  const result = classifyExternalUrl('obsidian://open?vault=Personal&file=00%20Inbox%2FMy%20Note.md')

  assert.equal(result?.kind, 'external')
  assert.equal(result?.url.protocol, 'obsidian:')
  assert.equal(result?.url.hostname, 'open')
})

test('classifyExternalUrl rejects other application protocols and Obsidian actions', () => {
  assert.equal(classifyExternalUrl('file://attacker/share/note.md'), null)
  assert.equal(classifyExternalUrl('file:////attacker/share/note.md'), null)
  assert.equal(classifyExternalUrl('file://///attacker/share/note.md'), null)
  assert.equal(classifyExternalUrl('file://localhost//attacker/share/note.md'), null)
  assert.equal(classifyExternalUrl('vscode://file/C:/Users/example/note.md'), null)
  assert.equal(classifyExternalUrl('javascript:alert(1)'), null)
  assert.equal(classifyExternalUrl('obsidian://new?vault=Personal&name=Injected'), null)
  assert.equal(classifyExternalUrl('obsidian://open@attacker?vault=Personal'), null)
  assert.equal(classifyExternalUrl('obsidian://open/other-action?vault=Personal'), null)
})
