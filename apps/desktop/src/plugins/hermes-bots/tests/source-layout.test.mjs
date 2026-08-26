import assert from 'node:assert/strict'
import { existsSync, readdirSync } from 'node:fs'
import test from 'node:test'
import { fileURLToPath } from 'node:url'
import { dirname, join, resolve } from 'node:path'

const pluginsRoot = resolve(dirname(fileURLToPath(import.meta.url)), '../..')

test('plugin JavaScript sources never shadow TypeScript siblings', () => {
  const collisions = readdirSync(pluginsRoot, { withFileTypes: true })
    .filter(entry => entry.isDirectory())
    .map(entry => entry.name)
    .filter(name => {
      const root = join(pluginsRoot, name)
      return (
        existsSync(join(root, 'plugin.js')) &&
        (existsSync(join(root, 'plugin.ts')) || existsSync(join(root, 'plugin.tsx')))
      )
    })

  assert.deepEqual(collisions, [])
})
