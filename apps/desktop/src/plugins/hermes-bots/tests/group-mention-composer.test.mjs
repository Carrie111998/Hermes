import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Group-composer mentions (#89049): the room's "new thread" composer and the
// reply-in-thread box mount a member-scoped @-mention popover instead of a
// dead plain Input. Source-shape assertions plus a functional check of the
// caret tokenizer, extracted and run in isolation.

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

test('group room composers mount GroupMentionInput, not bare Input', () => {
  // Main composer (new thread) and thread reply box both use the wrapper.
  const mainComposer = source.match(/jsx\(GroupMentionInput, \{\s*'aria-label': `Message \$\{group\}`/)
  const replyComposer = source.match(/jsx\(GroupMentionInput, \{\s*'aria-label': 'Reply in thread'/)
  assert.ok(mainComposer, 'main group composer must render GroupMentionInput')
  assert.ok(replyComposer, 'thread reply box must render GroupMentionInput')

  // Neither composer renders a dead plain Input anymore.
  assert.ok(!/jsx\(Input, \{\s*'aria-label': `Message \$\{group\}`/.test(source))
  assert.ok(!/jsx\(Input, \{\s*'aria-label': 'Reply in thread'/.test(source))

  // Both receive the seated members for the popover scope (onSubmitDraft
  // rides along so Enter submits from the multi-line textarea, #89884).
  assert.ok(/GroupMentionInput\(\{ members, onChange, onSubmitDraft, value/.test(source))
})

test('popover offers @everyone/@all and inserts parser-compatible strings', () => {
  const component = source.slice(
    source.indexOf('function GroupMentionInput'),
    source.indexOf('function GroupChatWorkspace')
  )
  assert.ok(component.includes("['everyone', 'all']"), 'quick picks for the room-wide broadcast')
  // Insertion writes exactly "@handle " — the shape parseGroupChatMentions resolves —
  // but never double-spaces when the remainder already starts with whitespace.
  assert.ok(component.includes('`${value.slice(0, token.start)}@${handle}${separator}${value.slice(caret)}`'))
  assert.ok(component.includes("const separator = /^\\s/.test(value.slice(caret)) ? '' : ' '"), 'separator-aware insertion at word boundaries')
  // Member handles come from the same botHandle used by the parser.
  assert.ok(component.includes('botHandle(member.name, member)'))
  // mousedown insertion must preventDefault so the input keeps focus.
  assert.ok(/onMouseDown: event => \{\s*event\.preventDefault\(\)/.test(component))
})

test('mentionTokenAt finds the active @-token at the caret', () => {
  const start = source.indexOf('function mentionTokenAt')
  const end = source.indexOf('function GroupMentionInput')
  assert.ok(start > 0 && end > start, 'mentionTokenAt must precede GroupMentionInput')

  const ctx = {}
  vm.createContext(ctx)
  vm.runInContext(source.slice(start, end) + '\nthis.fn = mentionTokenAt', ctx)
  const fn = ctx.fn

  // Mid-word @ tokens resolve with the right query + start offset.
  // (Field-wise compare: vm-context objects have a foreign Object prototype,
  // which strict deepEqual rejects.)
  const eq = (actual, expected) => {
    assert.equal(actual?.query, expected?.query)
    assert.equal(actual?.start, expected?.start)
  }
  eq(fn('hey @al', 7), { query: 'al', start: 4 })
  eq(fn('@', 1), { query: '', start: 0 })
  eq(fn('ping @every', 11), { query: 'every', start: 5 })

  // Not a mention context: no token → popover stays closed.
  assert.equal(fn('email me a@b', 12), null)
  assert.equal(fn('plain text', 10), null)
  // Caret before the @ is not inside the token.
  assert.equal(fn('hey @al', 3), null)
})

test('insert never double-spaces at a word boundary (regression)', () => {
  // Extract the insert() body and run it with scripted closure values so the
  // separator logic is exercised for real, not just source-matched.
  const start = source.indexOf('function GroupMentionInput')
  const end = source.indexOf('function GroupChatWorkspace')
  const component = source.slice(start, end)

  const insertBody = component.slice(
    component.indexOf('const insert = handle => {'),
    component.indexOf('\n  }\n', component.indexOf('requestAnimationFrame')) + 4
  )
  const ctx = {
    inputRef: { current: { selectionStart: 7 } },
    token: { start: 6, query: '' },
    value: 'hello @ world',
    onChange: v => {
      ctx.__next = v
    },
    setToken: () => undefined,
    requestAnimationFrame: () => undefined
  }
  vm.createContext(ctx)
  vm.runInContext(insertBody + '\nthis.__insert = insert', ctx)
  ctx.__insert('herin')
  assert.equal(ctx.__next, 'hello @herin world', 'single space at word boundary, no double space')
})
