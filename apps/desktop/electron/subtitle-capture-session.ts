// subtitle-capture-session.ts — the live-subtitle session (Electron main).
//
// One session at a time: a hidden worker window screen-captures the display,
// crops the subtitle band of the target window at a few Hz, and ships changed
// crops here; main relays each crop to the backend (`/api/subtitles/process`,
// OCR + translation) and paints the result over the original line through the
// screen-annotation overlay's `subtitles` channel. The agent starts and stops
// the session; nothing in the per-line path touches a model conversation.
//
// All geometry lives in subtitle-capture.ts. The target window is re-resolved
// on a slow poll so the band follows a moved or resized player, and the
// session self-stops when the window disappears or the backend stays broken.

import fs from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

import { app, BrowserWindow, desktopCapturer, ipcMain, screen, session, webContents } from 'electron'

import { attachRendererConsoleCapture } from './renderer-log'
import { type AnnotationBounds, resolveAnnotationWindow } from './screen-annotations'
import type { ScreenAnnotationsController } from './screen-annotations-window'
import {
  bandFractions,
  clampBandFraction,
  clampSampleHz,
  type SubtitleBandFractions,
  subtitleBand,
  subtitleShapes,
  type SubtitleTextBox
} from './subtitle-capture'
import { type EnumeratedWindow, enumerateWindowsFrontToBack, enumerationFailed, enumerationFailureNote } from './window-below'
import { installWindowRendererLifecycle } from './window-renderer-lifecycle'

interface SubtitleCaptureOptions {
  annotations: ScreenAnnotationsController
  devServer?: string
  loadWindowUrl: (window: BrowserWindow, url: string, label: string) => void
  log: (message: string) => void
  /** Authenticated POST to the backend that owns OCR + translation. */
  postToBackend: (path: string, body: Record<string, unknown>) => Promise<Record<string, unknown>>
  preloadPath: string
  rendererIndex: () => string
  titlesAvailable: () => boolean
  wireWindow: (window: BrowserWindow) => void
}

export interface SubtitleCaptureController {
  close(): void
  control(request: unknown, senderBounds: AnnotationBounds | null): Promise<Record<string, unknown>>
  getRendererConfig(): Record<string, unknown> | null
  onFrame(senderId: number, payload: unknown): void
}

interface SubtitleSession {
  band: AnnotationBounds
  bandFraction: number
  consecutiveBackendErrors: number
  debugFrameSaved: boolean
  display: AnnotationBounds
  displayId: number
  epoch: number
  fractions: SubtitleBandFractions
  language: string
  lastLatencyMs: number
  lastShapesAt: number
  lastSourceText: string
  linesDrawn: number
  sampleHz: number
  startedAt: number
  streamId: string
  targetSpec?: string
  windowId: number
  windowRef: { app: string; title: string }
}

// The band follows the player: a moved/resized window re-anchors the crop, a
// vanished one (2 consecutive misses, matching hud-game-overlay's tolerance
// for transient enumeration failures) stops the session.
const FOLLOW_INTERVAL_MS = 5000
const FOLLOW_MISSES_BEFORE_STOP = 2

// A backend that fails this many times in a row is not coming back this
// session (dead OCR dep, unreachable remote); stop instead of spinning.
const BACKEND_ERRORS_BEFORE_STOP = 5

const SUPPORTED_ACTIONS = ['start', 'status', 'stop'] as const

const asBounds = (value: unknown): AnnotationBounds | null => {
  const raw = (value ?? {}) as Record<string, unknown>
  const nums = [raw.x, raw.y, raw.width, raw.height].map(entry =>
    typeof entry === 'number' && Number.isFinite(entry) ? entry : null
  )

  return nums.every(entry => entry !== null)
    ? { height: nums[3] as number, width: nums[2] as number, x: nums[0] as number, y: nums[1] as number }
    : null
}

