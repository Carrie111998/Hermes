import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import crypto from 'node:crypto'
import fs from 'node:fs'
import { createRequire } from 'node:module'
import path from 'node:path'

import { test } from 'vitest'

const REPO_ROOT = path.resolve(__dirname, '..')
const WEBSITE_ROOT = path.join(REPO_ROOT, 'website')
const ADAPTER_ROOT = path.join(REPO_ROOT, 'vendor', 'brace-expansion-compat')
const require = createRequire(import.meta.url)

type ExpandOptions = { max?: number; maxLength?: number }
type LegacyBraceExpansion = {
  (pattern: string, options?: ExpandOptions): string[]
  expand: (pattern: string, options?: ExpandOptions) => string[]
  EXPANSION_MAX: number
  EXPANSION_MAX_LENGTH: number
}

function readJson(file: string): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(file, 'utf8'))
}

function assertGraphUsesOnlyPatchedAdapter(root: string): void {
  const lock = readJson(path.join(root, 'package-lock.json'))
  const packages = lock.packages as Record<string, Record<string, unknown>>

  const records = Object.entries(packages).filter(
    ([packagePath]) =>
      packagePath === 'node_modules/brace-expansion' ||
      packagePath.endsWith('/node_modules/brace-expansion')
  )

  assert.ok(records.length > 0, `${root} must resolve brace-expansion`)

  for (const [packagePath, record] of records) {
    const resolvedRecord = record.link
      ? packages[String(record.resolved)]
      : record

    assert.ok(
      resolvedRecord,
      `${packagePath} link target ${String(record.resolved)} must exist in the lockfile`
    )
    assert.equal(
      resolvedRecord.version,
      '5.0.8',
      `${packagePath} must use the CVE-2026-14257 patched release`
    )
  }
}

function minimatchPackagePath(version: string): string {
  const lock = readJson(path.join(REPO_ROOT, 'package-lock.json'))
  const packages = lock.packages as Record<string, Record<string, unknown>>

  const match = Object.entries(packages).find(
    ([packagePath, record]) =>
      (packagePath === 'node_modules/minimatch' ||
        packagePath.endsWith('/node_modules/minimatch')) &&
      record.version === version
  )

  if (!match) {
    throw new Error(`root lockfile must contain minimatch ${version}`)
  }

  return path.join(REPO_ROOT, match[0])
}

function runConstrainedNumericSequence(
  label: string,
  modulePath: string,
  directAdapter = false
): void {
  const childScript = String.raw`
    const loaded = require(${JSON.stringify(modulePath)})
    const expand = ${directAdapter ? 'loaded' : 'loaded.braceExpand'}
    if (typeof expand !== 'function') throw new TypeError('missing callable expansion API')
    const values = expand('{1..5000000}')
    const total = values.reduce((sum, value) => sum + value.length, 0)
    console.log(JSON.stringify({ count: values.length, total }))
    if (values.length > 100000 || total > 4000000) process.exit(1)
  `

  const child = spawnSync(
    process.execPath,
    ['--max-old-space-size=64', '-e', childScript],
    {
      cwd: REPO_ROOT,
      encoding: 'utf8',
      timeout: 10_000,
    }
  )

  assert.equal(child.error, undefined, `${label}: ${String(child.error)}`)
  assert.equal(child.signal, null, `${label}: terminated by ${String(child.signal)}`)
  assert.equal(child.status, 0, `${label}: ${child.stderr}`)
  const result = JSON.parse(child.stdout) as { count: number; total: number }
  assert.equal(result.count, 100_000, `${label}: unexpected result cap`)
  assert.ok(result.total <= 4_000_000, `${label}: character limit exceeded`)
}

