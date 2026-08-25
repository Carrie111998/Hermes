import { readdirSync, readFileSync, statSync } from 'node:fs'
import { join, resolve } from 'node:path'

import { describe, expect, it } from 'vitest'

// Static-analysis guard: no DOM element in the desktop renderer may use the
// native HTML `title=` attribute as a tooltip. Native tooltips are unstyled,
// delayed (~500ms OS default), and visually inconsistent with the themed `Tip` —
// and crucially, they do NOT render in the Electron webview at all (see
// f08b1f344), so a `title=` on a chart bar, icon, or image shows nothing on
// hover. When a tip is warranted (see DESIGN.md — not every icon, never menu
// triggers), use `<Tip label={...}>` instead of `title=`.
//
// This is a source-text scan, not a behavior test — it's the same category as
// an ESLint rule, expressed as a vitest so it runs with the rest of the suite.
//
// Scans all of `src` (not just `src/components`): a native `title=` can live on
// any element. Surfaces under `src/app` (e.g. the Command Center daily-token
// bars, session rows) were previously invisible to this guard because it only
// walked `src/components`, and shipped with no tooltip at all.
//
// We match only lowercase DOM tags. Capitalized component props named `title`
// (e.g. <SectionHeading title={...}>, <Dialog title={...}>, and the
// `RowIconButton title` prop that forwards into <Tip>) are string props, not the
// HTML tooltip attribute, and are intentionally out of scope. `iframe title=` is
// a required accessibility attribute, not a hover tooltip, so it is skipped.

// Recursively walk a directory and collect all .tsx file paths.
function collectTsxFiles(dir: string): string[] {
  const results: string[] = []

  for (const entry of readdirSync(dir)) {
    // Skip node_modules, dist, and __tests__ (this file itself)
    if (entry === 'node_modules' || entry === 'dist' || entry === '__tests__') {
      continue
    }

    const fullPath = join(dir, entry)
    const stat = statSync(fullPath)

    if (stat.isDirectory()) {
      results.push(...collectTsxFiles(fullPath))
    } else if (entry.endsWith('.tsx')) {
      results.push(fullPath)
    }
  }

  return results
}

describe('no native title= tooltip attribute', () => {
  it('uses <Tip> instead of native title= on DOM elements', () => {
    const violations: string[] = []
    const srcDir = resolve(__dirname, '../../..')

    for (const filePath of collectTsxFiles(srcDir)) {
      const content = readFileSync(filePath, 'utf-8')
      const relativePath = filePath.replace(srcDir + '/', '')

      // Match lowercase DOM opening tags (may span multiple lines).
      const tagPattern = /<([a-z][a-z0-9]*)\b([^>]*?)>/gsu
      let match: RegExpExecArray | null

      while ((match = tagPattern.exec(content)) !== null) {
        const tagName = match[1]
        const attrs = match[2]

        // iframe title= is a required a11y attribute, not a hover tooltip.
        if (tagName === 'iframe') {
          continue
        }

        if (/\btitle=/.test(attrs)) {
          const lineNum = content.slice(0, match.index).split('\n').length
          violations.push(`${relativePath}:${lineNum} <${tagName}> has native title= — use <Tip>`)
        }
      }
    }

    expect(violations, violations.join('\n')).toEqual([])
  })
})
