/**
 * Pure helpers for inline Kanban card references in assistant markdown.
 *
 * The token rule intentionally mirrors the kanban-card-links plugin so the
 * renderer and the plugin agree on which ids are references. Markdown hrefs
 * carry the canonical `t_` id; the original token remains the visible label.
 */

/** `t_<hex>` and `@card:<hex>` tokens, with the plugin's negative-class guard. */
export const KANBAN_CARD_TOKEN_RE = /(?<![A-Za-z0-9_])@card:[0-9a-f]{4,}|(?<![A-Za-z0-9_])t_[0-9a-f]{4,}/g

const BRACKETED_KANBAN_CARD_RE = /\[(@card:[0-9a-f]{4,}|t_[0-9a-f]{4,})\](?!\()/g
const KANBAN_CARD_HREF_PREFIX = '#kanban/'
const KANBAN_CARD_ID_RE = /^t_[0-9a-f]{4,}$/

export function normalizeKanbanCardToken(token: string): string {
  return token.startsWith('@card:') ? `t_${token.slice('@card:'.length)}` : token
}

export function kanbanCardMarkdownHref(id: string): string {
  return `${KANBAN_CARD_HREF_PREFIX}${encodeURIComponent(id)}`
}

export function kanbanCardRefFromMarkdownHref(href?: string): null | string {
  if (!href?.startsWith(KANBAN_CARD_HREF_PREFIX)) {
    return null
  }

  try {
    const id = decodeURIComponent(href.slice(KANBAN_CARD_HREF_PREFIX.length))

    return KANBAN_CARD_ID_RE.test(id) ? id : null
  } catch {
    return null
  }
}

function isInsideAngleAutolink(text: string, index: number): boolean {
  return text.lastIndexOf('<', index) > text.lastIndexOf('>', index)
}

function isInsideRawUrl(text: string, index: number): boolean {
  return /(?:^|[^A-Za-z0-9_])https?:\/\/[^\s<>`]*$/i.test(text.slice(0, index))
}

function isInsideMarkdownLinkDestination(text: string, index: number): boolean {
  const destinationStart = text.lastIndexOf('](', index)
  const destinationEnd = text.lastIndexOf(')', index)

  return destinationStart > destinationEnd
}

function isInsideMarkdownLinkLabel(text: string, index: number): boolean {
  const labelStarts: number[] = []

  for (let cursor = 0; cursor < text.length; cursor += 1) {
    if (text[cursor] === '[') {
      labelStarts.push(cursor)

      continue
    }

    if (text[cursor] !== ']') {
      continue
    }

    const labelStart = labelStarts.pop()

    if (labelStart !== undefined && labelStart < index && index < cursor && text[cursor + 1] === '(') {
      return true
    }
  }

  return false
}

function shouldLeaveKanbanCardTokenUnchanged(text: string, index: number): boolean {
  return (
    isInsideAngleAutolink(text, index) ||
    isInsideRawUrl(text, index) ||
    isInsideMarkdownLinkDestination(text, index) ||
    isInsideMarkdownLinkLabel(text, index)
  )
}

/**
 * Rewrites visible card tokens into markdown links so they reach the same
 * renderer seam as URLs. The caller must split out inline code and fenced
 * blocks first; `preprocessMarkdown` does that before calling this helper.
 */
export function linkifyKanbanCardRefs(text: string): string {
  if (!text.includes('t_') && !text.includes('@card:')) {
    return text
  }

  const bracketNormalized = text.replace(BRACKETED_KANBAN_CARD_RE, (match, token: string, index: number, source: string) => {
    const tokenIndex = index + 1

    if (shouldLeaveKanbanCardTokenUnchanged(source, tokenIndex)) {
      return match
    }

    return `[${token}](${kanbanCardMarkdownHref(normalizeKanbanCardToken(token))})`
  })

  return bracketNormalized.replace(KANBAN_CARD_TOKEN_RE, (token, index: number, source: string) => {
    if (shouldLeaveKanbanCardTokenUnchanged(source, index)) {
      return token
    }

    const id = normalizeKanbanCardToken(token)

    return `[${token}](${kanbanCardMarkdownHref(id)})`
  })
}