export function createSubtitleCaptureController(options: SubtitleCaptureOptions): SubtitleCaptureController {
  const { annotations, devServer, loadWindowUrl, log, postToBackend, preloadPath, rendererIndex, titlesAvailable, wireWindow } = options

  let captureWindow: BrowserWindow | null = null
  let current: SubtitleSession | null = null
  let followTimer: NodeJS.Timeout | null = null
  let followMisses = 0
  let followInFlight = false
  // Stale-result guard: only the newest submitted frame may paint. A slow
  // translation finishing after a newer line landed must be dropped, not drawn.
  let frameGeneration = 0
  let displayMediaHandlerInstalled = false

  const url = () => {
    if (devServer) {
      return `${devServer.endsWith('/') ? devServer.slice(0, -1) : devServer}/?win=subcap#/`
    }

    return `${pathToFileURL(rendererIndex()).toString()}?win=subcap#/`
  }

  // getDisplayMedia in the hidden worker resolves through this handler: the
  // worker gets the screen source for the session's display, every other
  // frame in the app gets a refusal — nothing else in Hermes screen-captures,
  // and an in-app page must never be able to open a stream of the desktop.
  const installDisplayMediaHandler = () => {
    if (displayMediaHandlerInstalled) {
      return
    }

    displayMediaHandlerInstalled = true

    session.defaultSession.setDisplayMediaRequestHandler((request, callback) => {
      const requester = request.frame ? webContents.fromFrame(request.frame) : null
      const allowed =
        requester && captureWindow && !captureWindow.isDestroyed() && requester.id === captureWindow.webContents.id

      if (!allowed || !current) {
        callback({})

        return
      }

      const wantedDisplayId = String(current.displayId)

      desktopCapturer
        .getSources({ fetchWindowIcons: false, thumbnailSize: { height: 0, width: 0 }, types: ['screen'] })
        .then(sources => {
          const match = sources.find(source => source.display_id === wantedDisplayId) ?? sources[0]

          if (match) {
            callback({ video: match })
          } else {
            callback({})
          }
        })
        .catch(error => {
          log(`subtitles: screen source lookup failed: ${error instanceof Error ? error.message : String(error)}`)
          callback({})
        })
    })
  }

  const rendererConfig = (): Record<string, unknown> | null => {
    if (!current) {
      return null
    }

    return {
      epoch: current.epoch,
      fractions: current.fractions,
      sample_hz: current.sampleHz
    }
  }

  const pushConfig = () => {
    if (captureWindow && !captureWindow.isDestroyed()) {
      captureWindow.webContents.send('hermes:subtitle-capture:config', rendererConfig())
    }
  }

  const spawnCaptureWindow = () => {
    // A worker, not a surface: never shown, never focused. Background
    // throttling must stay off — the whole window exists to run a timer while
    // hidden.
    const next = new BrowserWindow({
      focusable: false,
      frame: false,
      height: 180,
      show: false,
      skipTaskbar: true,
      webPreferences: {
        backgroundThrottling: false,
        contextIsolation: true,
        devTools: true,
        nodeIntegration: false,
        preload: preloadPath,
        sandbox: true
      },
      width: 320
    })

    wireWindow(next)
    installWindowRendererLifecycle(next, { callbacks: { log }, kind: 'subtitle-capture' })
    attachRendererConsoleCapture(next, 'subtitle-capture', log)

    // Push on load AND answer the renderer's pull — its chunk is lazy, so the
    // listener can attach after did-finish-load already fired.
    next.webContents.on('did-finish-load', pushConfig)

    next.on('closed', () => {
      if (captureWindow === next) {
        captureWindow = null
      }
    })

    loadWindowUrl(next, url(), 'Subtitle capture')

    return next
  }

  const resolveTarget = async (
    spec: string | undefined,
    senderBounds: AnnotationBounds | null
  ): Promise<{ error: string; window?: undefined } | { error?: undefined; window: EnumeratedWindow }> => {
    const windows = await enumerateWindowsFrontToBack(process.pid, titlesAvailable())

    if (enumerationFailed(windows)) {
      return { error: enumerationFailureNote(process.platform, process.env, windows.reason) }
    }

    return resolveAnnotationWindow(windows, process.pid, senderBounds, spec)
  }

  const stop = (reason?: string): Record<string, unknown> => {
    const wasRunning = current !== null
    const stats = current
      ? { lines_translated: current.linesDrawn, ran_seconds: Math.round((Date.now() - current.startedAt) / 1000) }
      : {}

    if (followTimer) {
      clearInterval(followTimer)
      followTimer = null
    }

    followMisses = 0
    frameGeneration += 1
    current = null

    if (captureWindow && !captureWindow.isDestroyed()) {
      captureWindow.close()
    }

    captureWindow = null
    annotations.clearChannel('subtitles')

    if (reason) {
      log(`subtitles: stopped — ${reason}`)
    }

    return { stopped: wasRunning, success: true, ...(reason ? { reason } : {}), ...stats }
  }

  const followTick = async () => {
    if (!current || followInFlight) {
      return
    }

    followInFlight = true

    try {
      const windows = await enumerateWindowsFrontToBack(process.pid, titlesAvailable())

      if (!current || enumerationFailed(windows)) {
        return
      }

      const match =
        windows.find(win => win.id === current!.windowId) ??
        windows.find(
          win =>
            win.app === current!.windowRef.app &&
            win.title === current!.windowRef.title &&
            win.bounds.width > 0 &&
            win.bounds.height > 0
        )

      if (!match || match.bounds.width <= 0 || match.bounds.height <= 0) {
        followMisses += 1

        if (followMisses >= FOLLOW_MISSES_BEFORE_STOP) {
          stop('the target window is gone')
        }

        return
      }

      followMisses = 0

      const display = screen.getDisplayMatching(match.bounds)
      const band = subtitleBand(match.bounds, display.bounds, current.bandFraction)

      if (!band) {
        return
      }

      const moved =
        band.x !== current.band.x ||
        band.y !== current.band.y ||
        band.width !== current.band.width ||
        band.height !== current.band.height ||
        display.id !== current.displayId

      if (!moved) {
        return
      }

      current.band = band
      current.display = display.bounds
      current.displayId = display.id
      current.fractions = bandFractions(band, display.bounds)
      current.windowId = match.id
      current.epoch += 1
      pushConfig()
    } catch (error) {
      log(`subtitles: follow tick failed: ${error instanceof Error ? error.message : String(error)}`)
    } finally {
      followInFlight = false
    }
  }

  const start = async (req: Record<string, unknown>, senderBounds: AnnotationBounds | null) => {
    const language = typeof req.language === 'string' ? req.language.trim() : ''

    if (!language) {
      return { error: "start needs a language (e.g. 'pt', 'es', 'Portuguese').", success: false }
    }

    if (current) {
      stop('replaced by a new start')
    }

    const spec = typeof req.target === 'string' && req.target.trim() ? req.target.trim() : undefined
    const resolved = await resolveTarget(spec, senderBounds)

    if (resolved.error !== undefined) {
      return { error: resolved.error, success: false }
    }

    const target = resolved.window
    const display = screen.getDisplayMatching(target.bounds)
    const fraction = clampBandFraction(typeof req.band_fraction === 'number' ? req.band_fraction : undefined)
    const band = subtitleBand(target.bounds, display.bounds, fraction)

    if (!band) {
      return { error: `The "${target.app}" window is not visibly on any display.`, success: false }
    }

    current = {
      band,
      bandFraction: fraction,
      consecutiveBackendErrors: 0,
      debugFrameSaved: false,
      display: display.bounds,
      displayId: display.id,
      epoch: 1,
      fractions: bandFractions(band, display.bounds),
      language,
      lastLatencyMs: 0,
      lastShapesAt: 0,
      lastSourceText: '',
      linesDrawn: 0,
      sampleHz: clampSampleHz(typeof req.sample_hz === 'number' ? req.sample_hz : undefined),
      startedAt: Date.now(),
      streamId: `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`,
      targetSpec: spec,
      windowId: target.id,
      windowRef: { app: target.app, title: target.title }
    }

    installDisplayMediaHandler()

    if (!captureWindow || captureWindow.isDestroyed()) {
      captureWindow = spawnCaptureWindow()
    } else {
      pushConfig()
    }

    followMisses = 0
    followTimer = setInterval(() => void followTick(), FOLLOW_INTERVAL_MS)

    return {
      band,
      language,
      success: true,
      target: { app: target.app, title: target.title },
      watching: `bottom ${Math.round(fraction * 100)}% of the ${target.app} window`
    }
  }

  const status = (): Record<string, unknown> => {
    if (!current) {
      return { running: false, success: true }
    }

    return {
      band: current.band,
      language: current.language,
      last_latency_ms: current.lastLatencyMs,
      lines_translated: current.linesDrawn,
      running: true,
      running_seconds: Math.round((Date.now() - current.startedAt) / 1000),
      success: true,
      target: current.windowRef
    }
  }

  const control = async (request: unknown, senderBounds: AnnotationBounds | null): Promise<Record<string, unknown>> => {
    const req = (request && typeof request === 'object' ? request : {}) as Record<string, unknown>
    const action = typeof req.action === 'string' ? req.action.trim().toLowerCase() : ''

    if (action === 'start') {
      return start(req, senderBounds)
    }

    if (action === 'stop') {
      return stop()
    }

    if (action === 'status') {
      return status()
    }

    return { error: `action must be one of: ${SUPPORTED_ACTIONS.join(', ')}.`, success: false }
  }

  let lastDrawn: { display: AnnotationBounds; shapes: ReturnType<typeof subtitleShapes> } | null = null

  const redrawLast = () => {
    if (lastDrawn) {
      annotations.setChannelShapes('subtitles', lastDrawn.shapes, lastDrawn.display)
    }
  }

  const asBox = (value: unknown): SubtitleTextBox | null => {
    const bounds = asBounds(value)

    return bounds && bounds.width > 0 && bounds.height > 0 ? bounds : null
  }

  // First crop of every session lands on disk so "is the capture black?"
  // (DRM-protected content) is answerable from a log line instead of a debug
  // build. One frame, temp dir, overwritten per session.
  const saveDebugFrame = (dataUrl: string) => {
    try {
      const base64 = dataUrl.slice(dataUrl.indexOf(',') + 1)
      const file = path.join(app.getPath('temp'), 'hermes-subtitles-first-frame.png')

      fs.writeFileSync(file, Buffer.from(base64, 'base64'))
      log(`subtitles: first captured frame saved to ${file}`)
    } catch {
      // Diagnostics only — never fail the pipeline over it.
    }
  }

  const onFrame = (senderId: number, payload: unknown): void => {
    if (!current || !captureWindow || captureWindow.isDestroyed() || senderId !== captureWindow.webContents.id) {
      return
    }

    const frame = (payload && typeof payload === 'object' ? payload : {}) as Record<string, unknown>
    const dataUrl = typeof frame.data_url === 'string' ? frame.data_url : ''
    const cropWidth = typeof frame.width === 'number' ? frame.width : 0
    const cropHeight = typeof frame.height === 'number' ? frame.height : 0

    if (!dataUrl.startsWith('data:image/png;base64,') || cropWidth <= 0 || cropHeight <= 0 || frame.epoch !== current.epoch) {
      return
    }

    if (!current.debugFrameSaved) {
      current.debugFrameSaved = true
      saveDebugFrame(dataUrl)
    }

    const generation = ++frameGeneration
    const startedAt = Date.now()
    const sessionAtSubmit = current

    void postToBackend('/api/subtitles/process', {
      image_data_url: dataUrl,
      language: sessionAtSubmit.language,
      prev_text: sessionAtSubmit.lastSourceText,
      stream_id: sessionAtSubmit.streamId
    })
      .then(result => {
        if (!current || current !== sessionAtSubmit || generation !== frameGeneration) {
          return
        }

        current.lastLatencyMs = Date.now() - startedAt

        if (!result || result.ok !== true) {
          throw new Error(typeof result?.detail === 'string' ? result.detail : 'backend returned no result')
        }

        current.consecutiveBackendErrors = 0

        // Same line still on screen (background motion tripped the hash):
        // refresh the hold so the cover doesn't blink out mid-line.
        if (result.unchanged === true) {
          if (current.lastShapesAt > 0) {
            redrawLast()
          }

          return
        }

        const sourceText = typeof result.source_text === 'string' ? result.source_text : ''
        const text = typeof result.text === 'string' ? result.text.trim() : ''
        const box = asBox(result.box)

        current.lastSourceText = sourceText

        if (!text || !box) {
          // No subtitle on screen right now.
          lastDrawn = null
          current.lastShapesAt = 0
          annotations.clearChannel('subtitles')

          return
        }

        const shapes = subtitleShapes({
          band: current.band,
          box,
          cropHeight,
          cropWidth,
          display: current.display,
          text
        })

        if (shapes.length === 0) {
          return
        }

        lastDrawn = { display: current.display, shapes }
        current.lastShapesAt = Date.now()
        current.linesDrawn += 1
        annotations.setChannelShapes('subtitles', shapes, current.display)
      })
      .catch(error => {
        if (!current || current !== sessionAtSubmit) {
          return
        }

        current.consecutiveBackendErrors += 1
        log(`subtitles: process failed (${current.consecutiveBackendErrors}): ${error instanceof Error ? error.message : String(error)}`)

        if (current.consecutiveBackendErrors >= BACKEND_ERRORS_BEFORE_STOP) {
          stop('the backend kept failing to OCR/translate frames')
        }
      })
  }

  const close = () => {
    stop()
  }

  return { close, control, getRendererConfig: rendererConfig, onFrame }
}

/**
 * IPC surface. The chat renderer that received the agent's control request
 * forwards it here (its own bounds anchor the default target, matching the
 * annotate tool); the hidden capture renderer pulls its config on mount and
 * streams changed crops up. Frame/config channels only answer the capture
 * window itself.
 */
export function registerSubtitleCaptureIpc(controller: SubtitleCaptureController): void {
  ipcMain.handle('hermes:subtitles:control', async (event, request) => {
    const sender = BrowserWindow.fromWebContents(event.sender)
    const senderBounds = sender && !sender.isDestroyed() ? sender.getBounds() : null

    try {
      return await controller.control(request, senderBounds)
    } catch (error) {
      return { error: error instanceof Error ? error.message : String(error), success: false }
    }
  })

  ipcMain.handle('hermes:subtitle-capture:get', () => controller.getRendererConfig())

  ipcMain.on('hermes:subtitle-capture:frame', (event, payload) => {
    controller.onFrame(event.sender.id, payload)
  })
}
