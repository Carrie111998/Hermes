import assert from 'node:assert/strict'
import { execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'

import { test } from 'vitest'

const REPO_ROOT = path.resolve(__dirname, '..', '..', '..')
const ROOT_PKG = path.join(REPO_ROOT, 'package.json')
const ROOT_LOCK = path.join(REPO_ROOT, 'package-lock.json')
const DESKTOP_PKG = path.join(REPO_ROOT, 'apps', 'desktop', 'package.json')
const GET_WINDOWS_FORK = path.join(REPO_ROOT, 'vendor', 'get-windows')
const JS_TESTS_WORKFLOW = path.join(REPO_ROOT, '.github', 'workflows', 'js-tests.yml')
const E2E_DESKTOP_WORKFLOW = path.join(REPO_ROOT, '.github', 'workflows', 'e2e-desktop.yml')
const INSTALL_SCRIPT = path.join(REPO_ROOT, 'scripts', 'install.sh')
const NATIVE_DEPS_CHECK = path.join(REPO_ROOT, 'scripts', 'ci', 'verify-native-deps.mjs')

const ELECTRON_EXTRACTION_CHECK = path.join(
  REPO_ROOT,
  'scripts',
  'ci',
  'verify-electron-extraction.mjs'
)

test('get-windows build tools use a documented local compatibility fork', () => {
  const rootPkg = JSON.parse(fs.readFileSync(ROOT_PKG, 'utf-8'))
  const desktopPkg = JSON.parse(fs.readFileSync(DESKTOP_PKG, 'utf-8'))

  const forkPkg = JSON.parse(
    fs.readFileSync(path.join(GET_WINDOWS_FORK, 'package.json'), 'utf-8')
  )

  assert.equal(desktopPkg.dependencies?.['get-windows'], 'file:../../vendor/get-windows')
  assert.equal(rootPkg.overrides?.['get-windows'], undefined)
  assert.equal(forkPkg.name, 'get-windows')
  // Keep the upstream version so node-pre-gyp continues to resolve the
  // publisher's v9.3.0 prebuilt binaries instead of forcing local compilation.
  assert.equal(forkPkg.version, '9.3.0')
  assert.equal(forkPkg.hermesPatch, 'build-tool-compatibility-1')
  assert.equal(forkPkg.optionalDependencies?.['@mapbox/node-pre-gyp'], '2.0.3')
  assert.equal(forkPkg.optionalDependencies?.['node-gyp'], '11.5.0')
  assert.equal(forkPkg.peerDependencies?.['node-gyp'], '^11.5.0')

  const lock = JSON.parse(fs.readFileSync(ROOT_LOCK, 'utf-8'))
  const linkedFork = lock.packages?.['node_modules/get-windows']
  assert.equal(linkedFork?.resolved, 'vendor/get-windows')
  assert.equal(linkedFork?.link, true)
})

test('Windows CI exercises Electron extraction and get-windows source fallback', () => {
  const workflow = fs.readFileSync(JS_TESTS_WORKFLOW, 'utf-8')
  assert.match(workflow, /windows-native-install:/)
  assert.match(workflow, /runs-on: windows-latest/)
  assert.match(workflow, /node-version:\s*\[22\.22\.0,\s*26\]/)
  assert.match(workflow, /node-version:\s*\$\{\{ matrix\.node-version \}\}/)
  assert.match(workflow, /npm ci/)
  assert.match(workflow, /node scripts\/ci\/verify-electron-extraction\.mjs/)
  assert.match(workflow, /npm rebuild get-windows --build-from-source/)
  assert.match(workflow, /Verify Electron and published get-windows binaries/)
  assert.match(workflow, /Verify rebuilt get-windows binding/)
})

test('Electron extraction smoke test validates a fresh archive expansion', () => {
  const check = fs.readFileSync(ELECTRON_EXTRACTION_CHECK, 'utf-8')
  assert.match(check, /downloadArtifact/)
  assert.match(check, /extract\(archivePath/)
  assert.match(check, /Electron archive extracted files:/)
})

test('desktop installs use a per-run Electron artifact cache', () => {
  const installer = fs.readFileSync(INSTALL_SCRIPT, 'utf-8')
  const jsWorkflow = fs.readFileSync(JS_TESTS_WORKFLOW, 'utf-8')
  const e2eWorkflow = fs.readFileSync(E2E_DESKTOP_WORKFLOW, 'utf-8')

  assert.match(installer, /mktemp -d .*hermes-electron-cache/)
  assert.match(installer, /local electron_config_cache=/)

  const jobLevelRunnerCache = /^    env:\n      electron_config_cache:.*runner\.temp/gm
  const stepLevelRunnerCache = /^        env:\n          electron_config_cache:.*runner\.temp/gm

  assert.doesNotMatch(
    jsWorkflow,
    jobLevelRunnerCache,
    'runner context is unavailable in reusable-workflow job-level env'
  )
  assert.doesNotMatch(
    e2eWorkflow,
    jobLevelRunnerCache,
    'runner context is unavailable in reusable-workflow job-level env'
  )
  assert.equal([...jsWorkflow.matchAll(stepLevelRunnerCache)].length, 2)
  assert.equal([...e2eWorkflow.matchAll(stepLevelRunnerCache)].length, 1)
})

test('native dependency verifier exercises the installed platform', () => {
  const output = execFileSync(process.execPath, [NATIVE_DEPS_CHECK], {
    cwd: REPO_ROOT,
    encoding: 'utf-8'
  })

  assert.match(output, /Electron executable:/)
  assert.match(output, /Pure-JavaScript extractor:/)
  assert.match(output, /get-windows (?:binding|runtime):/)
})
