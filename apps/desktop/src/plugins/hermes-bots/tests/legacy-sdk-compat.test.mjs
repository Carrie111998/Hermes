import assert from 'node:assert/strict'
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { pathToFileURL } from 'node:url'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')
const OPTIONAL_CAPABILITY_EXPORTS = new Set(['McpTab', 'ToolsetConfigPanel', 'SkillsView'])

function sdkNamedImports(source) {
  const match = source.match(/import\s+\{([\s\S]*?)\}\s+from '@hermes\/plugin-sdk'/)

  assert.ok(match, 'plugin.js must retain the mandatory named SDK import')

  return match[1]
    .split(',')
    .map(name => name.trim())
    .filter(Boolean)
    .map(name => name.split(/\s+as\s+/)[0])
}

function proxyModule(exportNames) {
  const exports = exportNames.map(name => `  proxy as ${name}`).join(',\n')

  return `const proxy = new Proxy(function () { return proxy }, {
  apply: () => proxy,
  get: (_target, key) => {
    if (key === Symbol.iterator) return function * () {}
    if (key === Symbol.toPrimitive) return () => 0
    if (key === 'then') return undefined
    return proxy
  }
})

export {
${exports}
}
`
}

function writeLegacySdk(root) {
  const packageRoot = join(root, 'node_modules', '@hermes', 'plugin-sdk')
  const exportNames = sdkNamedImports(pluginSource).filter(
    name => !OPTIONAL_CAPABILITY_EXPORTS.has(name)
  )

  mkdirSync(packageRoot, { recursive: true })
  writeFileSync(
    join(packageRoot, 'package.json'),
    `${JSON.stringify({ name: '@hermes/plugin-sdk', type: 'module', exports: './index.js' }, null, 2)}\n`
  )
  writeFileSync(join(packageRoot, 'index.js'), proxyModule(exportNames))
}

function writeReactStubs(root) {
  const packageRoot = join(root, 'node_modules', 'react')

  mkdirSync(packageRoot, { recursive: true })
  writeFileSync(
    join(packageRoot, 'package.json'),
    `${JSON.stringify(
      {
        name: 'react',
        type: 'module',
        exports: { '.': './index.js', './jsx-runtime': './jsx-runtime.js' }
      },
      null,
      2
    )}\n`
  )
  writeFileSync(join(packageRoot, 'index.js'), proxyModule(['useEffect', 'useMemo', 'useRef', 'useState']))
  writeFileSync(join(packageRoot, 'jsx-runtime.js'), proxyModule(['jsx', 'jsxs']))
}

// The plugin pulls blobatar's motion sheet directly (the SDK exports the
// component but not the stylesheet). Vite resolves that in the real app; node
// has no CSS loader, so the stub maps the subpath onto an empty ES module.
// Its presence is the point: this test links plugin.js for real, so it is what
// catches a non-SDK import that the bundler would resolve but a bare runtime
// cannot.
function writeBlobatarStub(root) {
  const packageRoot = join(root, 'node_modules', 'blobatar')

  mkdirSync(packageRoot, { recursive: true })
  writeFileSync(
    join(packageRoot, 'package.json'),
    `${JSON.stringify(
      {
        name: 'blobatar',
        type: 'module',
        sideEffects: ['*.css'],
        exports: { './motion.css': './motion.css.js' }
      },
      null,
      2
    )}\n`
  )
  writeFileSync(join(packageRoot, 'motion.css.js'), 'export {}\n')
}

test('legacy SDK without optional capability exports still links Bot Mode', async t => {
  const root = mkdtempSync(join(tmpdir(), 'hermes-bot-mode-legacy-sdk-'))
  const pluginPath = join(root, 'plugin.js')

  t.after(() => rmSync(root, { recursive: true, force: true }))

  writeFileSync(join(root, 'package.json'), '{"type":"module"}\n')
  writeFileSync(pluginPath, pluginSource)
  writeLegacySdk(root)
  writeReactStubs(root)
  writeBlobatarStub(root)

  const loaded = await import(pathToFileURL(pluginPath).href)

  assert.equal(loaded.default.id, 'hermes-bots')
  assert.equal(typeof loaded.default.register, 'function')
})
