/**
 * Pure helpers for inline Kanban card references in assistant markdown.
 *
 * The token rule intentionally mirrors the kanban-card-links plugin so the
 * renderer and the plugin agree on which ids are references. Markdown hrefs
 * carry the canonical `t_` id; the original token remains the visible label.
 */

/** `t_<hex>` and `@card:<hex>` tokens, with the plugin's negative-class guard. */
export const KANBAN_CARD_TOKEN_RE = /(?<![A-Za-z0-9_])@card:[0-9a-f]{4,}|(?<![A-Za-z0-9_])t_[0-9a-f]{4,}/g

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

function isInsideMarkdownLinkDestination(text: string, index: number): boolean {
  const destinationStart = text.lastIndexOf('](', index)
  const destinationEnd = text.lastIndexOf(')', index)

  return destinationStart > destinationEnd
}

function isInsideMarkdownLinkLabel(text: string, index: number): boolean {
  const labelStart = text.lastIndexOf('[', index)
  const labelEnd = text.lastIndexOf(']', index)

  return labelStart > labelEnd && text.indexOf('](', index) !== -1
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

  return text.replace(KANBAN_CARD_TOKEN_RE, (token, index: number, source: string) => {
    if (
      isInsideAngleAutolink(source, index) ||
      isInsideMarkdownLinkDestination(source, index) ||
      isInsideMarkdownLinkLabel(source, index)
    ) {
      return token
    }

    const id = normalizeKanbanCardToken(token)

    return `[${token}](${kanbanCardMarkdownHref(id)})`
  })
}
