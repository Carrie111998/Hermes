import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'
import { afterAll, beforeAll, test } from 'vitest'

const desktopRoot = path.resolve(import.meta.dirname, '..')
const repoRoot = path.resolve(desktopRoot, '../..')
const viteBin = path.join(repoRoot, 'node_modules/vite/bin/vite.js')
const output = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-startup-graph-'))
let html = ''
let manifest = {}
let preloads = []

beforeAll(() => {
  execFileSync(process.execPath, [viteBin, 'build', '--outDir', output, '--emptyOutDir'], {
    cwd: desktopRoot,
    env: { ...process.env, VITE_CONFIG_NATIVE_IGNORE_WARNING: 'true' },
    stdio: 'pipe'
  })
  html = fs.readFileSync(path.join(output, 'index.html'), 'utf8')
  manifest = JSON.parse(fs.readFileSync(path.join(output, '.vite/manifest.json'), 'utf8'))
  preloads = [...html.matchAll(/<link\s+rel="modulepreload"[^>]+href="([^"]+)"/g)].map(match => match[1])
}, 30_000)

afterAll(() => fs.rmSync(output, { recursive: true, force: true }))

test('initial renderer modulepreloads stay relative and within the startup budget', () => {
  assert.ok(preloads.length > 0, 'production index should preload its static entry dependencies')
  assert.ok(preloads.every(href => href.startsWith('./assets/')), 'modulepreloads must remain valid under file://')

  const bytes = preloads.reduce((total, href) => total + fs.statSync(path.join(output, href.slice(2))).size, 0)
  assert.ok(preloads.length <= 120, `initial modulepreload count ${preloads.length} exceeds 120`)
  assert.ok(bytes <= 3_000_000, `initial modulepreload bytes ${bytes} exceeds 3,000,000`)
})

test('interaction-only rendering features are absent from initial modulepreloads', () => {
  const forbidden = /(?:katex|shiki|mermaid|code-editor|session-export|(?:create|rename|delete)-profile-dialog|edit-soul-dialog|pet-gallery)-/i
  assert.deepEqual(preloads.filter(href => forbidden.test(href)), [])
})

test('math, editor, export, profile dialogs, and gallery remain emitted as lazy features', () => {
  const emitted = Object.values(manifest).map(entry => entry.file)
  for (const feature of ['katex-', 'code-editor-', 'session-export-', 'rename-profile-dialog-', 'pet-gallery-']) {
    assert.ok(emitted.some(file => file.includes(feature)), `${feature} feature chunk was not emitted`)
  }
})
