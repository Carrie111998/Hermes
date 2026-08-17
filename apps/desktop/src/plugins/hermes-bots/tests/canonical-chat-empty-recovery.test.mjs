import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadCanonicalRecovery({ openSession, request }) {
  const start = source.indexOf('const canonicalCreations = new Map()')
  const end = source.indexOf('function displayName(', start)
  const saved = []
  const context = {
    host: { openSession, request },
    saveBotMeta: (name, patch) => saved.push({ name, patch }),
    $hideBotChats: { get: () => false },
    window: { setTimeout: callback => callback() }
  }
  const section = source
    .slice(start, end)
    .concat('\nglobalThis.__canonical = { openBotCanonicalChat };\n')

  assert.notEqual(start, -1, 'canonical chat section is missing')
  assert.notEqual(end, -1, 'canonical chat section delimiter is missing')
  vm.runInNewContext(section, context, { filename: 'canonical-recovery.js' })
  return { ...context.__canonical, saved }
}

test('regression: a definitively-gone pin with no history clears and creates a replacement', async () => {
  // New contract (hermes-agent#88200): the pin is verified through the
  // backend's precise preferred_session resolver — NOT a paginated,
  // hidden-excluding session.list window. preferred_session=null is the
  // definitive "this session is gone"; with no previewed history to
  // re-anchor on, recovery clears the pin and creates a fresh chat.
  const opened = []
  const runtime = loadCanonicalRecovery({
    openSession: async id => opened.push(id),
    request: async method => {
      if (method === 'profiles.list') return { profiles: [{ name: 'ops', preferred_session: null }] }
      if (method === 'session.create') return { stored_session_id: 'replacement', session_id: 'replacement-runtime' }
      return {}
    }
  })

  assert.equal(await runtime.openBotCanonicalChat('ops', 'stale-pin', null), 'replacement')
  assert.deepEqual(opened, ['replacement'])
  assert.deepEqual(JSON.parse(JSON.stringify(runtime.saved)), [
    { name: 'ops', patch: { chat: null } },
    { name: 'ops', patch: { chat: 'replacement' } }
  ])
})
