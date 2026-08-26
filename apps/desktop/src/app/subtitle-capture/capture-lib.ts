// capture-lib.ts — pure frame logic for the hidden subtitle-capture window.
//
// The loop samples the subtitle band a few times a second, but only a frame
// whose TEXT plausibly changed is worth shipping to the backend. Raw
// pixel-hashing would fire on every tick — the band contains moving video —
// so the change signal is a hash of the band's BRIGHT-PIXEL MASK: rendered
// subtitle text is high-luminance and positionally stable, scene motion
// behind it rarely produces a stable bright pattern. Pure functions, unit
// tested; the DOM/stream glue lives in capture-root.tsx.

/** Hash grid: 16x8 cells = 128 bits, packed into 32 hex chars. */
export const HASH_WIDTH = 16
export const HASH_HEIGHT = 8

/** Luma threshold (0-255) above which a pixel counts as "bright" — subtitle
 *  text is white or near-white in every mainstream player. */
export const BRIGHT_LUMA = 200

/** A cell is "on" when at least this share of its pixels are bright. Filters
 *  the odd bright speck while keeping thin glyph strokes. */
const CELL_ON_SHARE = 0.08

/** Hamming distance at or below this is "same text" — antialiasing wobble and
 *  compression shimmer flip a few cells frame to frame. */
export const SAME_TEXT_MAX_DISTANCE = 6

/** Bright-mask hash of an RGBA buffer (any size). Cells are averaged over
 *  their pixel block, so callers can pass the full-resolution band directly. */
export function brightMaskHash(rgba: Uint8ClampedArray, width: number, height: number): string {
  const bits: number[] = []

  for (let cellY = 0; cellY < HASH_HEIGHT; cellY += 1) {
    for (let cellX = 0; cellX < HASH_WIDTH; cellX += 1) {
      const x0 = Math.floor((cellX * width) / HASH_WIDTH)
      const x1 = Math.max(x0 + 1, Math.floor(((cellX + 1) * width) / HASH_WIDTH))
      const y0 = Math.floor((cellY * height) / HASH_HEIGHT)
      const y1 = Math.max(y0 + 1, Math.floor(((cellY + 1) * height) / HASH_HEIGHT))

      let bright = 0
      let total = 0

      for (let y = y0; y < y1; y += 1) {
        for (let x = x0; x < x1; x += 1) {
          const offset = (y * width + x) * 4
          // Rec. 601 luma, integer-ish weights.
          const luma = 0.299 * rgba[offset] + 0.587 * rgba[offset + 1] + 0.114 * rgba[offset + 2]

          if (luma >= BRIGHT_LUMA) {
            bright += 1
          }

          total += 1
        }
      }

      bits.push(bright / total >= CELL_ON_SHARE ? 1 : 0)
    }
  }

  let hex = ''

  for (let index = 0; index < bits.length; index += 4) {
    hex += ((bits[index] << 3) | (bits[index + 1] << 2) | (bits[index + 2] << 1) | bits[index + 3]).toString(16)
  }

  return hex
}

export function hammingDistance(a: string, b: string): number {
  if (a.length !== b.length) {
    return Math.max(a.length, b.length) * 4
  }

  let distance = 0

  for (let index = 0; index < a.length; index += 1) {
    let xor = parseInt(a[index], 16) ^ parseInt(b[index], 16)

    while (xor) {
      distance += xor & 1
      xor >>= 1
    }
  }

  return distance
}

export interface BandFractions {
  height: number
  left: number
  top: number
  width: number
}

export interface PixelRect {
  height: number
  width: number
  x: number
  y: number
}

/** Band fractions × live video dimensions → integer crop rect, clamped inside
 *  the frame. Returns null when the result has no usable area. */
export function cropRectFor(fractions: BandFractions, videoWidth: number, videoHeight: number): PixelRect | null {
  if (videoWidth <= 0 || videoHeight <= 0) {
    return null
  }

  const x = Math.max(0, Math.round(fractions.left * videoWidth))
  const y = Math.max(0, Math.round(fractions.top * videoHeight))
  const width = Math.min(videoWidth - x, Math.round(fractions.width * videoWidth))
  const height = Math.min(videoHeight - y, Math.round(fractions.height * videoHeight))

  if (width < 8 || height < 8) {
    return null
  }

  return { height, width, x, y }
}

/** Ship size for a crop: capped width keeps the PNG payload and the OCR input
 *  small — the spike measured identical accuracy at 1280 vs full Retina. */
export const SHIP_MAX_WIDTH = 1280

export function shipSize(crop: PixelRect): { height: number; width: number } {
  if (crop.width <= SHIP_MAX_WIDTH) {
    return { height: crop.height, width: crop.width }
  }

  const scale = SHIP_MAX_WIDTH / crop.width

  return { height: Math.max(8, Math.round(crop.height * scale)), width: SHIP_MAX_WIDTH }
}
