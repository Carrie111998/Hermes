// screen-annotations-window.ts — the transparent overlay the agent draws on.
//
// One frameless, click-through, always-on-top window covering the display the
// target sits on. It is a pure display surface: never focusable, never
// interactive, hidden from taskbar/Mission Control, and it steps over
// fullscreen Spaces the same way the HUD does (visibleOnFullScreen) — the
// whole point is marking the app the user is playing under the HUD. All
// geometry decisions live in screen-annotations.ts; main.ts owns only the IPC
// registration (see registerScreenAnnotationsIpc call site).

import { pathToFileURL } from 'node:url'

import { BrowserWindow, ipcMain, screen } from 'electron'

import { attachRendererConsoleCapture } from './renderer-log'
import {
  type AnnotationBounds,
  clampAnnotationTtlSeconds,
  isScreenTarget,
  mapAnnotationShapes,
  type MappedAnnotationShape,
  resolveAnnotationWindow
} from './screen-annotations'
import { enumerateWindowsFrontToBack, enumerationFailed, enumerationFailureNote } from './window-below'
import { installWindowRendererLifecycle } from './window-renderer-lifecycle'

interface ScreenAnnotationsOptions {
  devServer?: string
  isMac: boolean
  loadWindowUrl: (window: BrowserWindow, url: string, label: string) => void
  log: (message: string) => void
  preloadPath: string
  rendererIndex: () => string
  /** The macOS Screen Recording grant, so window titles match what
   *  read_window_below reports (true on other platforms, where titles are free). */
  titlesAvailable: () => boolean
  wireWindow: (window: BrowserWindow) => void
}

export interface ScreenAnnotationsController {
  annotate(request: unknown, senderBounds: AnnotationBounds | null): Promise<Record<string, unknown>>
  clear(): void
  close(): void
  getState(): { shapes: MappedAnnotationShape[] }
}

const asPositive = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : null

