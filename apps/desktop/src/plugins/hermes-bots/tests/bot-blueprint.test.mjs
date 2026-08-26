/**
 * Bot blueprints — one plain-text description of a team becomes N bots.
 *
 * Pins the parser contract (prose / list / JSON in, deduped specs out) and the
 * batch creator's failure policy: one bad `profiles.create` must not abort the
 * rest of the roster, and a cosmetic look failure must not un-create a profile
 * that the gateway already made.
 */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

/** Arrays built inside the vm carry the sandbox's Array.prototype, so
 *  deepStrictEqual against a literal here fails on the prototype alone even
 *  when every element matches. Re-materialise them in this realm first. */
const plain = (list, map = value => value) => Array.from(list, map)

function load({ failOn = null } = {}) {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const calls = []
  const invalidated = []
  const context = {
    atom,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    queryClient: { invalidateQueries: args => invalidated.push(args) },
    host: {
      state: { profile: { listen: () => undefined } },
      request: async (method, params) => {
        calls.push({ method, params })

        if (failOn && method === 'profiles.create' && params?.name === failOn) {
          throw new Error(`profile ${params.name} already exists`)
        }

        return { ok: true }
      }
    }
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(
      '\nglobalThis.__bp = { parseBotBlueprint, createBotsFromBlueprint, blueprintLook, $botMeta, BLUEPRINT_LIMIT };\n'
    )
  vm.runInNewContext(source, context, { filename: 'plugin.js' })

  return { context, calls, invalidated, ...context.__bp }
}

test('unit: prose with "named" yields one bot per named agent', () => {
  const { parseBotBlueprint } = load()
  const { specs } = parseBotBlueprint(
    'I need a research assistant named Ada who reads papers, and a writer called Bob who drafts summaries'
  )

  assert.deepEqual(plain(specs, spec => spec.name), ['ada', 'bob'])
  assert.equal(specs[0].title, 'research assistant')
  assert.equal(specs[0].description, 'reads papers')
  assert.equal(specs[1].title, 'writer')
  assert.equal(specs[1].description, 'drafts summaries')
})

test('regression: commas inside one mission do not spawn extra bots', () => {
  const { parseBotBlueprint } = load()
  const { specs } = parseBotBlueprint('a bot named Bob who writes summaries, drafts, and release notes')

  // A clause with no name signal is a continuation, not a new bot.
  assert.deepEqual(plain(specs, spec => spec.name), ['bob'])
  assert.equal(specs[0].description, 'writes summaries, drafts, release notes')
})

test('unit: bulleted lines parse into handle / role / mission', () => {
  const { parseBotBlueprint } = load()
  const { specs } = parseBotBlueprint(
    [
      '- Ada — Researcher: reads papers, then files notes',
      '- Bob (Writer) drafts the summary',
      '3. Cleo: fact-checks claims'
    ].join('\n')
  )

  assert.deepEqual(plain(specs, spec => [spec.name, spec.title, spec.description]), [
    ['ada', 'Researcher', 'reads papers, then files notes'],
    ['bob', 'Writer', 'drafts the summary'],
    ['cleo', '', 'fact-checks claims']
  ])
})

test('unit: an explicit JSON roster is taken verbatim', () => {
  const { parseBotBlueprint } = load()
  const { specs } = parseBotBlueprint(
    JSON.stringify([
      { name: 'Inbox Triage', title: 'Sorter', description: 'triages mail' },
      { name: 'digest', role: 'Summariser', mission: 'writes the daily digest' }
    ])
  )

  assert.deepEqual(plain(specs, spec => [spec.name, spec.title, spec.description]), [
    ['inbox-triage', 'Sorter', 'triages mail'],
    ['digest', 'Summariser', 'writes the daily digest']
  ])
})

test('unit: handles collide-resolve against the roster and within the batch', () => {
  const { parseBotBlueprint } = load()
  const { specs } = parseBotBlueprint('- Ada: reads papers\n- Ada: writes notes', [{ name: 'ada' }])

  assert.deepEqual(plain(specs, spec => spec.name), ['ada-2', 'ada-3'])
  assert.equal(specs[0].renamedFrom, 'ada')
})

test('unit: the batch is capped and says so', () => {
  const { parseBotBlueprint, BLUEPRINT_LIMIT } = load()
  const lines = Array.from({ length: BLUEPRINT_LIMIT + 5 }, (_, n) => `- bot${n}: does a thing`)
  const { specs, warnings } = parseBotBlueprint(lines.join('\n'))

  assert.equal(specs.length, BLUEPRINT_LIMIT)
  assert.match(plain(warnings).join(' '), /Only the first 24 bots/)
})

test('unit: an unparseable description yields no specs and an actionable warning', () => {
  const { parseBotBlueprint } = load()
  const { specs, warnings } = parseBotBlueprint('!!! ???')

  assert.equal(specs.length, 0)
  assert.match(plain(warnings).join(' '), /No bots found/)
})

test('unit: creating a blueprint issues one profiles.create per spec, with a SOUL', async () => {
  const { parseBotBlueprint, createBotsFromBlueprint, calls, invalidated } = load()
  const { specs } = parseBotBlueprint('- Ada — Researcher: reads papers\n- Bob — Writer: drafts summaries')
  const result = await createBotsFromBlueprint({ specs })

  assert.deepEqual(plain(result.created), ['ada', 'bob'])
  assert.equal(result.failed.length, 0)

  const creates = calls.filter(call => call.method === 'profiles.create')
  assert.deepEqual(plain(creates, call => call.params.name), ['ada', 'bob'])
  assert.equal(creates[0].params.clone_from, 'default')
  assert.equal(creates[0].params.description, 'Researcher — reads papers')
  assert.match(creates[0].params.soul, /Researcher/)
  assert.match(creates[0].params.soul, /reads papers/)
  assert.equal(invalidated.length, 1)
})

test('regression: one failed profile does not abort the rest of the batch', async () => {
  const { parseBotBlueprint, createBotsFromBlueprint, calls } = load({ failOn: 'bob' })
  const { specs } = parseBotBlueprint('- Ada: reads papers\n- Bob: drafts summaries\n- Cleo: checks facts')
  const result = await createBotsFromBlueprint({ specs })

  assert.deepEqual(plain(result.created), ['ada', 'cleo'])
  assert.deepEqual(plain(result.failed, entry => entry.name), ['bob'])
  assert.match(result.failed[0].message, /already exists/)

  // Cleo was still attempted after Bob threw.
  assert.ok(calls.some(call => call.method === 'profiles.create' && call.params.name === 'cleo'))
})

test('unit: each created bot gets a distinct colour and its title as the look', async () => {
  const { parseBotBlueprint, createBotsFromBlueprint, $botMeta } = load()
  const { specs } = parseBotBlueprint('- Ada — Researcher: reads papers\n- Bob — Writer: drafts summaries')
  await createBotsFromBlueprint({ specs })

  const meta = $botMeta.get()
  assert.equal(meta.ada.title, 'Researcher')
  assert.equal(meta.bob.title, 'Writer')
  assert.notEqual(meta.ada.color, meta.bob.color)
  assert.equal(meta.ada.imageKind, 'shape')
})

test('unit: progress is reported for every spec', async () => {
  const { parseBotBlueprint, createBotsFromBlueprint } = load({ failOn: 'bob' })
  const { specs } = parseBotBlueprint('- Ada: reads papers\n- Bob: drafts summaries')
  const seen = []
  await createBotsFromBlueprint({ specs, onProgress: event => seen.push(`${event.name}:${event.phase}`) })

  assert.deepEqual(seen, ['ada:creating', 'ada:created', 'bob:creating', 'bob:failed'])
})

// The dialog is a React closure, so the wiring is pinned against the source
// the way the other create-dialog tests do it.

test('unit: the roster "+" menu offers the blueprint dialog', () => {
  assert.match(pluginSource, /onSelect: \(\) => setBlueprintOpen\(true\)/)
  assert.match(pluginSource, /'New Bots from Description…'/)
})

test('unit: the blueprint dialog is mounted and refetches the roster on close', () => {
  const mount = pluginSource.slice(
    pluginSource.indexOf('jsx(CreateBlueprintDialog, {'),
    pluginSource.indexOf('jsx(CreateAgentDialog, {')
  )

  assert.match(mount, /open: blueprintOpen/)
  assert.match(mount, /setBlueprintOpen\(false\)/)
  assert.match(mount, /void refetch\(\)/)
  assert.match(mount, /roster: activeSourceRoster/)
})

test('regression: the preview renders the same look creation persists', () => {
  const dialog = pluginSource.slice(
    pluginSource.indexOf('function CreateBlueprintDialog('),
    pluginSource.indexOf('// ── create dialog ─')
  )

  // A preview drawn from anything but blueprintLook would drift from the
  // avatar the bot is actually created with.
  assert.match(dialog, /const look = blueprintLook\(spec, index\)/)
  assert.match(dialog, /parseBotBlueprint\(text, roster\)/)
})

test('regression: an instruction preamble is not read as a bot', () => {
  const { parseBotBlueprint } = load()
  const { specs } = parseBotBlueprint(
    'I want a support team: a triage bot named Sentry that reads tickets, and a writer called Quill who drafts replies'
  )

  // "I want a support team:" introduces the roster — it is not a member of it,
  // and it must not leak into the first bot's role either.
  assert.deepEqual(plain(specs, spec => spec.name), ['sentry', 'quill'])
  assert.equal(specs[0].title, 'triage bot')
})

test('regression: a header line above a bulleted list is not a bot', () => {
  const { parseBotBlueprint } = load()
  const { specs } = parseBotBlueprint(
    ['Build me a newsroom:', '- editor — Editor: assigns stories', '- reporter — Reporter: writes the story'].join('\n')
  )

  assert.deepEqual(plain(specs, spec => spec.name), ['editor', 'reporter'])
})

test('unit: a bare list of proper nouns becomes one bot each', () => {
  const { parseBotBlueprint } = load()
  const { specs } = parseBotBlueprint('Create three bots: Alpha, Beta and Gamma')

  assert.deepEqual(plain(specs, spec => spec.name), ['alpha', 'beta', 'gamma'])
  // A handle that is the whole clause should not repeat itself as the mission.
  assert.equal(specs[0].description, '')
})

test('regression: blueprintOpen is declared in the pane that reads it', () => {
  // `createOpen` is declared TWICE — once in RoutinesPane, once in BotsPane.
  // The menu item and the dialog mount both live in BotsPane, so a declaration
  // that lands next to the first one is a ReferenceError the moment the pane
  // renders. The vm harness never executes these components, so pin the scope.
  const pane = pluginSource.slice(pluginSource.indexOf('function BotsPane() {'))

  assert.match(pane, /const \[blueprintOpen, setBlueprintOpen\] = useState\(false\)/)
  assert.match(pane, /setBlueprintOpen\(true\)/)
  assert.match(pane, /open: blueprintOpen/)

  const routines = pluginSource.slice(
    pluginSource.indexOf('function RoutinesPane() {'),
    pluginSource.indexOf('function BotsPane() {')
  )
  assert.doesNotMatch(routines, /blueprintOpen/)
})
