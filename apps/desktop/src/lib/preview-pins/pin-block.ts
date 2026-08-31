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

import type { PinShot, PreviewPin } from './types'

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

/** The open pins of a payload, oldest first. Shared by the block and the
 *  image ordering so the two can never disagree about what "image 2" is. */
export function openPins(detail: string): PreviewPin[] {
  let pins: PreviewPin[]

  try {
    const parsed = JSON.parse(detail)
    pins = Array.isArray(parsed) ? parsed : parsed?.pins

    if (!Array.isArray(pins)) {return []}
  } catch {
    return []
  }

  return pins.filter(pin => pin && !pin.resolved).sort((a, b) => a.createdAt - b.createdAt)
}

/**
 * Every attached image, numbered the way the block numbers them.
 *
 * The images reach the model as ordinary image attachments, which arrive with
 * no idea which comment they belong to. The block says "[image 2]" on the pin
 * that owns it, so this walk and the one that builds those attachments have to
 * produce the same order — hence one function, used by both.
 */
export function orderedShots(pins: PreviewPin[]): { pin: PreviewPin; shot: PinShot }[] {
  return pins.flatMap(pin => (pin.shots ?? []).map(shot => ({ pin, shot })))
}

/** Group pins by the page they were placed on, keeping first-seen page order. */
function byPage(pins: PreviewPin[]): { pins: PreviewPin[]; url: string }[] {
  const groups: { pins: PreviewPin[]; url: string }[] = []

  for (const pin of pins) {
    const url = pin.pageUrl || ''
    const existing = groups.find(group => group.url === url)

    if (existing) {existing.pins.push(pin)}
    else {groups.push({ pins: [pin], url })}
  }

  return groups
}

/**
 * Render open pins as one fenced block.
 *
 * Resolved pins are dropped: "address my comments" means the open ones, and
 * shipping resolved ones back is how an agent ends up redoing work the user
 * already accepted.
 *
 * A review that stayed on one page keeps the page on the fence line, which is
 * the shortest thing that can be said. A review that walked across pages grows
 * a heading per page instead — numbering still runs straight through, because
 * the numbers are also the image numbers.
 *
 * Returns null when there is nothing to say, so the caller can fall through to
 * the attachment's own ref text exactly like `reviewCommentBlock` does on a
 * malformed payload.
 */
export function pinCommentBlock(detail: string): null | string {
  const open = openPins(detail)

  if (!open.length) {return null}

  let counter = 0
  let image = 0

  const render = (pin: PreviewPin) => {
    counter += 1
    const shots = pin.shots ?? []
    // Name the images on the line that owns them. Without this the model gets
    // N pictures and no way to tell which sentence each one illustrates.
    const marks = shots.map(() => `[image ${(image += 1)}]`).join(' ')
    const head = `${counter}. ${describeTarget(pin)}${marks ? ` ${marks}` : ''}`
    // An orphaned pin still carries the user's sentence, but the agent has to
    // know the address is stale or it will trust a selector that no longer
    // resolves.
    const stale = pin.orphaned ? '\n   (this element is no longer on the page — locate it by description)' : ''
    const comment = (pin.comment || '').trim()

    return `${head}${stale}\n   ${comment || '(no comment)'}`
  }

  const groups = byPage(open)

  if (groups.length === 1) {
    const url = groups[0].url

    return `\`\`\`preview-comments${url ? ` ${url}` : ''}\n${open.map(render).join('\n\n')}\n\`\`\``
  }

  const sections = groups.map(
    group => `${group.url || '(unknown page)'}\n${group.pins.map(render).join('\n\n')}`
  )

  return `\`\`\`preview-comments\n${sections.join('\n\n')}\n\`\`\``
}

/** Chip label: "3 comments" reads better than a truncated first comment. */
export function pinAttachmentLabel(pins: PreviewPin[]): string {
  const open = pins.filter(pin => pin && !pin.resolved).length

  return open === 1 ? '1 comment' : `${open} comments`
}
