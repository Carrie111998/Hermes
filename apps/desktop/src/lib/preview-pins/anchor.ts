/**
 * ANCHOR — what a pin is fastened to, and how it finds its way home after the
 * page is rebuilt underneath it.
 *
 * This is the whole feature. `annotate_preview` can bind to a `@e` handle
 * because an agent's mark lives for one turn; a handle is documented as durable
 * only "for as long as this page is open", and `identity.ts` deliberately mints
 * a NEW handle rather than guess when its affinity falls below the bar. Both
 * are right for the agent. Both are fatal for a user's comment, which has to
 * outlive a reload, a hot-reload, and a framework re-render — otherwise the
 * feature demos beautifully and dies at the first ⌘R.
 *
 * So a pin captures several independent signals and, on the way back, scores
 * candidates against all of them. When nothing scores well enough the pin is
 * reported ORPHANED rather than attached to a plausible neighbour. That
 * asymmetry is deliberate and matches identity.ts's reasoning: a pin the user
 * has to re-place is an annoyance, a comment silently pointing at the wrong
 * button is a wrong instruction to the agent.
 *
 * SELF-CONTAINMENT. `anchorKit` is stringified into the guest page by
 * `pin-in-page.ts`. Module scope does not exist there, so the factory closes
 * over nothing but its argument and declares every helper inside itself. One
 * free identifier is a ReferenceError that Electron reports only as "Script
 * failed to execute". Same contract as `preview-act/naming.ts`; see its header.
 */

/** Every signal we know about one pinned element, captured at pin time. */
export interface PinAnchor {
  /** `#id` or `[data-testid]` — the page's own promise of identity, if it made one. */
  selector: string
  /** Structural path from the document root: `body>main>div:nth-of-type(2)>button`. */
  path: string
  /** ARIA role, else the tag name. */
  role: string
  /** Accessible name at pin time. */
  label: string
  /** Normalised text excerpt, for pages whose labels are all empty. */
  text: string
  /** Which one it was among same-role, same-label siblings. */
  ordinal: number
  /** Position as fractions of the document box, so a resize does not move it. */
  rect: { h: number; w: number; x: number; y: number }
}

export interface AnchorMatch {
  /** 0..1. Below `ANCHOR_MIN_CONFIDENCE` the caller must treat it as orphaned. */
  confidence: number
  element: Element | null
  /** Which rung answered — shown in the pin list so a weak match is visible. */
  how: string
}

/**
 * Anything below this is a guess, and a guess is worse than an orphan.
 *
 * Set at the rung where only geometry agreed: position alone is how you end up
 * commenting on whatever moved into the same place, which on a re-rendered list
 * is a different row with the same shape.
 */
export const ANCHOR_MIN_CONFIDENCE = 0.5

export interface AnchorKit {
  capture(el: Element): PinAnchor
  resolve(anchor: PinAnchor): AnchorMatch
}

