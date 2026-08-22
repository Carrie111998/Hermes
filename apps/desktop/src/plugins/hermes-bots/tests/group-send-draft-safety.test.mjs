import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

// Live-repro'd (Aug 2026 desktop audit): a fully-typed room message could
// vanish — no thread, no error — when the member seat was empty (roster
// hydration race / legacy room record). Two contracts prevent a regression:
// 1. sendToGroupChat surfaces the empty-member case instead of silently
//    returning null.
// 2. submit()/submitReply() clear the draft/images ONLY after a successful
//    send, so a rejected send never eats the user's text.

test('source contract: empty member seat surfaces an error, not a silent null', () => {
  assert.match(pluginSource, /members are still loading/)
  // The guard is split: empty text returns silently, empty members notifies.
  assert.doesNotMatch(pluginSource, /\(!trimmed && !attached\.length\) \|\| !members\.length/)
})

test('source contract: main composer clears the draft only after a minted thread', () => {
  const fn = pluginSource.slice(pluginSource.indexOf('const submit = () => {'))
  const body = fn.slice(0, fn.indexOf('const submitReply'))
  const mintedIdx = body.indexOf('const minted = sendToGroupChat(')
  const clearIdx = body.indexOf("setDraft('')")
  assert.ok(mintedIdx > -1 && clearIdx > -1, 'both sites present')
  assert.ok(clearIdx > mintedIdx, 'draft cleared after send, inside the minted guard')
  assert.match(body, /if \(minted\) \{\s*setDraft\(''\)/)
})

test('source contract: reply box clears its draft only after the send landed', () => {
  const fn = pluginSource.slice(pluginSource.indexOf('const submitReply = thread => {'))
  const body = fn.slice(0, fn.indexOf('attachmentRow'))
  assert.match(body, /const landed = sendToGroupChat\(/)
  assert.match(body, /if \(landed\) \{\s*setReplyDrafts/)
})
