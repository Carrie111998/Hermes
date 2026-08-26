// capture-root.tsx — the hidden subtitle-capture worker window (?win=subcap).
//
// Not a UI: this window is never shown. It exists because screen capture
// (getDisplayMedia) and canvas cropping are renderer capabilities — main
// cannot hold a MediaStream. The loop: play the display stream into an
// offscreen <video>, sample the configured band at sample_hz, hash the
// bright-pixel mask (capture-lib.ts), and ship a PNG of the crop to main only
// when the mask says the text changed. Main relays to the backend and paints;
// nothing here decides what a subtitle says.

import { brightMaskHash, cropRectFor, hammingDistance, SAME_TEXT_MAX_DISTANCE, shipSize } from './capture-lib'

interface CaptureConfig {
  epoch: number
  fractions: { height: number; left: number; top: number; width: number }
  sample_hz: number
}

// Successive identical-hash frames still re-send occasionally so main can
// refresh the overlay hold timer through the same code path (a paused movie
// must keep its translated line up).
const KEEPALIVE_MS = 8000

function isConfig(value: unknown): value is CaptureConfig {
  const raw = (value ?? {}) as Record<string, unknown>
  const fractions = (raw.fractions ?? {}) as Record<string, unknown>

  return (
    typeof raw.epoch === 'number' &&
    typeof raw.sample_hz === 'number' &&
    ['left', 'top', 'width', 'height'].every(key => typeof fractions[key] === 'number')
  )
}

async function runCaptureLoop(): Promise<void> {
  const bridge = window.hermesDesktop?.subtitleCapture

  if (!bridge) {
    return
  }

  let config: CaptureConfig | null = null
  let lastHash = ''
  let lastSentAt = 0
  let timer: number | null = null

  const video = document.createElement('video')

  video.muted = true

  const cropCanvas = document.createElement('canvas')
  const cropContext = cropCanvas.getContext('2d', { willReadFrequently: true })

  if (!cropContext) {
    return
  }

  const tick = () => {
    if (!config || video.readyState < 2 || video.videoWidth <= 0) {
      return
    }

    const crop = cropRectFor(config.fractions, video.videoWidth, video.videoHeight)

    if (!crop) {
      return
    }

    const target = shipSize(crop)

    if (cropCanvas.width !== target.width || cropCanvas.height !== target.height) {
      cropCanvas.width = target.width
      cropCanvas.height = target.height
    }

    cropContext.drawImage(video, crop.x, crop.y, crop.width, crop.height, 0, 0, target.width, target.height)

    const pixels = cropContext.getImageData(0, 0, target.width, target.height)
    const hash = brightMaskHash(pixels.data, target.width, target.height)
    const now = Date.now()
    const changed = hammingDistance(hash, lastHash) > SAME_TEXT_MAX_DISTANCE

    if (!changed && now - lastSentAt < KEEPALIVE_MS) {
      return
    }

    lastHash = hash
    lastSentAt = now
    bridge.sendFrame({
      data_url: cropCanvas.toDataURL('image/png'),
      epoch: config.epoch,
      height: target.height,
      width: target.width
    })
  }

  const applyConfig = (next: unknown) => {
    if (!isConfig(next)) {
      return
    }

    const restartTimer = !config || config.sample_hz !== next.sample_hz

    config = next
    // A moved band means the old mask is meaningless — force the next tick
    // through to the backend.
    lastHash = ''
    lastSentAt = 0

    if (restartTimer) {
      if (timer !== null) {
        window.clearInterval(timer)
      }

      timer = window.setInterval(tick, Math.round(1000 / Math.max(1, next.sample_hz)))
    }
  }

  bridge.onConfig(applyConfig)
  applyConfig(await bridge.getConfig().catch(() => null))

  // The display-media request resolves through main's handler with the
  // session display's screen source — no picker exists on this path.
  const stream = await navigator.mediaDevices.getDisplayMedia({ audio: false, video: true })

  video.srcObject = stream
  await video.play()
}

export function mountSubtitleCapture(): void {
  document.title = 'Hermes Subtitle Capture'
  void runCaptureLoop().catch(error => {
    // Surfaced through the renderer console capture main attaches to this
    // window — there is no UI to show it in.
    console.error('[subtitle-capture] loop failed:', error)
  })
}