export function createScreenAnnotationsController({
  devServer,
  isMac,
  loadWindowUrl,
  log,
  preloadPath,
  rendererIndex,
  titlesAvailable,
  wireWindow
}: ScreenAnnotationsOptions): ScreenAnnotationsController {
  let window: BrowserWindow | null = null
  let shapes: MappedAnnotationShape[] = []
  let ttlTimer: NodeJS.Timeout | null = null

  // Draw/clear ordering guard. `annotate` suspends at the window-enumeration
  // await, and requests can overlap (the main window and the HUD each run
  // their own session), so an older draw could resume after a newer draw or
  // clear and overwrite it with stale shapes and a stale TTL. Every request
  // takes a generation at entry; the shared state only accepts the latest.
  let requestGeneration = 0

  const url = () => {
    if (devServer) {
      return `${devServer.endsWith('/') ? devServer.slice(0, -1) : devServer}/?win=annotate#/`
    }

    return `${pathToFileURL(rendererIndex()).toString()}?win=annotate#/`
  }

  const sendState = () => {
    if (window && !window.isDestroyed()) {
      window.webContents.send('hermes:screen-annotations:state', { shapes })
    }
  }

  const disarmTtl = () => {
    if (ttlTimer) {
      clearTimeout(ttlTimer)
      ttlTimer = null
    }
  }

  const spawn = (bounds: AnnotationBounds) => {
    const next = new BrowserWindow({
      ...bounds,
      alwaysOnTop: true,
      backgroundColor: '#00000000',
      // Non-activating: a pointer overlay must never take the keyboard or
      // become the app's switcher anchor (same rationale as the pet overlay).
      focusable: false,
      frame: false,
      fullscreenable: false,
      hasShadow: false,
      hiddenInMissionControl: isMac,
      maximizable: false,
      minimizable: false,
      movable: false,
      resizable: false,
      // Full-display drawing surface: a rounded corner would clip a mark
      // drawn at the screen's edge.
      roundedCorners: false,
      show: false,
      skipTaskbar: !isMac,
      transparent: true,
      type: isMac ? 'panel' : undefined,
      webPreferences: {
        backgroundThrottling: false,
        contextIsolation: true,
        devTools: true,
        nodeIntegration: false,
        preload: preloadPath,
        sandbox: true
      }
    })

    // `floating` + panel is the macOS NSPanel path the HUD/pet overlay use;
    // `screen-saver` elsewhere so the marks clear borderless-fullscreen games.
    next.setAlwaysOnTop(true, isMac ? 'floating' : 'screen-saver')
    next.setHiddenInMissionControl?.(true)

    // Pure display surface — every click belongs to the app underneath. No
    // `forward`: the overlay has no hover logic to feed. On X11 ignore-mouse
    // is a one-way door (hud-ipc.ts), which is fine here: it never reopens.
    next.setIgnoreMouseEvents(true)

    try {
      next.setVisibleOnAllWorkspaces(true, {
        skipTransformProcessType: true,
        visibleOnFullScreen: true
      })
    } catch {
      // Not supported everywhere — best effort.
    }

    wireWindow(next)

    // Log-only renderer lifecycle (#81290): the overlay is an ephemeral
    // auxiliary surface; a dead renderer should be diagnosable, not
    // resurrected — the next draw respawns it anyway.
    installWindowRendererLifecycle(next, { callbacks: { log }, kind: 'annotations' })
    attachRendererConsoleCapture(next, 'annotations', log)

    // Push on load AND let the renderer pull (hermes:screen-annotations:get):
    // its mount is a lazy chunk, so the listener can attach after
    // did-finish-load already fired.
    next.webContents.on('did-finish-load', sendState)

    next.once('ready-to-show', () => {
      if (!next.isDestroyed() && shapes.length > 0) {
        next.showInactive()
      }
    })

    next.on('closed', () => {
      if (window === next) {
        window = null
      }
    })

    loadWindowUrl(next, url(), 'Screen annotations')

    return next
  }

  const showOn = (bounds: AnnotationBounds) => {
    if (!window || window.isDestroyed()) {
      window = spawn(bounds)

      return
    }

    const current = window.getBounds()
    const resizing = current.width !== bounds.width || current.height !== bounds.height
    const moving = current.x !== bounds.x || current.y !== bounds.y

    if (moving || resizing) {
      // The window is created non-resizable (a transparent click-through
      // overlay must not expose a system resize hot-zone), which on
      // Windows/Linux also makes Electron drop the size half of setBounds —
      // so flip resizable on for the call and restore the lock after. The
      // same dance appears in pet-overlay-ipc, hud-ipc and hud-geometry's
      // applyHudResetBounds; if a fifth site turns up, extract one helper
      // over all of them rather than growing another copy.
      const restoreResizeLock = resizing && !window.isResizable()

      try {
        if (restoreResizeLock) {
          window.setResizable(true)
        }

        window.setBounds(bounds)
      } finally {
        if (restoreResizeLock && !window.isDestroyed()) {
          window.setResizable(false)
        }
      }
    }

    window.showInactive()
  }

  const clear = () => {
    disarmTtl()
    shapes = []
    sendState()

    if (window && !window.isDestroyed()) {
      window.hide()
    }
  }

  const annotate = async (
    request: unknown,
    senderBounds: AnnotationBounds | null
  ): Promise<Record<string, unknown>> => {
    const generation = ++requestGeneration
    const req = (request && typeof request === 'object' ? request : {}) as Record<string, unknown>
    const action = typeof req.action === 'string' ? req.action.trim().toLowerCase() : 'draw'

    if (action === 'clear') {
      clear()

      return { cleared: true, success: true }
    }

    const frameRaw = (req.frame && typeof req.frame === 'object' ? req.frame : {}) as Record<string, unknown>
    const frameWidth = asPositive(frameRaw.width)
    const frameHeight = asPositive(frameRaw.height)

    if (!frameWidth || !frameHeight) {
      return {
        error: 'draw needs frame.width and frame.height — the pixel size of the screenshot the coordinates come from.',
        success: false
      }
    }

    const spec = typeof req.target === 'string' ? req.target : undefined
    let target: AnnotationBounds
    let targetInfo: Record<string, unknown>

    if (isScreenTarget(spec)) {
      // Whole-display coordinates, anchored to the display the asking Hermes
      // window sits on — where the user is actually looking.
      const display = senderBounds ? screen.getDisplayMatching(senderBounds) : screen.getPrimaryDisplay()
      target = display.bounds
      targetInfo = { target: 'screen' }
    } else {
      const windows = await enumerateWindowsFrontToBack(process.pid, titlesAvailable())

      // The one suspension point. Everything below runs synchronously, so a
      // single staleness check here is enough to keep an older request from
      // overwriting a newer draw or clear that landed while we enumerated.
      if (generation !== requestGeneration) {
        return { error: 'Superseded by a newer screen-annotation request.', success: false }
      }

      if (enumerationFailed(windows)) {
        return {
          error:
            enumerationFailureNote(process.platform, process.env, windows.reason) +
            " Pass target='screen' to draw in whole-display coordinates instead.",
          success: false
        }
      }

      const resolved = resolveAnnotationWindow(windows, process.pid, senderBounds, spec)

      if (resolved.error !== undefined) {
        return { error: resolved.error, success: false }
      }

      target = resolved.window.bounds
      targetInfo = {
        target: { app: resolved.window.app, bounds: resolved.window.bounds, title: resolved.window.title }
      }
    }

    const display = screen.getDisplayMatching(target)
    const mapped = mapAnnotationShapes(req.shapes, { height: frameHeight, width: frameWidth }, target, display.bounds)

    if (mapped.shapes.length === 0) {
      return { error: 'No drawable shapes in the request.', success: false }
    }

    const ttl = clampAnnotationTtlSeconds(req.ttl_seconds)

    disarmTtl()
    shapes = mapped.shapes
    showOn(display.bounds)
    sendState()

    ttlTimer = setTimeout(() => {
      ttlTimer = null
      clear()
    }, ttl * 1000)

    const result: Record<string, unknown> = {
      ...targetInfo,
      display: display.bounds,
      expires_in_seconds: ttl,
      shapes_drawn: mapped.shapes.length,
      success: true
    }

    if (mapped.skipped > 0) {
      result.shapes_skipped = mapped.skipped
    }

    return result
  }

  const close = () => {
    // Invalidate any in-flight draw so it cannot respawn the overlay behind a
    // quitting app. (The TTL's own clear() deliberately does NOT bump the
    // generation: it belongs to the shapes it expired, and a newer draw that
    // is mid-enumeration when it fires must still land.)
    requestGeneration += 1
    disarmTtl()

    if (window && !window.isDestroyed()) {
      window.close()
    }

    window = null
    shapes = []
  }

  return {
    annotate,
    clear,
    close,
    getState: () => ({ shapes })
  }
}

/**
 * IPC surface. The renderer that received `screen.annotate.request` forwards
 * the payload here (the sender's own bounds anchor the default target), and
 * the overlay renderer pulls the latest shapes on mount — its lazy chunk can
 * attach listeners after main's did-finish-load push already fired.
 */
export function registerScreenAnnotationsIpc(controller: ScreenAnnotationsController): void {
  ipcMain.handle('hermes:screen:annotate', async (event, request) => {
    const sender = BrowserWindow.fromWebContents(event.sender)
    const senderBounds = sender && !sender.isDestroyed() ? sender.getBounds() : null

    try {
      return await controller.annotate(request, senderBounds)
    } catch (error) {
      return { error: error instanceof Error ? error.message : String(error), success: false }
    }
  })

  ipcMain.handle('hermes:screen-annotations:get', () => controller.getState())
}
