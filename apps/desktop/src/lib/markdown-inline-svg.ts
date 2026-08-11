interface Range {
  end: number
  kind: 'fence' | 'indented' | 'inline'
  start: number
}

interface FenceMarker {
  character: '`' | '~'
  containers: FenceContainer[]
  length: number
}

type FenceContainer = { kind: 'blockquote' } | { indent: number; kind: 'list' }

interface MarkupToken {
  end: number
  kind: 'svg-close' | 'svg-open' | 'other'
  selfClosing: boolean
}

const TERMINAL_MARKUP = 'terminal-markup'

function lineEnd(text: string, start: number): number {
  const end = text.indexOf('\n', start)

  return end === -1 ? text.length : end
}

interface ContainerLine {
  blockquoteDepth: number
  content: string
}

function stripBlockquotePrefix(line: string): ContainerLine {
  let blockquoteDepth = 0
  let cursor = 0

  while (cursor < line.length) {
    const match = /^ {0,3}>[ \t]?/.exec(line.slice(cursor))

    if (!match) {
      break
    }

    blockquoteDepth += 1
    cursor += match[0].length
  }

  return { blockquoteDepth, content: line.slice(cursor) }
}

function nextColumn(column: number, character: string): number {
  return character === '\t' ? column + (4 - (column % 4)) : column + 1
}

function consumeIndent(
  line: string,
  start: number,
  column: number,
  maximum: number
): { columns: number; cursor: number } {
  let columns = 0
  let cursor = start

  while (line[cursor] === ' ' || line[cursor] === '\t') {
    const next = nextColumn(column + columns, line[cursor] || '')
    const width = next - (column + columns)

    if (columns + width > maximum) {
      break
    }

    columns += width
    cursor += 1
  }

  return { columns, cursor }
}

function listMarkerEnd(line: string, start: number): number {
  const marker = line[start]

  if (marker === '*' || marker === '+' || marker === '-') {
    return start + 1
  }

  let cursor = start

  while (cursor < start + 9 && /\d/.test(line[cursor] || '')) {
    cursor += 1
  }

  return cursor > start && (line[cursor] === '.' || line[cursor] === ')') ? cursor + 1 : start
}

// Fences can open after alternating blockquote and list containers. A list
// marker appears only on the opening line; continuation lines replace it with
// its visual content indent, while every blockquote marker must remain present.
function fenceContainerPrefix(line: string): { containers: FenceContainer[]; content: string } {
  const containers: FenceContainer[] = []
  let column = 0
  let cursor = 0

  while (cursor < line.length) {
    const segmentStart = cursor
    const segmentColumn = column
    const leading = consumeIndent(line, cursor, column, 3)

    cursor = leading.cursor
    column += leading.columns

    if (line[cursor] === '>') {
      containers.push({ kind: 'blockquote' })
      cursor += 1
      column += 1

      if (line[cursor] === ' ' || line[cursor] === '\t') {
        column = nextColumn(column, line[cursor] || '')
        cursor += 1
      }

      continue
    }

    const afterMarker = listMarkerEnd(line, cursor)

    if (afterMarker === cursor || (line[afterMarker] !== ' ' && line[afterMarker] !== '\t')) {
      cursor = segmentStart
      column = segmentColumn

      break
    }

    column += afterMarker - cursor
    cursor = afterMarker

    const padding = consumeIndent(line, cursor, column, 5)
    const contentIndent = padding.columns > 4 ? consumeIndent(line, cursor, column, 1) : padding

    cursor = contentIndent.cursor
    column += contentIndent.columns
    containers.push({ indent: column - segmentColumn, kind: 'list' })
  }

  return { containers, content: line.slice(cursor) }
}

function stripFenceContainerPrefix(line: string, containers: FenceContainer[]): string | null {
  let column = 0
  let cursor = 0

  for (const container of containers) {
    if (container.kind === 'list') {
      const indent = consumeIndent(line, cursor, column, container.indent)

      if (indent.columns !== container.indent) {
        return null
      }

      cursor = indent.cursor
      column += indent.columns

      continue
    }

    const leading = consumeIndent(line, cursor, column, 3)

    cursor = leading.cursor
    column += leading.columns

    if (line[cursor] !== '>') {
      return null
    }

    cursor += 1
    column += 1

    if (line[cursor] === ' ' || line[cursor] === '\t') {
      column = nextColumn(column, line[cursor] || '')
      cursor += 1
    }
  }

  return line.slice(cursor)
}

