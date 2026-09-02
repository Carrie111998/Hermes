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
 * DIRTY-TREE REFUSAL: a manifest written over uncommitted source would
 * attest a clean HEAD while packaging different bytes. Run
 * `git status --porcelain` first and refuse on any output — the release
 * gate then fails with "no build manifest", which is the honest outcome.
 * EXCEPTION: the bundle directory itself (target/) is gitignored, so its
 * contents never count as dirt; status is run with --untracked-files=no to
 * avoid untracked build outputs blocking a legit clean build.
 *
 * IMPORTANT ordering (the #100600 review's second blocker): the version-bump
 * commit in release.py creates a NEW HEAD, so a manifest built before the
 * bump commit can never match the release HEAD. Build the DMG at the FINAL
 * HEAD. On the macOS release host:
 *
 *   1. python scripts/release.py --bump minor --prepare-only
 *        (commits the bump AND the full set of version files; no tag/push)
 *   2. cd apps/bootstrap-installer && npm run tauri:build
 *        (manifest written at this exact HEAD; refuses on a dirty tree)
 *   3. python scripts/release.py --publish --no-bump --date <same calver>
 *        (gate validates against the prepare HEAD, then tags/pushes/releases)
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
let dirty = ''
try {
  sha = execFileSync('git', ['rev-parse', 'HEAD'], { cwd: repoRoot, encoding: 'utf-8' }).trim()
  dirty = execFileSync('git', ['status', '--porcelain', '--untracked-files=no'],
    { cwd: repoRoot, encoding: 'utf-8' }).trim()
} catch {
  console.error('[build-manifest] git unavailable — no manifest written; release gate will refuse (correct: provenance unknown)')
  process.exit(0)
}

if (dirty) {
  const lines = dirty.split('\n').slice(0, 5).join('\n  ')
  console.error(
    '[build-manifest] REFUSING to write the manifest: the working tree is dirty.\n' +
    '  A manifest over uncommitted source would attest a clean HEAD while\n' +
    '  packaging different bytes. Commit or stash first, then rerun\n' +
    '  `npm run tauri:build`. Dirty entries:\n  ' + lines
  )
  process.exit(1)
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
