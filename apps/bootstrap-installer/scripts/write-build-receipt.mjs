/**
 * Write the DMG build receipt for the release gate (#85422).
 *
 * Runs automatically after `npm run tauri:build` (see package.json) and
 * records the exact source tree the DMG was built from:
 *
 *   bundle/dmg/.build-receipt.json
 *     { "sha": "<git HEAD sha>", "built_at_unix": <epoch seconds> }
 *
 * scripts/release.py --publish refuses to attach a DMG whose receipt SHA
 * does not match the release SHA (a copied DMG from another tag/branch
 * cannot masquerade as this build) and blocks zero-asset releases unless
 * explicitly bypassed with --allow-no-mac-asset.
 *
 * Best-effort by design: if git is unavailable the receipt is skipped and
 * the release gate will report "no build receipt" — which is the correct
 * outcome, since provenance genuinely could not be established.
 */
import { execFileSync } from 'node:child_process'
import { existsSync, mkdirSync, writeFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const bundleDir = resolve(here, '../src-tauri/target/release/bundle/dmg')

if (!existsSync(bundleDir)) {
  console.error('[build-receipt] bundle/dmg not found — tauri build did not emit a DMG; skipping receipt')
  process.exit(0)
}

let sha = ''
try {
  sha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: resolve(here, '../..'), encoding: 'utf-8' }).trim()
} catch {
  console.error('[build-receipt] git unavailable — no receipt written; release gate will refuse (correct: provenance unknown)')
  process.exit(0)
}

const receipt = { sha, built_at_unix: Math.floor(Date.now() / 1000) }
const out = resolve(bundleDir, '.build-receipt.json')
writeFileSync(out, JSON.stringify(receipt, null, 2) + '\n', 'utf-8')
console.log(`[build-receipt] wrote ${out} (sha ${sha.slice(0, 10)})`)
