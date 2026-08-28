/**
 * The thresholds the DESKTOP PLATFORM HINT promises the model.
 *
 * `PLATFORM_HINTS['desktop']` in agent/prompt_builder.py tells the model that
 * mermaid always draws in place, that a small svg renders inline while a large
 * standalone graphic opens as an artifact card, and that html documents and
 * long code fences promote too. The model acts on those sentences, so changing
 * the numbers here silently makes the prompt lie — it will inline something
 * that promotes, or truncate something that would have been fine.
 *
 * The Python side of this contract is guarded by
 * tests/agent/test_desktop_capabilities_are_advertised.py, which fails when a
 * capability exists but is not named in the hint. This is the other half: the
 * capability behaving the way the hint describes.
 *
 * Content is Arabic on purpose — title extraction and the length thresholds
 * are character-based, and RTL text is where a naive implementation trips.
 */
import { describe, expect, it } from 'vitest'

import { detectArtifact } from '@/lib/artifact-detect'

const ar = 'مخطط عربي'

const svgOf = (pad: number) =>
  `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><title>${ar}</title>` +
  `<text x="1" y="5">${ar}</text>${'<!-- x -->'.repeat(pad)}</svg>`

describe('artifact promotion — the contract the desktop hint states', () => {
  it('keeps a small svg inline and promotes a large one', () => {
    expect(detectArtifact('svg', svgOf(1))).toBeNull()

    const big = svgOf(400)

    expect(big.length).toBeGreaterThanOrEqual(2000)
    expect(detectArtifact('svg', big)?.kind).toBe('svg')
  })

  it('never promotes mermaid, however long', () => {
    // The hint promises mermaid ALWAYS draws in place. Artifact detection runs
    // before the rich-fence router, so promotion here breaks that promise.
    const huge =
      'flowchart TD\n' +
      Array.from({ length: 400 }, (_, index) => `  A${index}[عقدة ${index}] --> A${index + 1}`).join('\n')

    expect(huge.length).toBeGreaterThan(3000)
    expect(detectArtifact('mermaid', huge)).toBeNull()
  })

  it('promotes an html document and a long code fence', () => {
    const doc = `<!doctype html><html><head><title>${ar}</title></head><body>${'<p>ص</p>'.repeat(40)}</body></html>`

    expect(detectArtifact('html', doc)?.kind).toBe('html')
    expect(
      detectArtifact('python', Array.from({ length: 60 }, (_, index) => `x = ${index}  # سطر`).join('\n'))?.kind
    ).toBe('code')
  })

  it('takes the arabic <title> as the artifact title', () => {
    expect(detectArtifact('svg', svgOf(400))?.title).toBe(ar)
  })
})