function fenceMarker(line: string): FenceMarker | null {
  const container = fenceContainerPrefix(line)
  const match = /^ {0,3}(`{3,}|~{3,})/.exec(container.content)

  if (!match) {
    return null
  }

  const marker = match[1] || ''

  return {
    character: marker[0] as FenceMarker['character'],
    containers: container.containers,
    length: marker.length
  }
}

function isFenceClose(line: string, marker: FenceMarker): boolean {
  const content = stripFenceContainerPrefix(line, marker.containers)

  if (content === null) {
    return false
  }

  let cursor = 0

  while (cursor < Math.min(3, content.length) && content[cursor] === ' ') {
    cursor += 1
  }

  let run = 0

  while (content[cursor + run] === marker.character) {
    run += 1
  }

  return run >= marker.length && content.slice(cursor + run).trim() === ''
}

function collectBlockCodeRanges(text: string, includeUnclosedFence = true): Range[] {
  const ranges: Range[] = []
  let cursor = 0
  let openFence: { marker: FenceMarker; start: number } | null = null

  while (cursor < text.length) {
    const end = lineEnd(text, cursor)
    const line = text.slice(cursor, end)
    const next = end < text.length ? end + 1 : end

    if (openFence) {
      if (isFenceClose(line, openFence.marker)) {
        ranges.push({ end: next, kind: 'fence', start: openFence.start })
        openFence = null
      }
    } else {
      const marker = fenceMarker(line)

      if (marker) {
        openFence = { marker, start: cursor }
      } else {
        const content = stripBlockquotePrefix(line).content

        if (/^(?: {4}|\t)/.test(content) && content.trim()) {
          ranges.push({ end: next, kind: 'indented', start: cursor })
        }
      }
    }

    cursor = next
  }

  if (openFence && includeUnclosedFence) {
    ranges.push({ end: text.length, kind: 'fence', start: openFence.start })
  }

  return ranges
}

export function collectFenceRanges(text: string, includeUnclosed = true): Array<{ end: number; start: number }> {
  return collectBlockCodeRanges(text, includeUnclosed)
    .filter(range => range.kind === 'fence')
    .map(({ end, start }) => ({ end, start }))
}

export function collectBalancedFenceRanges(text: string): Array<{ end: number; start: number }> {
  return collectFenceRanges(text, false)
}

/**
 * Return indented-code spans, including blank continuation lines between or
 * immediately after code lines. Consumers can transform the surrounding
 * markdown without changing code bytes or mistaking indented backticks for
 * real fence markers.
 */
export function collectIndentedCodeRanges(text: string): Array<{ end: number; start: number }> {
  const rawRanges = collectBlockCodeRanges(text)
    .filter(range => range.kind === 'indented')
    .map(({ end, start }) => ({ end, start }))

  const ranges: Array<{ end: number; start: number }> = []

  for (const rawRange of rawRanges) {
    let end = rawRange.end

    while (end < text.length) {
      const nextEnd = lineEnd(text, end)
      const next = nextEnd < text.length ? nextEnd + 1 : nextEnd
      const line = text.slice(end, nextEnd)

      if (stripBlockquotePrefix(line).content.trim()) {
        break
      }

      end = next
    }

    const previous = ranges[ranges.length - 1]

    if (previous && rawRange.start <= previous.end) {
      previous.end = Math.max(previous.end, end)
    } else {
      ranges.push({ end, start: rawRange.start })
    }
  }

  return ranges
}

function backtickRun(text: string, start: number): number {
  let length = 0

  while (text[start + length] === '`') {
    length += 1
  }

  return length
}

function findClosingBacktickRun(text: string, start: number, length: number): number {
  let cursor = start

  while (cursor < text.length) {
    const next = text.indexOf('`', cursor)

    if (next === -1) {
      return -1
    }

    const run = backtickRun(text, next)

    if (run === length) {
      return next + run
    }

    cursor = next + run
  }

  return -1
}

