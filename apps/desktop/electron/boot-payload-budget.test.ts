/**
 * Cold-boot payload budget.
 *
 * `index.html`'s `<script type="module">` + `<link rel="modulepreload">` set is
 * everything the renderer fetches and compiles BEFORE the first paint. It is
 * also the easiest thing in the app to regress by accident: one static import
 * from a boot-path module silently drags a multi-MB library onto the entry
 * graph, and nothing fails — the app just starts slower for everyone, forever.
 *
 * That has already happened and been fixed twice, each time by rerouting the
 * library behind a dynamic `import()`:
 *
 *   · shiki (19 MB, every grammar + theme) → `shiki-block.tsx` / `LazyShiki`
 *   · katex (253 KB, the slowest single chunk) → `lib/use-math-plugin.ts`
 *
 * Measured before the katex fix: 105 chunks / 5.97 MB. After: 104 / 5.72 MB.
 *
 * The two assertions below are deliberately different in kind:
 *
 *   · The size ceilings are a coarse ratchet. They exist to make an unexplained
 *     jump visible in review, not to police every kilobyte. Raising them is a
 *     legitimate outcome of a legitimate feature — do it with a note saying
 *     what grew.
 *   · The forbidden-library list is a hard invariant. Those chunks are large
 *     enough that reaching them from the entry graph is always a mistake, and
 *     the fix is always the same: route the importer through a dynamic
 *     `import()` the way the two modules above do. Do NOT relax this to make a
 *     build pass.
 *
 * Skipped when `dist/` has not been built (unit-test-only runs, fresh clones).
 * A build is what produces the thing under test; asserting against a missing
 * or stale one would just be noise.
 */

import fs from 'node:fs'
import path from 'node:path'

import { describe, expect, test } from 'vitest'

import { parseModuleAssetRefs } from './renderer-bundle'

const DIST = path.join(__dirname, '..', 'dist')
const INDEX = path.join(DIST, 'index.html')

// Headroom over the measured 5.72 MB / 104 chunks. Tight enough that adding a
// heavyweight dependency to the boot graph trips it; loose enough that ordinary
// feature work does not.
const MAX_BOOT_BYTES = 6.2 * 1024 * 1024
const MAX_BOOT_CHUNKS = 115

// Substrings matched against eager chunk filenames. Each names a library whose
// chunk is big enough that it must never be reachable statically from the entry.
const FORBIDDEN_IN_BOOT_GRAPH = ['shiki', 'katex', 'mermaid']

const built = fs.existsSync(INDEX)

function eagerChunks(): { name: string; bytes: number }[] {
  return parseModuleAssetRefs(fs.readFileSync(INDEX, 'utf8'))
    .filter(ref => /\.m?js$/i.test(ref))
    .map(ref => {
      let bytes = 0

      try {
        bytes = fs.statSync(path.join(DIST, ref)).size
      } catch {
        // A ref with no file is a torn generation — missingRendererAssets()
        // owns that failure mode. Counting it as 0 keeps this test focused on
        // payload size rather than duplicating that check.
      }

      return { name: path.basename(ref), bytes }
    })
}

describe.skipIf(!built)('cold-boot payload budget', () => {
  test('the eager module graph stays within budget', () => {
    const chunks = eagerChunks()
    const total = chunks.reduce((sum, c) => sum + c.bytes, 0)

    // Name the worst offenders in the failure message — a bare byte count
    // tells you a budget broke but not what to go look at.
    const worst = [...chunks]
      .sort((a, b) => b.bytes - a.bytes)
      .slice(0, 5)
      .map(c => `${c.name} (${(c.bytes / 1024).toFixed(0)} KB)`)
      .join(', ')

    expect(
      total,
      `boot payload is ${(total / 1048576).toFixed(2)} MB across ${chunks.length} chunks. Largest: ${worst}`
    ).toBeLessThanOrEqual(MAX_BOOT_BYTES)

    expect(chunks.length).toBeLessThanOrEqual(MAX_BOOT_CHUNKS)
  })

  test.each(FORBIDDEN_IN_BOOT_GRAPH)('%s is not on the boot graph', library => {
    const leaked = eagerChunks().filter(c => c.name.toLowerCase().includes(library))

    expect(
      leaked.map(c => c.name),
      `${library} reached the entry graph — some boot-path module now imports it statically. ` +
        `Route that importer through a dynamic import() (see lib/use-math-plugin.ts).`
    ).toEqual([])
  })
})
