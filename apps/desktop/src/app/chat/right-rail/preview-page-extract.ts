export interface PreviewViewportHeading {
  level: number
  text: string
}

export interface ExtractedPreviewPage {
  scroll_height: number
  scroll_ratio: number
  scroll_y: number
  selection_text?: string
  text: string
  viewport_height: number
  visible_headings: PreviewViewportHeading[]
  visible_text: string
}

export const PREVIEW_VISIBLE_HEADING_LIMIT = 8
export const PREVIEW_VISIBLE_HEADING_TEXT_MAX_CHARS = 200
export const PREVIEW_VISIBLE_TEXT_MAX_CHARS = 6_000
export const PREVIEW_SELECTION_TEXT_MAX_CHARS = 2_000

/**
 * Runs inside the preview guest. Keep this function self-contained: the
 * renderer serializes it below and sends it through webview.executeJavaScript.
 */
export function extractPreviewPage(): ExtractedPreviewPage {
  const visibleHeadingLimit = 8
  const visibleHeadingTextMaxChars = 200
  const visibleTextMaxChars = 6_000
  const selectionTextMaxChars = 2_000
  const body = document.body
  const root = document.documentElement
  const scrollingElement = document.scrollingElement ?? root
  const viewportHeight = Math.max(0, window.innerHeight || root?.clientHeight || 0)
  const viewportWidth = Math.max(0, window.innerWidth || root?.clientWidth || 0)
  const rawScrollY = window.scrollY || scrollingElement?.scrollTop || 0

  const scrollHeight = Math.max(
    viewportHeight,
    scrollingElement?.scrollHeight || 0,
    root?.scrollHeight || 0,
    body?.scrollHeight || 0
  )

  const maxScrollY = Math.max(0, scrollHeight - viewportHeight)
  const scrollY = Math.min(Math.max(0, Number.isFinite(rawScrollY) ? rawScrollY : 0), maxScrollY)
  const scrollRatio = maxScrollY > 0 ? scrollY / maxScrollY : 0

  const intersectsViewport = (rect: DOMRect, element: Element): boolean => {
    let left = Math.max(0, rect.left)
    let right = Math.min(viewportWidth, rect.right)
    let top = Math.max(0, rect.top)
    let bottom = Math.min(viewportHeight, rect.bottom)

    if (right <= left || bottom <= top || rect.width <= 0 || rect.height <= 0) {
      return false
    }

    for (let ancestor: Element | null = element; ancestor && ancestor !== body; ancestor = ancestor.parentElement) {
      const style = window.getComputedStyle(ancestor)

      if (style.display === 'none' || style.visibility === 'hidden') {
        return false
      }

      const clips = `${style.overflow} ${style.overflowX} ${style.overflowY}`

      if (/auto|clip|hidden|scroll/.test(clips)) {
        const ancestorRect = ancestor.getBoundingClientRect()

        left = Math.max(left, ancestorRect.left)
        right = Math.min(right, ancestorRect.right)
        top = Math.max(top, ancestorRect.top)
        bottom = Math.min(bottom, ancestorRect.bottom)

        if (right <= left || bottom <= top) {
          return false
        }
      }
    }

    return true
  }

  const elementIsVisible = (element: Element): boolean => {
    const style = window.getComputedStyle(element)

    return style.display !== 'none' && style.visibility !== 'hidden' && intersectsViewport(element.getBoundingClientRect(), element)
  }

  const visibleHeadings = body
    ? Array.from(body.querySelectorAll<HTMLElement>('h1,h2,h3,h4,h5,h6'))
        .filter(elementIsVisible)
        .slice(0, visibleHeadingLimit)
        .map(element => ({
          level: Number(element.tagName.slice(1)),
          text: (element.innerText || element.textContent || '').trim().slice(0, visibleHeadingTextMaxChars)
        }))
        .filter(heading => heading.text.length > 0)
    : []

  const visibleTextParts: string[] = []
  let visibleTextLength = 0

  if (body) {
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT)
    let node: Node | null

    while (visibleTextLength < visibleTextMaxChars && (node = walker.nextNode())) {
      const parent = node.parentElement
      const value = (node.nodeValue || '').replace(/\s+/g, ' ').trim()

      if (!parent || !value || parent.closest('script,style,noscript,template')) {
        continue
      }

      const style = window.getComputedStyle(parent)

      if (style.display === 'none' || style.visibility === 'hidden') {
        continue
      }

      const range = document.createRange()
      range.selectNodeContents(node)
      const rects = typeof range.getClientRects === 'function' ? Array.from(range.getClientRects()) : []

      const isVisible = (rects.length ? rects : [parent.getBoundingClientRect()]).some(rect =>
        intersectsViewport(rect, parent)
      )

      range.detach?.()

      if (!isVisible) {
        continue
      }

      const separatorLength = visibleTextParts.length ? 1 : 0
      const remaining = visibleTextMaxChars - visibleTextLength - separatorLength

      if (remaining <= 0) {
        break
      }

      const part = value.slice(0, remaining)

      visibleTextParts.push(part)
      visibleTextLength += separatorLength + part.length
    }
  }

  const selection = String(window.getSelection?.() || '')
    .trim()
    .slice(0, selectionTextMaxChars)

  return {
    scroll_height: scrollHeight,
    scroll_ratio: Math.min(1, Math.max(0, scrollRatio)),
    scroll_y: scrollY,
    ...(selection ? { selection_text: selection } : {}),
    text: body ? (typeof body.innerText === 'string' ? body.innerText : body.textContent || '') : '',
    viewport_height: viewportHeight,
    visible_headings: visibleHeadings,
    visible_text: visibleTextParts.join('\n')
  }
}

export const PREVIEW_PAGE_EXTRACT_SCRIPT = `(${extractPreviewPage.toString()})()`