function rangeContaining(ranges: Range[], offset: number): Range | null {
  let low = 0
  let high = ranges.length - 1
  let candidate: Range | null = null

  while (low <= high) {
    const middle = Math.floor((low + high) / 2)
    const range = ranges[middle]

    if (!range || range.start > offset) {
      high = middle - 1
    } else {
      candidate = range
      low = middle + 1
    }
  }

  return candidate && offset < candidate.end ? candidate : null
}

function collectProtectedRanges(text: string): Range[] {
  const blockRanges = collectBlockCodeRanges(text).sort((a, b) => a.start - b.start)
  const inlineRanges: Range[] = []
  let cursor = 0

  while (cursor < text.length) {
    const protectedRange = rangeContaining(blockRanges, cursor)

    if (protectedRange) {
      cursor = protectedRange.end

      continue
    }

    if (text[cursor] !== '`') {
      cursor += 1

      continue
    }

    const length = backtickRun(text, cursor)
    const end = findClosingBacktickRun(text, cursor + length, length)

    if (end === -1) {
      cursor += length

      continue
    }

    inlineRanges.push({ end, kind: 'inline', start: cursor })
    cursor = end
  }

  return [...blockRanges, ...inlineRanges].sort((a, b) => a.start - b.start)
}

function isEscaped(text: string, offset: number): boolean {
  let slashes = 0

  for (let cursor = offset - 1; cursor >= 0 && text[cursor] === '\\'; cursor -= 1) {
    slashes += 1
  }

  return slashes % 2 === 1
}

function findQuotedTagEnd(text: string, start: number): number {
  let quote = ''

  for (let cursor = start + 1; cursor < text.length; cursor += 1) {
    const character = text[cursor] || ''

    if (quote) {
      if (character === quote) {
        quote = ''
      }

      continue
    }

    if (character === '"' || character === "'") {
      quote = character
    } else if (character === '>') {
      return cursor + 1
    }
  }

  return -1
}

function readMarkupToken(text: string, start: number): MarkupToken | typeof TERMINAL_MARKUP {
  if (text.startsWith('<!--', start)) {
    const close = text.indexOf('-->', start + 4)

    return close === -1 ? TERMINAL_MARKUP : { end: close + 3, kind: 'other', selfClosing: false }
  }

  if (text.startsWith('<![CDATA[', start)) {
    const close = text.indexOf(']]>', start + 9)

    return close === -1 ? TERMINAL_MARKUP : { end: close + 3, kind: 'other', selfClosing: false }
  }

  if (text.startsWith('<?', start)) {
    const close = text.indexOf('?>', start + 2)

    return close === -1 ? TERMINAL_MARKUP : { end: close + 2, kind: 'other', selfClosing: false }
  }

  const end = findQuotedTagEnd(text, start)

  if (end === -1) {
    return TERMINAL_MARKUP
  }

  const tag = text.slice(start, end)
  const closing = /^<\s*\/\s*svg\s*>$/i.test(tag)
  const opening = !closing && /^<\s*svg(?=[\s/>])/i.test(tag)

  return {
    end,
    kind: closing ? 'svg-close' : opening ? 'svg-open' : 'other',
    selfClosing: /\/\s*>$/.test(tag)
  }
}

function createBlankLineDetector(text: string): (start: number, end: number) => boolean {
  const blankLineStarts: number[] = []
  const blankLinePattern = /\r?\n[\t ]*\r?\n/g
  let match: RegExpExecArray | null

  while ((match = blankLinePattern.exec(text)) !== null) {
    blankLineStarts.push(match.index)
  }

  let nextBlankLine = 0

  // SVG scanning asks about monotonically increasing end offsets. Advancing one
  // shared pointer keeps every blank-line boundary query O(1) amortized.
  return (start: number, end: number): boolean => {
    while (blankLineStarts[nextBlankLine] !== undefined && blankLineStarts[nextBlankLine] < end) {
      nextBlankLine += 1
    }

    return nextBlankLine > 0 && (blankLineStarts[nextBlankLine - 1] ?? -1) >= start
  }
}

