import { describe, expect, it } from 'vitest'

import { preprocessMarkdown } from '@/lib/markdown-preprocess'

describe('MATH_SPAN_SPLIT_RE backslash-run parity (#92371)', () => {
  it('shields inline math closed after an escaped backslash ($a\\\\$)', () => {
    // `\\` is an escaped backslash (valid TeX), so the final `$` closes the
    // span. A single-character lookbehind misread it as an escaped delimiter
    // and left the body unshielded — `\sqrt[3]` then lost its `[3]` to the
    // citation-marker rewrite.
    const out = preprocessMarkdown('$\\sqrt[3]{8}\\\\$')
    expect(out).toContain('\\sqrt[3]')
  })

  it('still treats a singly-escaped closing dollar as prose', () => {
    // `$x + \$5$`: the `\$` inside math is a literal dollar; the span ends at
    // the final unescaped `$`. And bare `\$5 and \$10` is never opened.
    expect(preprocessMarkdown('\\$5 and \\$10')).toBe('\\$5 and \\$10')
  })

  it('still shields display math across lines', () => {
    const src = '$$\nx = \\begin{cases}1\\\\2\\end{cases}\n$$\nsee [1]'
    const out = preprocessMarkdown(src)
    expect(out).toContain('\\begin{cases}')
  })
})
