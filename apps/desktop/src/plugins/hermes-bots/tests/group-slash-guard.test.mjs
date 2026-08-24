import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// Group-room slash-input guard (#93947): slash commands typed in a group
// composer have no handler — sendToGroupChat delivers them verbatim as each
// member's prompt, so "/new" reached bots as literal chat text. The fix
// rejects slash-style input in BOTH room composers (new thread + thread
// reply) with a visible notice instead of submitting it.

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

test('both group composers reject slash input before clearing drafts', () => {
  const mainSubmit = source.slice(
    source.indexOf('  const submit = () => {'),
    source.indexOf('sendToGroupChat(group, memberDescriptors(), text, null, images)')
  )
  assert.ok(mainSubmit.includes('isGroupSlashInput(text)'), 'main composer must guard slash input')
  // The guard must run BEFORE the draft is cleared — a rejected command has to
  // stay in the box so the user can edit it rather than losing what they typed.
  const guardAt = mainSubmit.indexOf('isGroupSlashInput(text)')
  const clearAt = mainSubmit.indexOf('updateComposerDraft(')
  assert.ok(guardAt >= 0 && clearAt > guardAt, 'slash guard must precede the draft clear')

  const replySubmit = source.slice(
    source.indexOf('  const submitReply = thread => {'),
    source.indexOf('sendToGroupChat(group, memberDescriptors(), text, thread, images)')
  )
  assert.ok(replySubmit.includes('isGroupSlashInput(text)'), 'thread reply box must guard slash input')
})

test('guard detects slash-style first tokens and passes normal chat', () => {
  const start = source.indexOf('const GROUP_SLASH_INPUT_RE')
  const end = source.indexOf('function sendToGroupChat')
  assert.ok(start > 0 && end > start)

  const { isGroupSlashInput } = (() => {
    const fnSource = source
      .slice(start, end)
      .replace(/^const GROUP_SLASH_INPUT_RE = (.+)$/m, '')
      .replace(
        "function isGroupSlashInput",
        "exports.isGroupSlashInput = function isGroupSlashInput"
      )
    const fn = new Function('exports', 'GROUP_SLASH_INPUT_RE', `${fnSource};`)
    const exportsObj = {}
    fn(exportsObj, /^\/\S/)
    return exportsObj
  })()

  // Slash-style first token: blocked.
  assert.equal(isGroupSlashInput('/new'), true)
  assert.equal(isGroupSlashInput('/reset'), true)
  assert.equal(isGroupSlashInput('  /compact  '), true) // trimmed before matching
  assert.equal(isGroupSlashInput('/model opus'), true)
  // Mid-message slashes are ordinary prose (paths, code): delivered as-is.
  assert.equal(isGroupSlashInput('check /var/log for me'), false)
  assert.equal(isGroupSlashInput('run npm /s something'), false)
  assert.equal(isGroupSlashInput('what does /etc/hosts do?'), false)
  // Empty/nothing-to-send shapes never trip the guard.
  assert.equal(isGroupSlashInput(''), false)
  assert.equal(isGroupSlashInput(null), false)
  assert.equal(isGroupSlashInput(undefined), false)
})

test('rejection notifies visibly instead of silently dropping', () => {
  const helper = source.slice(
    source.indexOf('function notifyGroupSlashUnsupported'),
    source.indexOf('function sendToGroupChat')
  )
  assert.ok(helper.includes("host.notify?.("), 'rejection must surface a visible notice')
  assert.ok(helper.includes("kind: 'info'"), 'notice is informational, not an error')
})
