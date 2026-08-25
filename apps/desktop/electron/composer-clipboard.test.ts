import assert from 'node:assert/strict'

import { test } from 'vitest'

import { clipboardTextExtension, composerTextFilenamePrefix, hasClipboardText } from './composer-clipboard'

test('clipboard objects and arrays use the json extension', () => {
  assert.equal(clipboardTextExtension('{"name":"Hermes"}'), '.json')
  assert.equal(clipboardTextExtension('\n [1, 2, 3] \n'), '.json')
})

test('clipboard primitives and malformed JSON use the markdown extension', () => {
  for (const text of ['42', 'true', '"hi"', 'null', '{not json}', '[not json]']) {
    assert.equal(clipboardTextExtension(text), '.md')
  }
})

test('whitespace-only clipboard text is not saved', () => {
  assert.equal(hasClipboardText(' \n\t '), false)
  assert.equal(hasClipboardText('clipboard text'), true)
})

test('filename prefix stays inside the composer-files directory', () => {
  const prefix = composerTextFilenamePrefix('../\u0000report<>:"/\\|?*')

  assert.equal(prefix, '..report')
  assert.ok(!prefix.includes('/'))
  assert.ok(!prefix.includes('\\'))
  assert.ok(!prefix.includes(String.fromCodePoint(0)))
})

test('filename prefix does not split supplementary-plane characters', () => {
  const text = `${'a'.repeat(29)}😀suffix`

  assert.equal(composerTextFilenamePrefix(text), `${'a'.repeat(29)}😀`)
})
