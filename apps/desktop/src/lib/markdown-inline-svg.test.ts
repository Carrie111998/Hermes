import { fromMarkdown } from 'mdast-util-from-markdown'
import { describe, expect, it, vi } from 'vitest'

import { collectBalancedFenceRanges, fenceRawSvgBlocks } from './markdown-inline-svg'

const SVG = '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'

interface MdastNode {
  children?: MdastNode[]
  type?: string
  value?: string
}

function valuesOfType(node: MdastNode, type: string): string[] {
  const values = node.type === type && node.value ? [node.value] : []

  for (const child of node.children || []) {
    values.push(...valuesOfType(child, type))
  }

  return values
}

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
      { end: mismatchedThenMatching.indexOf('\n> ```') + 1, start: 0 }
    ])
    expect(collectBalancedFenceRanges(mismatchedOnly)).toEqual([
      { end: mismatchedOnly.indexOf('\n> ```') + 1, start: 0 }
    ])
  })

  it('requires matching list indentation and blockquote containers around a fence', () => {
    const matching = ['> - 1. ~~~~html', '>      code', '>      ~~~~'].join('\n')
    const mismatchedThenMatching = ['> - ~~~html', '>   code', '> ~~~', '>   ~~~'].join('\n')
    const mismatchedOnly = ['> - ~~~html', '>   code', '> ~~~'].join('\n')

    expect(collectBalancedFenceRanges(matching)).toEqual([{ end: matching.length, start: 0 }])
    expect(collectBalancedFenceRanges(mismatchedThenMatching)).toEqual([
      { end: mismatchedThenMatching.indexOf('> ~~~'), start: 0 },
      { end: mismatchedThenMatching.length, start: mismatchedThenMatching.indexOf('> ~~~') }
    ])
    expect(collectBalancedFenceRanges(mismatchedOnly)).toEqual([{ end: mismatchedOnly.indexOf('> ~~~'), start: 0 }])
  })

  it('ends a container fence when its prefix disappears and reprocesses the boundary line', () => {
    const input = ['> ```html', '> quoted code', 'root prose', `> > ${SVG}`].join('\n')
    const output = fenceRawSvgBlocks(input)
    const tree = fromMarkdown(output)

    expect(collectBalancedFenceRanges(input)).toEqual([{ end: input.indexOf('root prose'), start: 0 }])
    expect(tree.children).toMatchObject([
      { children: [{ lang: 'html', type: 'code', value: 'quoted code' }], type: 'blockquote' },
      { children: [{ type: 'text', value: 'root prose' }], type: 'paragraph' },
      {
        children: [
          {
            children: [{ lang: 'svg', type: 'code', value: SVG }],
            type: 'blockquote'
          }
        ],
        type: 'blockquote'
      }
    ])
  })

  it.each([
    {
      expected: {
        children: [
          {
            children: [
              { children: [{ type: 'text', value: 'outer' }], type: 'paragraph' },
              { children: [{ lang: 'svg', type: 'code', value: SVG }], type: 'blockquote' }
            ],
            type: 'listItem'
          }
        ],
        type: 'list'
      },
      generated: `  > \`\`\`svg\n  > ${SVG}\n  > \`\`\``,
      input: ['- outer', `  > ${SVG}`].join('\n'),
      label: 'list continuation followed by a blockquote'
    },
    {
      expected: {
        children: [
          {
            children: [
              {
                children: [
                  {
                    children: [{ lang: 'svg', type: 'code', value: SVG }],
                    type: 'listItem'
                  }
                ],
                type: 'list'
              }
            ],
            type: 'blockquote'
          }
        ],
        type: 'blockquote'
      },
      generated: `> > 1. \`\`\`svg\n> >    ${SVG}\n> >    \`\`\``,
      input: `> > 1. ${SVG}`,
      label: 'nested blockquotes followed by an ordered list'
    }
  ])('keeps generated SVG fences inside the active containers for $label', ({ expected, generated, input }) => {
    const output = fenceRawSvgBlocks(input)

    expect(fromMarkdown(output).children).toMatchObject([expected])
    expect(output).toContain(generated)
  })

  it('does not balance an SVG across a terminated container', () => {
    const input = ['> <svg viewBox="0 0 1 1">', '<path d="M0 0h1"/>', '> </svg>'].join('\n')

    expect(fenceRawSvgBlocks(input)).toBe(input)
  })

  it('retains surviving outer containers when an inner quote fence terminates', () => {
    const input = ['- > ```html', '  > code', '', `  > ${SVG}`].join('\n')
    const output = fenceRawSvgBlocks(input)

    expect(fromMarkdown(output).children).toMatchObject([
      {
        children: [
          {
            children: [
              { children: [{ lang: 'html', type: 'code', value: 'code' }], type: 'blockquote' },
              { children: [{ lang: 'svg', type: 'code', value: SVG }], type: 'blockquote' }
            ],
            type: 'listItem'
          }
        ],
        type: 'list'
      }
    ])
  })

  it('allows an unindented blank line to continue a list-contained fence', () => {
    const input = ['- ```html', '', `  ${SVG}`, '  ```'].join('\n')

    expect(collectBalancedFenceRanges(input)).toEqual([{ end: input.length, start: 0 }])
    expect(fenceRawSvgBlocks(input)).toBe(input)
  })

  it.each([
    ['list continuation', `- item\n    ~~~~html\n  ${SVG}\n  ~~~~`],
    ['nested list continuation', `- outer\n  - inner\n      ~~~~html\n    ${SVG}\n    ~~~~`],
    ['blockquoted nested list continuation', `> - outer\n>   - inner\n>       ~~~~html\n>     ${SVG}\n>     ~~~~`]
  ])('keeps SVG in a fence opened from a %s inert', (_label, input) => {
    expect(collectBalancedFenceRanges(input)).toEqual([
      { end: input.length, start: input.lastIndexOf('\n', input.indexOf('~~~~html')) + 1 }
    ])
    expect(fenceRawSvgBlocks(input)).toBe(input)
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

  it('pairs inline-code delimiters only inside the CommonMark block that owns them', () => {
    const input = ['`dangling opener', '', `\`${SVG}\``].join('\n')
    const output = fenceRawSvgBlocks(input)
    const tree = fromMarkdown(output)

    expect(output).toBe(input)
    expect(tree.children).toMatchObject([
      { children: [{ type: 'text', value: '`dangling opener' }], type: 'paragraph' },
      { children: [{ type: 'inlineCode', value: SVG }], type: 'paragraph' }
    ])
  })

  it.each([
    ['ATX heading', ['`dangling opener', '# heading', `\`${SVG}\``].join('\n')],
    ['blockquote', ['`dangling opener', `> \`${SVG}\``].join('\n')],
    ['sibling list item', ['- `dangling opener', `- \`${SVG}\``].join('\n')]
  ])('does not pair backticks across an interrupting %s boundary', (_label, input) => {
    const expectedTree = fromMarkdown(input)

    expect(valuesOfType(expectedTree, 'inlineCode')).toContain(SVG)
    expect(fenceRawSvgBlocks(input)).toBe(input)
  })

  it('keeps a valid multiline inline-code span protected', () => {
    const input = `\`before\n${SVG}\nafter\``

    expect(valuesOfType(fromMarkdown(input), 'inlineCode')).toEqual([`before\n${SVG}\nafter`])
    expect(fenceRawSvgBlocks(input)).toBe(input)
  })

  it.each([
    ['four-column unordered-list continuation', ['- item', `    ${SVG}`].join('\n')],
    ['five-column ordered-list continuation', ['123. item', `     ${SVG}`].join('\n')]
  ])('subtracts active container indentation for a %s', (_label, input) => {
    const output = fenceRawSvgBlocks(input)

    expect(output).toContain(`\`\`\`svg\n`)
    expect(fromMarkdown(output).children).toMatchObject([
      {
        children: [
          {
            children: [
              { children: [{ type: 'text', value: 'item' }], type: 'paragraph' },
              { lang: 'svg', type: 'code', value: SVG }
            ],
            type: 'listItem'
          }
        ],
        type: 'list'
      }
    ])
  })

  it('reprocesses a root SVG after an unterminated quoted SVG container ends', () => {
    const rootSvg = SVG.replace('<svg ', '<svg id="root-after-quote" ')
    const input = ['> <svg id="unterminated-quote"><path/>', '', rootSvg].join('\n')
    const output = fenceRawSvgBlocks(input)

    expect(output).toContain(`\`\`\`svg\n${rootSvg}\n\`\`\``)
    expect(fromMarkdown(output).children).toMatchObject([
      { type: 'blockquote' },
      { lang: 'svg', type: 'code', value: rootSvg }
    ])
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

  it('indexes increasing unmatched backtick runs within a linear operation budget', () => {
    const runCount = 200
    const backticks = Array.from({ length: runCount }, (_, index) => '`'.repeat(index + 1)).join('x')
    const input = `${backticks}\n${SVG}`
    const originalIndexOf = String.prototype.indexOf
    let backtickSearches = 0

    const indexOfSpy = vi.spyOn(String.prototype, 'indexOf').mockImplementation(function (
      this: string,
      search: string,
      position?: number
    ) {
      if (this.valueOf() === input && search === '`') {
        backtickSearches += 1
      }

      return originalIndexOf.call(this, search, position)
    })

    try {
      expect(fenceRawSvgBlocks(input)).toContain(`\`\`\`svg\n${SVG}\n\`\`\``)
    } finally {
      indexOfSpy.mockRestore()
    }

    expect(backtickSearches).toBeLessThanOrEqual(runCount)
  })

  it('parses a decreasing malformed container prefix within a linear copy budget', () => {
    const depth = 300
    const prefix = '> '.repeat(depth)
    const input = [`${prefix}paragraph`, SVG].join('\n')
    const originalSlice = Array.prototype.slice
    let copiedContainerSlots = 0

    const sliceSpy = vi.spyOn(Array.prototype, 'slice').mockImplementation(function (
      this: unknown[],
      start?: number,
      end?: number
    ) {
      const result = originalSlice.call(this, start, end)
      const first = this[0] as { kind?: unknown } | undefined

      if (first?.kind === 'blockquote' || first?.kind === 'list') {
        copiedContainerSlots += result.length
      }

      return result
    })

    try {
      expect(fenceRawSvgBlocks(input)).toContain(`\`\`\`svg\n${SVG}\n\`\`\``)
    } finally {
      sliceSpy.mockRestore()
    }

    expect(copiedContainerSlots).toBeLessThanOrEqual(depth * 8)
  })

  it('handles a large malformed nested container before a valid root SVG', () => {
    const prefix = '> '.repeat(4_000)
    const rootSvg = SVG.replace('<svg ', '<svg id="after-large-container" ')
    const input = [`${prefix}<svg id="unterminated-nested">`, '', rootSvg].join('\n')

    expect(fenceRawSvgBlocks(input)).toContain(`\`\`\`svg\n${rootSvg}\n\`\`\``)
  })
})
