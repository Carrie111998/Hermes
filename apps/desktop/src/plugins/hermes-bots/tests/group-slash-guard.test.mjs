import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// Group-room slash-input guard (#93947): slash commands typed in a group
// composer have no handler — sendToGroupChat delivers them verbatim as each
// member's prompt, so "/new" reached bots as literal chat text. The fix
// rejects COMMAND-SHAPED input ("/new", "/model opus") in BOTH room
// composers while letting prose that merely starts with a path ("/etc/hosts
// — what is this?", "/usr/bin/env python3 …") through, backed by a
// defense-in-depth refusal inside sendToGroupChat itself.
//
// The guard functions are single-file by loader constraint (plugin.js loads
// as a blob import, so sibling modules cannot resolve), so these tests
// evaluate the ACTUAL plugin source slices instead of re-typing the logic:
// the regex under test IS the regex that ships.

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

/** Evaluate a slice of the real plugin.js with the given dependencies in scope.
 *  Returns the names listed in `extract` (defaults to the dep names). */
function evalSlice(startMarker, endMarker, deps = {}, extract = null) {
  const start = source.indexOf(startMarker)
  const end = source.indexOf(endMarker)

  assert.ok(start >= 0, `start marker not found: ${startMarker}`)
  assert.ok(end > start, `end marker not found or out of order: ${endMarker}`)

  const names = Object.keys(deps)
  const extracted = extract || names
  const factory = new Function(
    ...names,
    `${source.slice(start, end)}\nreturn { ${extracted.join(', ')} };`
  )

  return factory(...names.map(n => deps[n]))
}

const { isGroupSlashInput } = evalSlice(
  'const GROUP_SLASH_INPUT_RE',
  'function notifyGroupSlashUnsupported',
  {},
  ['isGroupSlashInput']
)

test('guard blocks command-shaped first tokens and passes normal chat', () => {
  // Slash commands: blocked.
  assert.equal(isGroupSlashInput('/new'), true)
  assert.equal(isGroupSlashInput('/reset'), true)
  assert.equal(isGroupSlashInput('  /compact  '), true) // trimmed before matching
  assert.equal(isGroupSlashInput('/model opus'), true) // args allowed
  assert.equal(isGroupSlashInput('/Model'), true) // case-insensitive command word

  // Path-like prose openers are ordinary chat: delivered as-is. These were
  // FALSE-blocked by the original /^\S/ shape.
  assert.equal(isGroupSlashInput('/usr/bin/env python3 --version'), false)
  assert.equal(isGroupSlashInput('/etc/hosts — what is this?'), false)

  // Mid-message slashes were never guarded.
  assert.equal(isGroupSlashInput('check /var/log for me'), false)
  assert.equal(isGroupSlashInput('run npm /s something'), false)
  assert.equal(isGroupSlashInput('what does /etc/hosts do?'), false)

  // Non-command shapes never trip the guard.
  assert.equal(isGroupSlashInput('/'), false) // bare slash, no token
  assert.equal(isGroupSlashInput('/2 fast'), false) // digit-led, not a command word
  assert.equal(isGroupSlashInput(''), false)
  assert.equal(isGroupSlashInput(null), false)
  assert.equal(isGroupSlashInput(undefined), false)
})

test('rejection notice names the tripped token', () => {
  const seen = []
  const host = { notify: n => seen.push(n) }

  const { notifyGroupSlashUnsupported } = evalSlice(
    'function notifyGroupSlashUnsupported',
    '// One shared rejection point',
    { host },
    ['notifyGroupSlashUnsupported']
  )

  notifyGroupSlashUnsupported('/model opus')

  assert.equal(seen.length, 1)
  assert.equal(seen[0].kind, 'info', 'notice is informational, not an error')
  assert.match(seen[0].message, /\/model/, 'notice should name what tripped the guard')

  seen.length = 0
  notifyGroupSlashUnsupported('')

  assert.match(seen[0].message, /was not sent/, 'fallback wording survives empty input')
})

test('shared helper rejects visibly once and passes plain text silently', () => {
  const seen = []
  const host = { notify: n => seen.push(n) }

  const { rejectGroupSlashIfNeeded } = evalSlice(
    '// One shared rejection point',
    'function sendToGroupChat',
    {
      host,
      isGroupSlashInput,
      notifyGroupSlashUnsupported: text => {
        seen.push(text)
      }
    },
    ['rejectGroupSlashIfNeeded']
  )

  assert.equal(rejectGroupSlashIfNeeded('/new'), true)
  assert.deepEqual(seen, ['/new'])

  seen.length = 0
  assert.equal(rejectGroupSlashIfNeeded('hello team'), false)
  assert.deepEqual(seen, [], 'plain text must not notify')
})

test('both group composers route rejection through the shared helper before clearing drafts', () => {
  const mainStart = source.indexOf('  const submit = () => {')
  const mainEnd = source.indexOf('sendToGroupChat(group, memberDescriptors(), text, null, images)')
  assert.ok(mainStart > 0 && mainEnd > mainStart, 'main composer submit not found')

  const mainSubmit = source.slice(mainStart, mainEnd)
  assert.ok(mainSubmit.includes('rejectGroupSlashIfNeeded(text)'), 'main composer must use the shared guard')
  const guardAt = mainSubmit.indexOf('rejectGroupSlashIfNeeded(text)')
  const clearAt = mainSubmit.indexOf('updateComposerDraft(')
  assert.ok(guardAt >= 0 && clearAt > guardAt, 'slash guard must precede the draft clear')

  const replyStart = source.indexOf('  const submitReply = thread => {')
  const replyEnd = source.indexOf('sendToGroupChat(group, memberDescriptors(), text, thread, images)')
  assert.ok(replyStart > 0 && replyEnd > replyStart, 'reply composer submit not found')

  const replySubmit = source.slice(replyStart, replyEnd)
  assert.ok(replySubmit.includes('rejectGroupSlashIfNeeded(text)'), 'thread reply box must use the shared guard')
  const replyGuardAt = replySubmit.indexOf('rejectGroupSlashIfNeeded(text)')
  const replyClearAt = replySubmit.indexOf('updateComposerDraft(')
  assert.ok(replyGuardAt >= 0 && replyClearAt > replyGuardAt, 'slash guard must precede the draft clear')
})

test('sendToGroupChat refuses slash input before delivery (backstop)', () => {
  const start = source.indexOf('function sendToGroupChat')
  const deliverAt = source.indexOf('appendGroupChatEntry(group,', start)
  assert.ok(start > 0 && deliverAt > start, 'sendToGroupChat delivery path not found')

  const head = source.slice(start, deliverAt)
  const backstopAt = head.indexOf('isGroupSlashInput(trimmed)')
  assert.ok(backstopAt > 0, 'delivery entry point must refuse command-shaped input')
})