test('both npm graphs pin the reviewed compatibility adapter', () => {
  const rootPackage = readJson(path.join(REPO_ROOT, 'package.json'))
  const rootDevDependencies = rootPackage.devDependencies as Record<string, string>
  const rootOverrides = rootPackage.overrides as Record<string, string>
  assert.equal(
    rootDevDependencies['brace-expansion'],
    'file:vendor/brace-expansion-compat/brace-expansion-compat-5.0.8-hermes.2.tgz'
  )
  assert.equal(rootOverrides['brace-expansion'], '$brace-expansion')

  const websitePackage = readJson(path.join(WEBSITE_ROOT, 'package.json'))
  const websiteDependencies = websitePackage.dependencies as Record<string, string>
  const websiteOverrides = websitePackage.overrides as Record<string, string>
  assert.equal(
    websiteDependencies['brace-expansion'],
    'file:../vendor/brace-expansion-compat/brace-expansion-compat-5.0.8-hermes.2.tgz'
  )
  assert.equal(websiteOverrides['brace-expansion'], '$brace-expansion')

  assertGraphUsesOnlyPatchedAdapter(REPO_ROOT)
  assertGraphUsesOnlyPatchedAdapter(WEBSITE_ROOT)
})

test('vendored security implementation matches the reviewed 5.0.8 tarball', () => {
  const manifest = fs
    .readFileSync(path.join(ADAPTER_ROOT, 'UPSTREAM_SHA256SUMS'), 'utf8')
    .trim()
    .split('\n')

  for (const line of manifest) {
    const [expected, relativePath] = line.split(/\s+/, 2)

    const actual = crypto
      .createHash('sha256')
      .update(fs.readFileSync(path.join(ADAPTER_ROOT, relativePath)))
      .digest('hex')

    assert.equal(actual, expected, `${relativePath} differs from upstream 5.0.8`)
  }
})

test('CommonJS keeps the legacy callable API and exposes the patched named API', () => {
  const expand = require('brace-expansion') as LegacyBraceExpansion

  assert.equal(typeof expand, 'function')
  assert.equal(typeof expand.expand, 'function')
  assert.equal(expand.EXPANSION_MAX, 100_000)
  assert.equal(expand.EXPANSION_MAX_LENGTH, 4_000_000)
  assert.deepEqual(expand('x{a,b}y'), ['xay', 'xby'])
  assert.equal(
    expand('{1..100001}').length,
    100_000,
    'legacy callable API must retain the patched result-count cap'
  )

  const bounded = expand.expand('{a,b}'.repeat(100), {
    max: 1_000,
    maxLength: 10_000,
  })

  const totalCharacters = bounded.reduce((total, value) => total + value.length, 0)
  assert.ok(
    totalCharacters <= 10_000,
    `CVE-2026-14257 regression: expanded ${totalCharacters} characters`
  )
})

test('known advisory input stays bounded in a constrained child process', () => {
  const childScript = String.raw`
    const expand = require('brace-expansion')
    const values = expand('{a,b}'.repeat(500))
    const total = values.reduce((sum, value) => sum + value.length, 0)
    console.log(JSON.stringify({ count: values.length, total }))
    if (total > 4000000) process.exit(1)
  `

  const child = spawnSync(
    process.execPath,
    ['--max-old-space-size=256', '-e', childScript],

    {
      cwd: REPO_ROOT,
      encoding: 'utf8',
      timeout: 10_000,
    }
  )

  assert.equal(child.error, undefined)
  assert.equal(child.signal, null)
  assert.equal(child.status, 0, child.stderr)
  const result = JSON.parse(child.stdout) as { count: number; total: number }

  assert.ok(result.count > 0)
  assert.ok(result.total <= 4_000_000)
})

test('legacy and real minimatch consumers cap large numeric sequences', () => {
  runConstrainedNumericSequence(
    'legacy adapter',
    require.resolve('brace-expansion'),
    true
  )

  for (const version of ['3.1.5', '5.1.9', '9.0.9']) {
    runConstrainedNumericSequence(
      `minimatch ${version}`,
      minimatchPackagePath(version)
    )
  }
})

test('ES modules receive the native patched named API', async () => {
  const secure = await import('brace-expansion')

  assert.equal(typeof secure.expand, 'function')
  assert.equal(secure.EXPANSION_MAX, 100_000)
  assert.equal(secure.EXPANSION_MAX_LENGTH, 4_000_000)
  assert.deepEqual(secure.expand('x{a,b}y'), ['xay', 'xby'])
})
