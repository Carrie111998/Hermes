/**
 * Write the per-artifact DMG build manifest for the release gate (#85422).
 *
 * Runs automatically after `npm run tauri:build` (see package.json) and
 * records, in bundle/dmg/.build-manifest.json:
 *
 *   {
 *     "sha": "<git HEAD sha at build time>",
 *     "built_at_unix": <epoch seconds>,
 *     "artifacts": [
 *       { "filename": "...", "sha256": "...", "size": N, "arch": "..." }
 *     ]
 *   }
 *
 * scripts/release.py recomputes each DMG's SHA-256 at release time and only
 * attaches artifacts whose bytes match the recorded inventory — arbitrary
 * bytes with an aligned mtime cannot masquerade as the build.
 *
 * IMPORTANT ordering (the #100600 review's second blocker): the version-bump
 * commit in release.py creates a NEW HEAD, so a manifest built before
 * `release.py --publish` can never match. Build the DMG at the FINAL HEAD:
 * commit the bump first, then build, then publish. On the macOS release host:
 *
 *   1. python scripts/release.py --bump minor   (commits the bump; no tag)
 *   2. cd apps/bootstrap-installer && npm run tauri:build
 *   3. python scripts/release.py --publish --no-bump --date <same calver>
 *
 * Best-effort by design: if git or the bundle dir is unavailable the
 * manifest is skipped and the release gate reports "no build manifest" —
 * the correct outcome, since provenance genuinely could not be established.
 */
import { execFileSync } from 'node:child_process'
import { createHash } from 'node:crypto'
import { existsSync, readdirSync, readFileSync, statSync, writeFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const here = dirname(fileURLToPath(import.meta.url))
const repoRoot = resolve(here, '../../..')
const bundleDir = resolve(here, '../src-tauri/target/release/bundle/dmg')

if (!existsSync(bundleDir)) {
  console.error('[build-manifest] bundle/dmg not found — tauri build did not emit a DMG; skipping manifest')
  process.exit(0)
}

let sha = ''
try {
  sha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoRoot, encoding: 'utf-8' }).trim()
} catch {
  console.error('[build-manifest] git unavailable — no manifest written; release gate will refuse (correct: provenance unknown)')
  process.exit(0)
}

function sha256Of(file) {
  const h = createHash('sha256')
  h.update(readFileSync(file))
  return h.digest('hex')
}

const artifacts = []
for (const name of readdirSync(bundleDir).sort()) {
  if (!name.endsWith('.dmg')) continue
  const full = resolve(bundleDir, name)
  artifacts.push({
    filename: name,
    sha256: sha256Of(full),
    size: statSync(full).size,
    arch: name.includes('aarch64') || name.includes('arm64') ? 'aarch64' : name.includes('x64') || name.includes('x86_64') ? 'x64' : 'unknown',
  })
}
if (artifacts.length === 0) {
  console.error('[build-manifest] no .dmg artifacts in bundle dir; skipping manifest')
  process.exit(0)
}

const manifest = { sha, built_at_unix: Math.floor(Date.now() / 1000), artifacts }
const out = resolve(bundleDir, '.build-manifest.json')
writeFileSync(out, JSON.stringify(manifest, null, 2) + '\n', 'utf-8')
console.log(`[build-manifest] wrote ${out} (sha ${sha.slice(0, 10)}, ${artifacts.length} artifact(s))`)
