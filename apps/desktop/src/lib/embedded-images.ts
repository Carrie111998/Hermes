const EMBEDDED_IMAGE_RE =
  /(\{\s*"type"\s*:\s*"image_url"\s*,\s*"image_url"\s*:\s*\{\s*"url"\s*:\s*")?(data:image\/[\w.+-]+;base64,[A-Za-z0-9+/=]{64,})("\s*\}\s*\})?/g

const DATA_URL_RE = /^data:([\w./+-]+);base64,(.*)$/i

export const DATA_IMAGE_URL_RE = /^data:image\/[\w.+-]+;base64,/i

export interface EmbeddedImageExtraction {
  cleanedText: string
  images: string[]
}

export function dataUrlToBlob(dataUrl: string): Blob | null {
  const match = DATA_URL_RE.exec(dataUrl.trim())

  if (!match) {
    return null
  }

  try {
    const bytes = atob(match[2])
    const buffer = new Uint8Array(bytes.length)

    for (let i = 0; i < bytes.length; i += 1) {
      buffer[i] = bytes.charCodeAt(i)
    }

    return new Blob([buffer], { type: match[1] })
  } catch {
    return null
  }
}

export function extractEmbeddedImages(text: string): EmbeddedImageExtraction {
  if (!text || !text.includes('data:image/')) {
    return { cleanedText: text, images: [] }
  }

  const images: string[] = []

  const cleanedText = text
    .replace(EMBEDDED_IMAGE_RE, (_match, _open, dataUrl: string) => {
      images.push(dataUrl)

      return ''
    })
    .replace(/[ \t]+\n/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()

  return { cleanedText, images }
}

export function embeddedImageUrls(text: string): string[] {
  return extractEmbeddedImages(text).images
}

export function textWithoutEmbeddedImages(text: string): string {
  return extractEmbeddedImages(text).cleanedText
}

// The gateway persists attached images as `@image:<path>` directive lines
// (see tui_gateway/server.py's persist-time rewrite), prepended before the
// user's own text. The composer's own optimistic/local turn never carries
// this prefix — it keeps the attachment as separate `attachmentRefs`
// metadata, not inline text. The turn-equality comparisons in
// preserveLocalPendingTurnMessages / appendLiveSessionProjection strip ALL
// reference-directive lines (not just images) via
// `textWithoutReferenceLines` in components/assistant-ui/reference-kinds.ts;
// IMAGE_REF_LINE_RE remains here for extractImageRefs below, which moves the
// image directives into attachmentRefs metadata.
const IMAGE_REF_LINE_RE = /^@image:[^\n]*\n?/gm

// Same directive lines as IMAGE_REF_LINE_RE, but keeps them instead of
// discarding — used when converting persisted server messages into
// ChatMessage/ThreadMessageLike shape, where `@image:<path>` refs need to
// move from inline text into the `attachmentRefs` metadata field (mirroring
// how the local optimistic composer represents attachments) rather than stay
// embedded in the bubble's clamped text body, where a large inline thumbnail
// pushes the caption text out of the clamp's visible area.
// Native-vision turns are stored as a parts list, which the session store
// flattens by replacing each image part with a literal `[screenshot]` line. The
// `@image:` ref describes that same attachment, so keeping both renders the
// placeholder as stray text under the thumbnail. Drop it only when a ref was
// actually lifted, so a `[screenshot]` in a message without attachments stays.
const SCREENSHOT_PLACEHOLDER_LINE_RE = /^\[screenshot\]\n?/gm

export function extractImageRefs(text: string): { cleanedText: string; refs: string[] } {
  const refs: string[] = []

  let cleanedText = text.replace(IMAGE_REF_LINE_RE, match => {
    refs.push(match.trim())

    return ''
  })

  if (refs.length) {
    cleanedText = cleanedText.replace(SCREENSHOT_PLACEHOLDER_LINE_RE, '')
  }

  return { cleanedText: cleanedText.trim(), refs }
}
