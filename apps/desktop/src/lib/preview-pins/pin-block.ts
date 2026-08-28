/**
 * PIN BLOCK — how a batch of pins reaches the model.
 *
 * This is the send-time expansion of a `pins` composer attachment, the exact
 * counterpart of `reviewCommentBlock` in chat-runtime.ts: the chip is a
 * summary, and the real payload only materialises when the message is sent.
 *
 * Deliberately plain text in a fenced block rather than a tool call or a JSON
 * envelope. The RFC on #90654 makes the point for element context and it holds
 * here: "no fabricated structure crosses into the model; it's plain context
 * text." A pin is a human sentence about a place on a page, and the model
 * already reads prose about code perfectly well.
 *
 * Pure and dependency-free so it can be unit-tested without a page.
 */

import type { PreviewPin } from './types'

/** Round a document fraction to something readable in a prompt. */
function percent(value: number): string {
  return `${Math.round(value * 100)}%`
}

function describeTarget(pin: PreviewPin): string {
  if (pin.kind === 'region' && pin.region) {
    return `region at ${percent(pin.region.x)},${percent(pin.region.y)} sized ${percent(pin.region.w)}×${percent(pin.region.h)}`
  }

  const anchor = pin.anchor

  if (!anchor) {return pin.target || 'unknown target'}
  const name = anchor.label ? `${anchor.role} "${anchor.label}"` : anchor.role
  // The selector is what the agent will actually grep for, so it goes in when
  // the page offered one. The path is the fallback and is noisier, so it only
  // appears when there is no selector.
  const where = anchor.selector || anchor.path

  return where ? `${name} — ${where}` : name
}

/**
 * Render open pins as one fenced block.
 *
 * Resolved pins are dropped: "address my comments" means the open ones, and
 * shipping resolved ones back is how an agent ends up redoing work the user
 * already accepted.
 *
 * Returns null when there is nothing to say, so the caller can fall through to
 * the attachment's own ref text exactly like `reviewCommentBlock` does on a
 * malformed payload.
 */
export function pinCommentBlock(detail: string): null | string {
  let pins: PreviewPin[]

  try {
    const parsed = JSON.parse(detail)
    pins = Array.isArray(parsed) ? parsed : parsed?.pins

    if (!Array.isArray(pins)) {return null}
  } catch {
    return null
  }

  const open = pins.filter(pin => pin && !pin.resolved)

  if (!open.length) {return null}

  const url = open.find(pin => pin.pageUrl)?.pageUrl ?? ''

  const lines = open
    .slice()
    .sort((a, b) => a.createdAt - b.createdAt)
    .map((pin, index) => {
      const head = `${index + 1}. ${describeTarget(pin)}`
      // An orphaned pin still carries the user's sentence, but the agent has to
      // know the address is stale or it will trust a selector that no longer
      // resolves.
      const stale = pin.orphaned ? '\n   (this element is no longer on the page — locate it by description)' : ''
      const comment = (pin.comment || '').trim()

      return `${head}${stale}\n   ${comment || '(no comment)'}`
    })

  return `\`\`\`preview-comments${url ? ` ${url}` : ''}\n${lines.join('\n\n')}\n\`\`\``
}

/** Chip label: "3 comments" reads better than a truncated first comment. */
export function pinAttachmentLabel(pins: PreviewPin[]): string {
  const open = pins.filter(pin => pin && !pin.resolved).length

  return open === 1 ? '1 comment' : `${open} comments`
}
