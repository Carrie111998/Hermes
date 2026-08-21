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

interface FenceContainerLine {
  containers: FenceContainer[]
  content: string
  continuationDepth: number
  end: number
  prefixLength: number
  start: number
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
function fenceContainerPrefix(
  line: string,
  start = 0,
  initialColumn = 0
): { containers: FenceContainer[]; content: string } {
  const containers: FenceContainer[] = []
  let column = initialColumn
  let cursor = start

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

function stripFenceContainerPrefix(
  line: string,
  containers: FenceContainer[],
  allowUnindentedListBlank = false
): string | null {
  let column = 0
  let cursor = 0

  for (let index = 0; index < containers.length; index += 1) {
    const container = containers[index]

    if (!container) {
      continue
    }

    if (container.kind === 'list') {
      const indent = consumeIndent(line, cursor, column, container.indent)

      if (indent.columns !== container.indent) {
        const remainingContainersAreLists = containers.slice(index).every(candidate => candidate.kind === 'list')

        if (allowUnindentedListBlank && remainingContainersAreLists && !line.slice(cursor).trim()) {
          return ''
        }

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

function continuedFenceContainerPrefix(line: string, inherited: FenceContainer[]) {
  let column = 0
  let cursor = 0
  let continuationDepth = 0

  for (let index = 0; index < inherited.length; index += 1) {
    const container = inherited[index]
    const segmentColumn = column
    const segmentStart = cursor

    if (!container) {
      continue
    }

    if (container.kind === 'list') {
      const indent = consumeIndent(line, cursor, column, container.indent)

      if (indent.columns !== container.indent) {
        if (!line.slice(cursor).trim()) {
          let implicitDepth = index

          while (inherited[implicitDepth]?.kind === 'list') {
            implicitDepth += 1
          }

          const containers = implicitDepth === inherited.length ? inherited : inherited.slice(0, implicitDepth)

          return { containers, content: '', continuationDepth: implicitDepth }
        }

        break
      }

      cursor = indent.cursor
      column += indent.columns
      continuationDepth = index + 1

      continue
    }

    const leading = consumeIndent(line, cursor, column, 3)

    cursor = leading.cursor
    column += leading.columns

    if (line[cursor] !== '>') {
      cursor = segmentStart
      column = segmentColumn

      break
    }

    cursor += 1
    column += 1

    if (line[cursor] === ' ' || line[cursor] === '\t') {
      column = nextColumn(column, line[cursor] || '')
      cursor += 1
    }

    continuationDepth = index + 1
  }

  const nested = fenceContainerPrefix(line, cursor, column)

  const continued = continuationDepth === inherited.length ? inherited : inherited.slice(0, continuationDepth)

  return {
    containers: nested.containers.length > 0 ? [...continued, ...nested.containers] : continued,
    content: nested.content,
    continuationDepth
  }
}

function fenceMarker(line: string, inherited: FenceContainer[] = []): FenceMarker | null {
  const container = continuedFenceContainerPrefix(line, inherited)
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
  let inheritedContainers: FenceContainer[] = []
  let openFence: { marker: FenceMarker; start: number } | null = null

  while (cursor < text.length) {
    const end = lineEnd(text, cursor)
    const line = text.slice(cursor, end)
    const next = end < text.length ? end + 1 : end

    if (openFence) {
      const continuation = stripFenceContainerPrefix(line, openFence.marker.containers, true)

      if (continuation === null) {
        // CommonMark closes a fenced block with its container. The first line
        // that no longer continues every required quote/list belongs to the
        // outer document and must be parsed again as a possible new block.
        ranges.push({ end: cursor, kind: 'fence', start: openFence.start })
        inheritedContainers = continuedFenceContainerPrefix(line, openFence.marker.containers).containers
        openFence = null

        continue
      }

      if (isFenceClose(line, openFence.marker)) {
        ranges.push({ end: next, kind: 'fence', start: openFence.start })
        inheritedContainers = openFence.marker.containers
        openFence = null
      }
    } else {
      const marker = fenceMarker(line, inheritedContainers)

      if (marker) {
        openFence = { marker, start: cursor }
      } else {
        const content = continuedFenceContainerPrefix(line, inheritedContainers).content

        if (/^(?: {4}|\t)/.test(content) && content.trim()) {
          ranges.push({ end: next, kind: 'indented', start: cursor })
        }

        inheritedContainers = continuedFenceContainerPrefix(line, inheritedContainers).containers
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

interface BacktickDelimiter {
  end: number
  length: number
  scope: number
  start: number
}

function backtickRun(text: string, start: number): number {
  let length = 0

  while (text[start + length] === '`') {
    length += 1
  }

  return length
}

function isIsolatedInlineBlock(content: string): boolean {
  return (
    /^ {0,3}#{1,6}(?:[ \t]+|$)/.test(content) ||
    /^ {0,3}(?:`{3,}|~{3,})/.test(content) ||
    /^ {0,3}(?:(?:\*[ \t]*){3,}|(?:_[ \t]*){3,}|(?:-[ \t]*){3,})$/.test(content) ||
    /^ {0,3}(?:=+|-+)[ \t]*$/.test(content)
  )
}

function collectBacktickDelimiters(text: string, lines: FenceContainerLine[]): BacktickDelimiter[] {
  const delimiters: BacktickDelimiter[] = []
  let previousWasIsolated = false
  let scope = 0

  for (let lineIndex = 0; lineIndex < lines.length; lineIndex += 1) {
    const line = lines[lineIndex]

    if (!line) {
      continue
    }

    const blank = !line.content.trim()
    const isolated = isIsolatedInlineBlock(line.content)
    const opensContainer = lineIndex > 0 && line.continuationDepth < line.containers.length

    if (blank || isolated || previousWasIsolated || opensContainer) {
      scope += 1
    }

    let cursor = line.start

    while (cursor < line.end) {
      if (text[cursor] !== '`') {
        cursor += 1

        continue
      }

      const length = backtickRun(text, cursor)

      delimiters.push({ end: cursor + length, length, scope, start: cursor })
      cursor += length
    }

    if (blank) {
      scope += 1
    }

    previousWasIsolated = isolated
  }

  return delimiters
}

function collectProtectedRanges(text: string, containerLines: FenceContainerLine[]): Range[] {
  const blockRanges = collectBlockCodeRanges(text).sort((a, b) => a.start - b.start)
  const delimiters = collectBacktickDelimiters(text, containerLines)
  const nextMatchingDelimiter = new Array<number>(delimiters.length).fill(-1)
  const nextByLength = new Map<number, number>()
  const inlineRanges: Range[] = []
  let delimiterScope = -1

  for (let index = delimiters.length - 1; index >= 0; index -= 1) {
    const delimiter = delimiters[index]

    if (!delimiter) {
      continue
    }

    if (delimiter.scope !== delimiterScope) {
      nextByLength.clear()
      delimiterScope = delimiter.scope
    }

    nextMatchingDelimiter[index] = nextByLength.get(delimiter.length) ?? -1
    nextByLength.set(delimiter.length, index)
  }

  let blockIndex = 0
  let delimiterIndex = 0

  while (delimiterIndex < delimiters.length) {
    const delimiter = delimiters[delimiterIndex]

    if (!delimiter) {
      break
    }

    while (blockRanges[blockIndex] && blockRanges[blockIndex].end <= delimiter.start) {
      blockIndex += 1
    }

    const blockRange = blockRanges[blockIndex]

    if (blockRange && blockRange.start <= delimiter.start && delimiter.start < blockRange.end) {
      delimiterIndex += 1

      continue
    }

    const closingIndex = nextMatchingDelimiter[delimiterIndex] ?? -1

    if (closingIndex === -1) {
      delimiterIndex += 1

      continue
    }

    const closing = delimiters[closingIndex]

    if (!closing) {
      delimiterIndex += 1

      continue
    }

    inlineRanges.push({ end: closing.end, kind: 'inline', start: delimiter.start })
    delimiterIndex = closingIndex + 1
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

function collectFenceContainerLines(text: string): FenceContainerLine[] {
  const lines: FenceContainerLine[] = []
  let cursor = 0
  let inherited: FenceContainer[] = []

  while (cursor < text.length) {
    const end = lineEnd(text, cursor)
    const line = text.slice(cursor, end)
    const container = continuedFenceContainerPrefix(line, inherited)

    lines.push({
      containers: container.containers,
      content: container.content,
      continuationDepth: container.continuationDepth,
      end,
      prefixLength: line.length - container.content.length,
      start: cursor
    })
    inherited = container.containers
    cursor = end < text.length ? end + 1 : end
  }

  return lines
}

function continuationPrefixFor(containers: FenceContainer[]): string {
  let prefix = ''

  for (const container of containers) {
    prefix += container.kind === 'blockquote' ? '> ' : ' '.repeat(container.indent)
  }

  return prefix
}

function svgWithoutContainerPrefixes(
  text: string,
  start: number,
  end: number,
  containers: FenceContainer[]
): string | null {
  const firstEnd = lineEnd(text, start)
  let svg = text.slice(start, Math.min(firstEnd, end))
  let cursor = firstEnd

  while (cursor < end) {
    const nextStart = cursor + 1
    const nextEnd = lineEnd(text, nextStart)
    const line = text.slice(nextStart, nextEnd)
    const content = stripFenceContainerPrefix(line, containers, true)

    if (content === null) {
      return null
    }

    const prefixLength = line.length - content.length
    const contentEnd = Math.max(0, Math.min(end, nextEnd) - nextStart - prefixLength)

    svg += `\n${content.slice(0, contentEnd)}`
    cursor = nextEnd
  }

  return svg
}

function separatorBeforeContainer(value: string, blankPrefix: string): string {
  if (!value || value.endsWith('\n\n')) {
    return ''
  }

  return value.endsWith('\n') ? `${blankPrefix}\n` : `\n${blankPrefix}\n`
}

function separatorAfterContainer(
  text: string,
  offset: number,
  blankPrefix: string,
  continuationPrefix: string
): string {
  if (offset >= text.length || text.startsWith('\n\n', offset)) {
    return ''
  }

  return text[offset] === '\n' ? `\n${blankPrefix}` : `\n${blankPrefix}\n${continuationPrefix}`
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

  const containerLines = collectFenceContainerLines(text)
  const protectedRanges = collectProtectedRanges(text, containerLines)
  const crossesBlankLine = createBlankLineDetector(text)
  let activeSvg: { depth: number; lastTokenEnd: number; line: FenceContainerLine; start: number } | null = null
  let output = ''
  let copiedThrough = 0
  let cursor = 0
  let protectedIndex = 0
  let containerLineIndex = 0

  while (cursor < text.length) {
    while (protectedRanges[protectedIndex] && protectedRanges[protectedIndex].end <= cursor) {
      protectedIndex += 1
    }

    const next = text.indexOf('<', cursor)

    if (next === -1) {
      break
    }

    while (containerLines[containerLineIndex] && containerLines[containerLineIndex].end < next) {
      containerLineIndex += 1
    }

    const containerLine = containerLines[containerLineIndex]
    const protectedRange = protectedRanges[protectedIndex]

    if (activeSvg && containerLine && containerLine.start > activeSvg.line.start) {
      const physicalLine = text.slice(containerLine.start, containerLine.end)

      if (stripFenceContainerPrefix(physicalLine, activeSvg.line.containers, true) === null) {
        // A quote/list container owns only lines that continue its prefix. End
        // the malformed candidate before interpreting this boundary line so a
        // valid SVG that starts the outer document is considered from scratch.
        activeSvg = null
      }
    }

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
      } else if (!isEscaped(text, next) && containerLine) {
        activeSvg = { depth: 1, lastTokenEnd: token.end, line: containerLine, start: next }
      }
    } else if (token.kind === 'svg-close' && activeSvg) {
      activeSvg.depth -= 1
      activeSvg.lastTokenEnd = token.end

      if (activeSvg.depth === 0) {
        const svg = svgWithoutContainerPrefixes(text, activeSvg.start, token.end, activeSvg.line.containers)
        const fence = svg ? fenceFor(svg) : null

        if (fence && svg) {
          const continuationPrefix = continuationPrefixFor(activeSvg.line.containers)
          const blankPrefix = continuationPrefix.replace(/[ \t]+$/, '')
          const contentBeforeSvg = text.slice(activeSvg.line.start + activeSvg.line.prefixLength, activeSvg.start)
          const startsLineContent = !contentBeforeSvg.trim()
          const replacementStart = startsLineContent ? activeSvg.line.start : activeSvg.start

          const openingPrefix = startsLineContent
            ? text.slice(activeSvg.line.start, activeSvg.start)
            : continuationPrefix

          const prefixedSvg = svg
            .split('\n')
            .map(line => `${continuationPrefix}${line}`)
            .join('\n')

          const before = text.slice(copiedThrough, replacementStart)

          output += before
          output += separatorBeforeContainer(output, blankPrefix)
          output += `${openingPrefix}${fence}svg\n${prefixedSvg}\n${continuationPrefix}${fence}`
          output += separatorAfterContainer(text, token.end, blankPrefix, continuationPrefix)
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
