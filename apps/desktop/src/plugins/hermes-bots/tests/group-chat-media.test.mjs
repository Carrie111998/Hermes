import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

test('group room uses the canonical raw-message renderer with an older-SDK fallback (#93728)', () => {
  const setup = source.slice(0, source.indexOf('export default'))
  const workspace = source.slice(source.indexOf('function GroupChatWorkspace'))
  const renderEntry = workspace.slice(workspace.indexOf('const renderEntry'), workspace.indexOf('// Threads:'))

  assert.match(setup, /MessageTextContent = typeof sdk === 'undefined' \? undefined : sdk\.MessageTextContent/)
  assert.match(renderEntry, /MessageTextContent\s*\? jsx\(MessageTextContent, \{ text: entry\.text \}\)/)
  assert.match(renderEntry, /Streamdown\s*\? jsx\(Streamdown, \{ children: entry\.text \}\)/)
})