function separatorBefore(value: string): string {
  if (!value || value.endsWith('\n\n')) {
    return ''
  }

  return value.endsWith('\n') ? '\n' : '\n\n'
}

function separatorAfter(text: string, offset: number): string {
  if (offset >= text.length || text.startsWith('\n\n', offset)) {
    return ''
  }

  return text[offset] === '\n' ? '\n' : '\n\n'
}

function fenceFor(svg: string): string | null {
  if (!svg.includes('```')) {
    return '```'
  }

  if (!svg.includes('~~~')) {
    return '~~~'
  }

  return null
}

/**
 * Lift balanced, bare SVG markup into the existing fenced-SVG renderer.
 *
 * Code ranges are discovered before SVG scanning, so a malformed opening tag
 * can never consume a closing tag from fenced, inline, or indented code. The
 * scanner is quote-aware and balances nested SVG tags; malformed markup stays
 * on Streamdown's inert raw-HTML path.
 */
export function fenceRawSvgBlocks(text: string): string {
  // This runs for every streaming markdown update. Avoid all code-range and
  // blank-line indexing when the growing buffer cannot contain an SVG opener.
  if (!/<\s*svg(?=[\s/>])/i.test(text)) {
    return text
  }

  const protectedRanges = collectProtectedRanges(text)
  const crossesBlankLine = createBlankLineDetector(text)
  let activeSvg: { depth: number; lastTokenEnd: number; start: number } | null = null
  let output = ''
  let copiedThrough = 0
  let cursor = 0
  let protectedIndex = 0

  while (cursor < text.length) {
    while (protectedRanges[protectedIndex] && protectedRanges[protectedIndex].end <= cursor) {
      protectedIndex += 1
    }

    const next = text.indexOf('<', cursor)

    if (next === -1) {
      break
    }

    const protectedRange = protectedRanges[protectedIndex]

    if (protectedRange && protectedRange.start < next) {
      const indentedSvgContent =
        activeSvg &&
        protectedRange.kind === 'indented' &&
        !crossesBlankLine(activeSvg.lastTokenEnd, protectedRange.start)

      if (!indentedSvgContent) {
        activeSvg = null
      }

      cursor = protectedRange.end

      continue
    }

    if (protectedRange && next >= protectedRange.start && next < protectedRange.end) {
      const indentedSvgContent =
        activeSvg && protectedRange.kind === 'indented' && !crossesBlankLine(activeSvg.lastTokenEnd, next)

      if (!indentedSvgContent) {
        activeSvg = null
        cursor = protectedRange.end

        continue
      }
    }

    const token = readMarkupToken(text, next)

    // A construct that starts here but never terminates owns the remainder of
    // the streaming buffer. Do not reinterpret '<' bytes inside it as markup;
    // that is both fail-open and an end-of-buffer rescan for every suffix.
    if (token === TERMINAL_MARKUP) {
      activeSvg = null

      break
    }

    if (token.kind === 'svg-open' && !token.selfClosing) {
      if (activeSvg) {
        activeSvg.depth += 1
        activeSvg.lastTokenEnd = token.end
      } else if (!isEscaped(text, next)) {
        activeSvg = { depth: 1, lastTokenEnd: token.end, start: next }
      }
    } else if (token.kind === 'svg-close' && activeSvg) {
      activeSvg.depth -= 1
      activeSvg.lastTokenEnd = token.end

      if (activeSvg.depth === 0) {
        const svg = text.slice(activeSvg.start, token.end)
        const fence = fenceFor(svg)

        if (fence) {
          const before = text.slice(copiedThrough, activeSvg.start)

          output += before
          output += separatorBefore(output)
          output += `${fence}svg\n${svg}\n${fence}`
          output += separatorAfter(text, token.end)
          copiedThrough = token.end
        }

        activeSvg = null
      }
    } else if (activeSvg) {
      activeSvg.lastTokenEnd = token.end
    }

    cursor = token.end
  }

  return copiedThrough === 0 ? text : output + text.slice(copiedThrough)
}
