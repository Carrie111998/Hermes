import { describe, expect, it, vi } from 'vitest'

import { collectBalancedFenceRanges, fenceRawSvgBlocks } from './markdown-inline-svg'

const SVG = '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'

describe('fenceRawSvgBlocks', () => {
  it('lifts a bare SVG out of surrounding prose', () => {
    expect(fenceRawSvgBlocks(`Before ${SVG} after`)).toBe(`Before \n\n\`\`\`svg\n${SVG}\n\`\`\`\n\n after`)
  })

  it('balances nested SVG tags and ignores tag-like text in comments and quoted attributes', () => {
    const nested = `<svg aria-label="> not the end">
    <!-- <svg></svg> -->
    <svg viewBox="0 0 1 1"><path d="M0 0"/></svg>
</svg>`

    const output = fenceRawSvgBlocks(nested)

    expect(output).toContain(`\`\`\`svg\n${nested}\n\`\`\``)
  })

  it('keeps the no-SVG streaming path out of the code-range and markup scanners', () => {
    const chunks = Array.from({ length: 80 }, (_, index) => `Streaming prose ${index}\n`.repeat(index + 1))
    const chunkSet = new Set(chunks)
    const originalIndexOf = String.prototype.indexOf
    let structuralSearches = 0

    const indexOfSpy = vi.spyOn(String.prototype, 'indexOf').mockImplementation(function (
      this: string,
      search: string,
      position?: number
    ) {
      if (chunkSet.has(this.valueOf()) && (search === '\n' || search === '<')) {
        structuralSearches += 1
      }

      return originalIndexOf.call(this, search, position)
    })

    try {
      for (const chunk of chunks) {
        expect(fenceRawSvgBlocks(chunk)).toBe(chunk)
      }
    } finally {
      indexOfSpy.mockRestore()
    }

    expect(structuralSearches).toBe(0)
  })

  it('keeps case-insensitive SVG candidates on the full scanner path', () => {
    const uppercaseSvg = '<SVG viewBox="0 0 1 1"><PATH d="M0 0h1"/></SVG>'

    expect(fenceRawSvgBlocks(uppercaseSvg)).toBe(`\`\`\`svg\n${uppercaseSvg}\n\`\`\``)
  })

  it('requires matching blockquote depth while allowing equivalent prefix spacing', () => {
    const matching = ['> > ```html', '> > code', '>  >    ````'].join('\n')
    const mismatchedThenMatching = ['> > ```html', '> > code', '> ```', '> > ```'].join('\n')
    const mismatchedOnly = ['> > ```html', '> > code', '> ```'].join('\n')

    expect(collectBalancedFenceRanges(matching)).toEqual([{ end: matching.length, start: 0 }])
    expect(collectBalancedFenceRanges(mismatchedThenMatching)).toEqual([
      { end: mismatchedThenMatching.length, start: 0 }
    ])
    expect(collectBalancedFenceRanges(mismatchedOnly)).toEqual([])
  })

  it('requires matching list indentation and blockquote containers around a fence', () => {
    const matching = ['> - 1. ~~~~html', '>      code', '>      ~~~~'].join('\n')
    const mismatchedThenMatching = ['> - ~~~html', '>   code', '> ~~~', '>   ~~~'].join('\n')
    const mismatchedOnly = ['> - ~~~html', '>   code', '> ~~~'].join('\n')

    expect(collectBalancedFenceRanges(matching)).toEqual([{ end: matching.length, start: 0 }])
    expect(collectBalancedFenceRanges(mismatchedThenMatching)).toEqual([
      { end: mismatchedThenMatching.length, start: 0 }
    ])
    expect(collectBalancedFenceRanges(mismatchedOnly)).toEqual([])
  })

  it.each(['    ', '\t'])('keeps growing %s-indented SVG code inert while streaming', indent => {
    const chunks = [
      `${indent}<svg viewBox="0 0 1 1">`,
      `${indent}<svg viewBox="0 0 1 1"><path d="M0 0h1"/>`,
      `${indent}<svg viewBox="0 0 1 1"><path d="M0 0h1"/></svg>`
    ]

    for (const chunk of chunks) {
      expect(fenceRawSvgBlocks(chunk)).toBe(chunk)
    }
  })

  it.each([
    ['fenced', `\`\`\`html\n${SVG}\n\`\`\``],
    ['blockquoted fenced', `> \`\`\`html\n> ${SVG}\n> \`\`\``],
    ['nested-blockquoted fenced', `> > ~~~~html\n> > ${SVG}\n> > ~~~~`],
    ['list-nested fenced', `- ~~~html\n  ${SVG}\n  ~~~`],
    ['list-nested unclosed fenced', `- ~~~html\n  ${SVG}`],
    ['nested-list blockquoted fenced', `> - 1. ~~~~html\n>      ${SVG}\n>      ~~~~`],
    ['list-nested blockquote fenced', `- 1. > ~~~~html\n     > ${SVG}\n     > ~~~~`],
    ['inline', `\`${SVG}\``],
    ['indented', `    ${SVG}`],
    ['tab-indented', `\t${SVG}`],
    ['blockquoted indented', `>     ${SVG}`]
  ])('does not lift SVG from %s code', (_label, input) => {
    expect(fenceRawSvgBlocks(input)).toBe(input)
  })

  it.each([
    ['missing root close', '<svg><circle/></g>'],
    ['malformed root close', '<svg><circle/></svg trailing>'],
    ['self-closing root', '<svg viewBox="0 0 1 1"/>'],
    ['escaped root', `\\${SVG}`]
  ])('leaves %s inert', (_label, input) => {
    expect(fenceRawSvgBlocks(input)).toBe(input)
  })

  it.each([
    ['comment', `<!-- ${SVG}`],
    ['CDATA section', `<![CDATA[ ${SVG}`],
    ['processing instruction', `<?render ${SVG}`],
    ['quoted tag', `<div data-markup="${SVG}`]
  ])('treats an unterminated %s as terminal instead of scanning nested SVG', (_label, input) => {
    expect(fenceRawSvgBlocks(input)).toBe(input)
  })

  it('does not borrow a closing tag from a later code fence', () => {
    const input = '<svg><circle/>\n\n```text\n</svg>\n```'

    expect(fenceRawSvgBlocks(input)).toBe(input)
  })

  it('does not borrow a closing tag from a later indented code block', () => {
    const input = '<svg><circle/>\n\n    </svg>'

    expect(fenceRawSvgBlocks(input)).toBe(input)
  })

  it('uses a tilde fence when SVG text contains triple backticks', () => {
    const svg = '<svg><text>```</text></svg>'

    expect(fenceRawSvgBlocks(svg)).toBe(`~~~svg\n${svg}\n~~~`)
  })

  it('fails closed when neither available fence marker can contain the SVG', () => {
    const svg = '<svg><text>``` and ~~~</text></svg>'

    expect(fenceRawSvgBlocks(svg)).toBe(svg)
  })

  it('scans repeated unclosed SVG candidates within a linear operation budget', () => {
    const candidateCount = 400
    const input = Array.from({ length: candidateCount }, (_, index) => `<svg id="open-${index}">`).join('\n')
    const originalIndexOf = String.prototype.indexOf
    let tagSearches = 0

    const indexOfSpy = vi.spyOn(String.prototype, 'indexOf').mockImplementation(function (
      this: string,
      search: string,
      position?: number
    ) {
      if (this.valueOf() === input && search === '<') {
        tagSearches += 1
      }

      return originalIndexOf.call(this, search, position)
    })

    try {
      expect(fenceRawSvgBlocks(input)).toBe(input)
    } finally {
      indexOfSpy.mockRestore()
    }

    expect(tagSearches).toBeLessThanOrEqual(candidateCount * 3)
  })

  it('stops after the first unterminated construct instead of rescanning every suffix', () => {
    const candidateCount = 400
    const input = `${'<!--'.repeat(candidateCount)}${SVG}`
    const originalIndexOf = String.prototype.indexOf
    let structuralSearches = 0

    const indexOfSpy = vi.spyOn(String.prototype, 'indexOf').mockImplementation(function (
      this: string,
      search: string,
      position?: number
    ) {
      if (this.valueOf() === input && (search === '<' || search === '-->')) {
        structuralSearches += 1
      }

      return originalIndexOf.call(this, search, position)
    })

    try {
      expect(fenceRawSvgBlocks(input)).toBe(input)
    } finally {
      indexOfSpy.mockRestore()
    }

    expect(structuralSearches).toBeLessThanOrEqual(2)
  })
})
