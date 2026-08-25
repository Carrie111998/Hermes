import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// #93572: RoutinesPane.openCreate() stores resolveRoutineOwner()'s result as
// CreateRoutineDialog's bot prop. The dialog wrapped that owner again and
// passed the resulting {name: <object>} shape to displayName(), which called
// string methods on the object and crashed the whole pane. Keep one normalized
// owner shape for both display and metadata lookup.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function load() {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const context = {
    atom,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: { state: { profile: { listen: () => undefined } } }
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(`
      globalThis.__api = { routineDialogBotLabel };
    `)
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  return context.__api
}

test('regression #93572: a roster-owner object composes the label without throwing', () => {
  const { routineDialogBotLabel } = load()
  assert.equal(routineDialogBotLabel({ name: 'blog-writer' }, {}), 'Blog Writer')
})

test('string owners keep their configured title through the normalized metadata lookup', () => {
  const { routineDialogBotLabel } = load()
  const metaByName = { 'blog-writer': { title: 'The Blog Writer' } }
  assert.equal(routineDialogBotLabel('blog-writer', metaByName), 'The Blog Writer')
})

test('roster-object owners keep their configured title through the same metadata lookup', () => {
  const { routineDialogBotLabel } = load()
  const metaByName = { 'blog-writer': { title: 'The Blog Writer' } }
  assert.equal(routineDialogBotLabel({ name: 'blog-writer' }, metaByName), 'The Blog Writer')
})

test('a remote roster-object owner never throws when its route cannot resolve', () => {
  const { routineDialogBotLabel } = load()
  const owner = { name: 'worker', remoteSource: true, sourceScoped: true, connectionId: 'gone' }
  const label = routineDialogBotLabel(owner, {})
  assert.equal(typeof label, 'string')
  assert.ok(label.length > 0)
})