/** Build the anchor helpers against one document. */
export function anchorKit(doc: Document): AnchorKit {
  const cssEscape = (value: string) =>
    typeof CSS !== 'undefined' && CSS.escape ? CSS.escape(value) : String(value).replace(/["\\]/g, '\\$&')

  const norm = (text: string) => String(text ?? '').replace(/\s+/g, ' ').trim()

  const clamp = (text: string, max: number) => {
    const value = norm(text)

    return value.length > max ? value.slice(0, max) : value
  }

  const roleOf = (el: Element) => {
    const explicit = el.getAttribute('role')

    if (explicit) {return explicit.trim().toLowerCase()}
    const tag = el.tagName.toLowerCase()

    if (tag === 'a') {return el.hasAttribute('href') ? 'link' : 'generic'}

    if (tag === 'button') {return 'button'}

    if (tag === 'select') {return 'combobox'}

    if (tag === 'textarea') {return 'textbox'}

    if (/^h[1-6]$/.test(tag)) {return 'heading'}

    if (tag === 'img') {return 'img'}

    if (tag === 'input') {
      const type = (el.getAttribute('type') || 'text').toLowerCase()

      if (type === 'checkbox') {return 'checkbox'}

      if (type === 'radio') {return 'radio'}

      if (type === 'submit' || type === 'button' || type === 'reset') {return 'button'}

      return 'textbox'
    }

    return tag
  }

  const labelOf = (el: Element) => {
    const aria = el.getAttribute('aria-label')

    if (aria) {return clamp(aria, 120)}
    const alt = el.getAttribute('alt')

    if (alt) {return clamp(alt, 120)}
    const placeholder = el.getAttribute('placeholder')

    if (placeholder) {return clamp(placeholder, 120)}
    const title = el.getAttribute('title')

    if (title) {return clamp(title, 120)}

    return clamp(el.textContent || '', 120)
  }

  /**
   * A stable-ish structural path.
   *
   * Stops at the first ancestor carrying an id, because a path rooted at a
   * named container survives changes anywhere above it — which is most of what
   * a layout change actually is.
   */
  const pathOf = (el: Element) => {
    const parts: string[] = []
    let node: Element | null = el
    let depth = 0

    while (node && node.nodeType === 1 && node !== doc.documentElement && depth < 24) {
      const tag = node.tagName.toLowerCase()

      if (node.id) {
        parts.unshift('#' + cssEscape(node.id))

        break
      }

      const parent: Element | null = node.parentElement

      if (!parent) {
        parts.unshift(tag)

        break
      }

      let ordinal = 1

      for (const sibling of Array.from(parent.children)) {
        if (sibling === node) {break}

        if (sibling.tagName === node.tagName) {ordinal += 1}
      }

      parts.unshift(ordinal > 1 ? tag + ':nth-of-type(' + ordinal + ')' : tag)
      node = parent
      depth += 1
    }

    return parts.join('>')
  }

  const selectorOf = (el: Element) => {
    if (el.id) {return '#' + cssEscape(el.id)}
    const testid = el.getAttribute('data-testid')

    if (testid) {return '[data-testid="' + String(testid).replace(/"/g, '\\"') + '"]'}

    return ''
  }

  const rectOf = (el: Element) => {
    const box = el.getBoundingClientRect()
    const view = doc.defaultView
    const scrollX = view ? view.scrollX : 0
    const scrollY = view ? view.scrollY : 0
    // Fractions of the document box, so the same pin lands in the same place at
    // a different window size — an absolute pixel rect would drift on every
    // responsive breakpoint.
    const width = Math.max(1, doc.documentElement.scrollWidth)
    const height = Math.max(1, doc.documentElement.scrollHeight)

    return {
      h: box.height / height,
      w: box.width / width,
      x: (box.left + scrollX) / width,
      y: (box.top + scrollY) / height
    }
  }

  const ordinalOf = (el: Element, role: string, label: string) => {
    const peers = Array.from(doc.querySelectorAll<Element>('*')).filter(
      candidate => roleOf(candidate) === role && labelOf(candidate) === label
    )

    const index = peers.indexOf(el)

    return index === -1 ? 0 : index
  }

  const capture = (el: Element): PinAnchor => {
    const role = roleOf(el)
    const label = labelOf(el)

    return {
      label,
      ordinal: ordinalOf(el, role, label),
      path: pathOf(el),
      rect: rectOf(el),
      role,
      selector: selectorOf(el),
      text: clamp(el.textContent || '', 200)
    }
  }

  const query = (selector: string): Element | null => {
    if (!selector) {return null}

    try {
      return doc.querySelector(selector)
    } catch {
      // A path built from an id containing characters CSS.escape did not cover
      // throws rather than missing. A throw here is a miss, not a crash.
      return null
    }
  }

  /** Do two labels share at least half their words? Ported in spirit from
   *  identity.ts's `alike`, for labels that gained a count or a suffix. */
  const alike = (a: string, b: string) => {
    if (!a || !b) {return false}

    if (a === b) {return true}
    const left = a.toLowerCase().split(/\s+/).filter(Boolean)
    const right = new Set(b.toLowerCase().split(/\s+/).filter(Boolean))

    if (!left.length) {return false}
    let shared = 0

    for (const word of left) {if (right.has(word)) {shared += 1}}

    return shared / left.length >= 0.5
  }

  const distance = (el: Element, anchor: PinAnchor) => {
    const now = rectOf(el)

    return Math.hypot(now.x - anchor.rect.x, now.y - anchor.rect.y)
  }

  const resolve = (anchor: PinAnchor): AnchorMatch => {
    // 1. The page's own identity. If it kept its id, it is the same element and
    //    nothing else needs checking.
    const bySelector = query(anchor.selector)

    if (bySelector) {return { confidence: 1, element: bySelector, how: 'selector' }}

    // 2. Structure plus agreement on what it is. A path alone is not enough —
    //    the same slot in a re-rendered list is a different row.
    const byPath = query(anchor.path)

    if (byPath) {
      const sameRole = roleOf(byPath) === anchor.role
      const sameLabel = labelOf(byPath) === anchor.label

      if (sameRole && sameLabel) {return { confidence: 0.95, element: byPath, how: 'path' }}

      if (sameRole && alike(labelOf(byPath), anchor.label)) {
        return { confidence: 0.8, element: byPath, how: 'path+label' }
      }
    }

    // 3. Role and label, which is what a human would use. Unique is strong;
    //    ambiguous falls through to the ordinal it had when it was pinned.
    const named = Array.from(doc.querySelectorAll<Element>('*')).filter(
      candidate => roleOf(candidate) === anchor.role && labelOf(candidate) === anchor.label
    )

    if (named.length === 1) {return { confidence: 0.9, element: named[0], how: 'role+label' }}

    if (named.length > 1) {
      const byOrdinal = named[anchor.ordinal]

      if (byOrdinal) {return { confidence: 0.7, element: byOrdinal, how: 'role+label+ordinal' }}

      // Ambiguous and the ordinal is gone: prefer the nearest to where it was,
      // which among identical siblings is the only signal left that means
      // anything.
      const nearest = named
        .map(candidate => ({ candidate, gap: distance(candidate, anchor) }))
        .sort((a, b) => a.gap - b.gap)[0]

      if (nearest) {return { confidence: 0.6, element: nearest.candidate, how: 'role+label+nearest' }}
    }

    // 4. Text, for pages that label nothing. Only when it is unique.
    if (anchor.text) {
      const byText = Array.from(doc.querySelectorAll<Element>('*')).filter(
        candidate =>
          candidate.children.length === 0 && clamp(candidate.textContent || '', 200) === anchor.text
      )

      if (byText.length === 1) {return { confidence: 0.65, element: byText[0], how: 'text' }}
    }

    // Nothing agreed. Geometry alone is exactly the guess this ladder exists to
    // avoid, so stop here and let the caller mark the pin orphaned.
    return { confidence: 0, element: null, how: 'orphaned' }
  }

  return { capture, resolve }
}
