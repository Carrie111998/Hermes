/**
 * Invariants for what is eager vs lazy in the root ``package.json``.
 *
 * The root ``package.json`` is installed by ``hermes update`` on every user,
 * including users who never opted into a given browser backend. Anything
 * listed in ``dependencies`` therefore runs its npm postinstall script for
 * everyone, and — per #43564 — is also part of the npm workspace install
 * graph, where a workspace-scoped ``npm ci`` (``--workspace ui-tui
 * --workspace web``) can silently prune it right back out on the next
 * ``hermes update``.
 *
 * The contract:
 *
 * - ``agent-browser`` is NOT a root dependency. Keeping it out of the
 *   workspace graph avoids the #43564 pruning failure, but it is no longer
 *   npx-only: the Hermes installer and dependency reconciler install it in
 *   the managed Node prefix. Browser tools retain npx as a last-resort
 *   runtime fallback, and ``hermes doctor`` reports that degraded state.
 *
 * - ``@streamdown/math`` is NOT a root dependency either. It's imported only
 *   by desktop's own TS code (``apps/desktop/src/...``), so it belongs in
 *   ``apps/desktop/package.json`` (alongside its sibling ``@streamdown/code``)
 *   — not root, where it was subject to the exact same pruning risk.
 *
 * - ``@askjo/camofox-browser`` is NOT eager. It is an explicit opt-in
 *   alternative browser backend, selected by the user via
 *   ``hermes tools`` → Browser Automation → Camofox, and only used at
 *   runtime when ``CAMOFOX_URL`` is set. Its postinstall fetches a ~300MB
 *   Firefox-fork binary, which silently blocked ``hermes update`` for
 *   multi-minute stretches on slow / network-restricted connections
 *   (notably users in China running through a VPN). The package is
 *   installed on demand by ``tools_config.py`` ``post_setup_key ==
 *   "camofox"`` when the user actually selects Camofox.
 *
 * If a future PR re-adds any of these to root ``dependencies``, this test
 * fails — read the lazy-install guidance in the ``hermes-agent-dev`` skill
 * before changing the expectations.
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

import { test } from 'vitest'

const REPO_ROOT = path.resolve(__dirname, '..')
const ROOT_PKG = path.join(REPO_ROOT, 'package.json')
const ROOT_LOCK = path.join(REPO_ROOT, 'package-lock.json')
const DESKTOP_PKG = path.join(REPO_ROOT, 'apps', 'desktop', 'package.json')

function rootPackageJson(): Record<string, unknown> {
  return JSON.parse(fs.readFileSync(ROOT_PKG, 'utf-8'))
}

test('camofox is not in root dependencies (must stay opt-in)', () => {
  const deps = (rootPackageJson().dependencies ?? {}) as Record<string, string>
  assert.ok(
    !('@askjo/camofox-browser' in deps),
    'Camofox is a ~300MB binary-postinstall backend that must stay ' +
      'out of root package.json dependencies. It belongs in the ' +
      'Camofox post_setup handler in hermes_cli/tools_config.py so it ' +
      'only installs when the user explicitly selects Camofox via ' +
      '`hermes tools` → Browser Automation → Camofox.'
  )
})

test('agent-browser stays out of root dependencies (managed prefix, #43564)', () => {
  const deps = (rootPackageJson().dependencies ?? {}) as Record<string, string>
  assert.ok(
    !('agent-browser' in deps),
    'agent-browser must stay out of the root package.json workspace graph. ' +
      'Hermes installs it separately in the managed Node prefix; browser ' +
      'tools retain npx only as a last-resort fallback.'
  )
})

test('@streamdown/math is not in root dependencies (desktop-only import)', () => {
  const deps = (rootPackageJson().dependencies ?? {}) as Record<string, string>
  assert.ok(
    !('@streamdown/math' in deps),
    '@streamdown/math is only imported by apps/desktop\'s own TS code ' +
      '(markdown-text.tsx, katex-memo.ts) — it belongs in ' +
      'apps/desktop/package.json alongside its sibling @streamdown/code, ' +
      'not root, where it\'s subject to the same workspace-pruning risk ' +
      'agent-browser had (#43564).'
  )
})

test('@streamdown/math is in desktop dependencies', () => {
  const deps = (JSON.parse(fs.readFileSync(DESKTOP_PKG, 'utf-8')).dependencies ??
    {}) as Record<string, string>

  assert.ok(
    '@streamdown/math' in deps,
    '@streamdown/math is imported by apps/desktop\'s own TS code ' +
      '(markdown-text.tsx, katex-memo.ts) and must be declared in ' +
      'apps/desktop/package.json now that it is no longer a root ' +
      'dependency.'
  )
})

test('root lockfile has no camofox entries', () => {
  if (!fs.existsSync(ROOT_LOCK)) {
    // Some CI matrix shards skip lockfile materialization.
    return
  }

  const text = fs.readFileSync(ROOT_LOCK, 'utf-8')
  assert.ok(
    !text.includes('@askjo/camofox-browser'),
    'package-lock.json still references @askjo/camofox-browser. ' +
      'Regenerate the lockfile after removing the dep: ' +
      '`rm package-lock.json && npm install --package-lock-only ' +
      '--ignore-scripts --no-fund --no-audit`.'
  )
  assert.ok(
    !text.includes('camoufox-js'),
    'package-lock.json still references camoufox-js (transitive of ' +
      '@askjo/camofox-browser). Regenerate the lockfile.'
  )
})

test('root lockfile has no agent-browser entry (#43564)', () => {
  if (!fs.existsSync(ROOT_LOCK)) {
    // Some CI matrix shards skip lockfile materialization.
    return
  }

  const text = fs.readFileSync(ROOT_LOCK, 'utf-8')
  assert.ok(
    !text.includes('"node_modules/agent-browser"'),
    'package-lock.json must keep agent-browser out of the workspace graph. ' +
      'The durable install lives in the Hermes managed Node prefix; ' +
      'regenerate the lockfile only if the root package graph changes.'
  )
})
