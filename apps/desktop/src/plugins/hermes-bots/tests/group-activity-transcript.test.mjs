import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadTranscriptHelpers() {
  const start = source.indexOf('const GROUP_ACTIVITY_TRANSCRIPT_LIMIT')
  const end = source.indexOf('/** Bot-scoped activity drawer.')

  assert.ok(start >= 0 && end > start, 'transcript helper block must remain extractable')

  const context = {}
  vm.runInNewContext(
    `function groupMemberKey(member) { return member.key || member.name }\n${source.slice(start, end)}\n` +
      'globalThis.__rows = groupTranscriptRows;\n' +
      'globalThis.__belongs = groupActivityBelongsToMember;\n',
    context
  )

  return context
}

test('observable transcript preserves user, assistant, tool call, and tool result order', () => {
  const rows = loadTranscriptHelpers().__rows([
    { role: 'system', content: 'hidden system prompt' },
    { role: 'user', content: 'Room delta', created_at: 10 },
    {
      role: 'assistant',
      content: [{ type: 'reasoning', text: 'private chain of thought' }],
      tool_calls: [{ function: { name: 'terminal', arguments: '{"cmd":"pwd"}' } }]
    },
    { role: 'tool', name: 'terminal', content: '/workspace' },
    { role: 'assistant', content: [{ type: 'output_text', text: 'Done.' }] }
  ])

  assert.deepEqual(
    Array.from(rows, row => `${row.kind}:${row.role}:${row.name || ''}`),
    ['message:user:', 'tool-call:tool:terminal', 'tool-result:tool:terminal', 'message:assistant:']
  )
  assert.equal(rows[0].at, 10_000)
  assert.equal(rows.at(-1).text, 'Done.')
  assert.ok(rows.every(row => !row.text.includes('private chain of thought')))
  assert.ok(rows.every(row => !row.text.includes('hidden system prompt')))
})

test('reasoning/thinking blocks are omitted while visible text blocks remain', () => {
  const rows = loadTranscriptHelpers().__rows([
    {
      role: 'assistant',
      content: [
        { type: 'thinking', text: 'do not expose' },
        { type: 'analysis', text: 'also private' },
        { type: 'output_text', text: 'Visible answer' }
      ]
    }
  ])

  assert.equal(rows.length, 1)
  assert.equal(rows[0].text, 'Visible answer')
})

test('activity filtering prefers source-qualified member keys', () => {
  const belongs = loadTranscriptHelpers().__belongs
  const local = { name: 'researcher', key: 'local::researcher' }
  const remote = { name: 'researcher', key: 'remote::researcher' }

  assert.equal(belongs({ member: 'researcher', memberKey: 'local::researcher' }, local), true)
  assert.equal(belongs({ member: 'researcher', memberKey: 'local::researcher' }, remote), false)
  // Legacy runtime events without memberKey retain name-based readability.
  assert.equal(belongs({ member: 'researcher' }, remote), true)
})

test('group working indicator is an avatar button and transcript reads canonical session messages', () => {
  assert.match(source, /title: `\$\{label\} is working — open activity`/)
  assert.match(source, /onClick: \(\) => setActivityMemberKey\(groupMemberKey\(activeActivityMember\)\)/)
  assert.doesNotMatch(source, /`\$\{groupSpeakerLabel\(room\.turn\)\} is thinking…`/)
  assert.match(source, /r\.turnKey = memberKey/)
  assert.match(source, /omit_messages: false/)
  assert.match(source, /private model reasoning is never displayed/)
})
