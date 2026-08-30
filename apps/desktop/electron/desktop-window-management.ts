/**
 * Auxiliary desktop window management extracted from electron/main.ts.
 *
 * The factory keeps Electron and main-process seams explicit while preserving
 * the historical function names at the composition boundary.
 */
export function createDesktopWindowManagement(deps: Record<string, any>) {
  const {
    BROWSER_WINDOW_HEIGHT,
    BROWSER_WINDOW_MIN_HEIGHT,
    BROWSER_WINDOW_MIN_WIDTH,
    BROWSER_WINDOW_WIDTH,
    BrowserWindow,
    DEV_SERVER,
    IS_MAC,
    PRELOAD_PATH,
    RENDERER_RELOAD_MAX,
    RENDERER_RELOAD_WINDOW_MS,
    SESSION_WINDOW_MIN_HEIGHT,
    SESSION_WINDOW_MIN_WIDTH,
    WINDOW_BUTTON_POSITION,
    WINDOW_MIN_HEIGHT,
    WINDOW_MIN_WIDTH,
    attachRendererConsoleCapture,
    buildBrowserWindowUrl,
    buildInstanceWindowUrl,
    buildSessionWindowUrl,
    chatWindowSurfaceOptions,
    chatWindowWebPreferences,
    computeWindowOptions,
    createSessionWindowRegistry,
    createWakeIndicatorWindowController,
    createWindowRevealController,
    getAppIconPath,
    getMainWindow,
    getTitleBarOverlayOptions,
    getWindowState,
    installBrowserNavGestures,
    installContextMenuBridge,
    installDevToolsShortcut,
    installFindShortcut,
    installPreviewShortcut,
    installWindowRendererLifecycle,
    instanceWindowBounds,
    installZoomReassertOnNavigation,
    installZoomReassertOnWindowEvents,
    installZoomShortcuts,
    loadWindowUrl,
    openExternalUrl,
    readWindowState,
    rememberLog,
    rendererReloadTimesRef,
    resolveRendererIndex,
    restorePersistedZoomLevel,
    screen,
    sendWindowStateChanged,
    streamThrottle,
    translucencyBackedWindows,
    zoomWiringForWindowKind
  } = deps

  // Shared navigation guards + window chrome wiring applied to every window
  // (the primary plus any secondary session windows). Factored out of
  // createWindow() so secondary windows can't drift from the main window's
  // security posture: external links open in the OS browser, in-app navigation
  // stays confined to the dev server / packaged file URL, and the preview /
  // devtools / zoom / context-menu affordances behave identically everywhere.
  //
  // `zoom` is opt-out for the pet overlay: it sizes its own OS window to fit the
  // sprite in unzoomed CSS px (overlayWindowSize -> setBounds) and has its own
  // Alt+wheel scale, so inheriting the global UI zoom would render the mascot
  // larger than its window and crop it. Chat windows keep zoom on.
  function wireCommonWindowHandlers(win, { zoom = true }: { zoom?: boolean } = {}) {
    installPreviewShortcut(win)
    installDevToolsShortcut(win)
    installBrowserNavGestures(win)

    // Claim Ctrl/Cmd+F in the main process — on Pop!_OS / GNOME-based Linux
    // distros the Ctrl+F keydown does not reach the renderer's `view.findInPage`
    // binding (#81727). Routing it through `before-input-event` forwards the
    // intent at the earliest observable point. macOS / Windows keep the
    // renderer's own rebindable keybind, so the hook is Linux-only: installing
    // it elsewhere would make Ctrl/Cmd+F un-rebindable and double-open.
    if (process.platform === 'linux') {
      installFindShortcut(win)
    }

    if (zoom) {
      installZoomShortcuts(win)
      // Re-apply persisted zoom on show/restore/resize/cross-display move
      // (Chromium can drop webContents zoom after these window transitions), on
      // EVERY full load — not once, since crash recovery reloads and would
      // outlive a spent `once` listener (#46429) — and after in-page navigation,
      // where Chromium applies the target hash route's own per-URL zoom record
      // (see installZoomReassertOnNavigation; #48658, #38854, #79863).
      const reassertZoom = () => restorePersistedZoomLevel(win)

      installZoomReassertOnWindowEvents(win, reassertZoom)
      installZoomReassertOnNavigation(win.webContents, reassertZoom)
    }

    installContextMenuBridge(win)
    win.webContents.setWindowOpenHandler(details => {
      openExternalUrl(details.url)

      return { action: 'deny' }
    })
    win.webContents.on('will-navigate', (event, url) => {
      if ((DEV_SERVER && url.startsWith(DEV_SERVER)) || (!DEV_SERVER && url.startsWith('file:'))) {
        return
      }

      event.preventDefault()
      openExternalUrl(url)
    })
  }

  // Every window we open starts with `show: false` so the renderer's first themed
  // paint lands before it appears, and `ready-to-show` is what reveals it.
  // Electron 40 can drop that event entirely (electron/electron#51972) on
  // Linux/Wayland, remote displays and VMs, leaving the window hidden forever even
  // though the renderer finished loading. Keep the themed path as the preferred
  // reveal, then fall back a few seconds after the renderer loads. `show` and
  // `onRevealed` carry the caller's reveal action and post-visible work; whichever
  // path wins runs them exactly once.
  function wireWindowReveal(win, { show, onRevealed }: { show?: () => void; onRevealed?: () => void } = {}) {
    const controller = createWindowRevealController(
      {
        isDestroyed: () => win.isDestroyed(),
        isVisible: () => win.isVisible(),
        show: show ?? (() => win.show())
      },
      { onRevealed }
    )

    win.once('ready-to-show', controller.reveal)
    win.webContents.once('did-finish-load', controller.scheduleFallback)
    win.on('closed', controller.dispose)

    return controller
  }

  // Secondary "session windows" — one extra OS window per chat so a user can
  // work with multiple chats side by side. The registry guarantees one window
  // per sessionId (re-opening focuses the existing window) and self-cleans on
  // close. The primary mainWindow is never tracked here. Pure logic + the URL
  // builder live in session-windows.ts so they stay unit-testable.
  const sessionWindows = createSessionWindowRegistry()

  function focusWindow(win) {
    if (!win || win.isDestroyed()) {
      return
    }

    if (win.isMinimized()) {
      win.restore()
    }

    if (!win.isVisible()) {
      win.show()
    }

    win.focus()
  }

  function spawnSecondaryWindow({ sessionId, watch }: { sessionId?: string; watch?: boolean } = {}) {
    const icon = getAppIconPath()

    const win = new BrowserWindow({
      width: SESSION_WINDOW_MIN_WIDTH,
      height: SESSION_WINDOW_MIN_HEIGHT,
      minWidth: SESSION_WINDOW_MIN_WIDTH,
      minHeight: SESSION_WINDOW_MIN_HEIGHT,
      title: 'Hermes',
      titleBarStyle: 'hidden',
      titleBarOverlay: getTitleBarOverlayOptions(),
      trafficLightPosition: IS_MAC ? WINDOW_BUTTON_POSITION : undefined,
      ...chatWindowSurfaceOptions(),
      icon,
      // Don't show until the renderer's first themed paint is ready. macOS
      // `vibrancy` ignores `backgroundColor` and paints a translucent OS
      // material (which follows the OS appearance, not the app theme), so a
      // dark-themed app on a light-mode Mac flashes white until the renderer
      // covers it. ready-to-show fires after the boot-time paint in
      // themes/context.tsx, so the window appears already themed.
      show: false,
      webPreferences: chatWindowWebPreferences(PRELOAD_PATH)
    })

    // Chat-surface registration: applyWindowTranslucency swaps this window's
    // backing between opaque-themed and alpha-0 when glass toggles.
    translucencyBackedWindows.add(win)

    if (IS_MAC) {
      win.setWindowButtonPosition?.(WINDOW_BUTTON_POSITION)
    }

    wireWindowReveal(win)

    win.on('enter-full-screen', () => sendWindowStateChanged(true))
    win.on('leave-full-screen', () => sendWindowStateChanged(false))

    streamThrottle.register(win)
    wireCommonWindowHandlers(win, zoomWiringForWindowKind('chat'))
    attachRendererConsoleCapture(win, 'session-window', rememberLog)

    // Renderer lifecycle diagnostics + recovery (#81290): a dead session-window
    // renderer used to log nothing and stay black; now it logs with its window
    // kind and reloads under the shared crash-loop budget, exactly like the
    // primary window, without touching any other window.
    installWindowRendererLifecycle(win, {
      kind: 'secondary',
      callbacks: {
        log: rememberLog,
        reload: () => {
          win.webContents.reload()
        }
      },
      reloadWindowMs: RENDERER_RELOAD_WINDOW_MS,
      reloadMax: RENDERER_RELOAD_MAX,
      recentReloadTimesRef: rendererReloadTimesRef
    })

    loadWindowUrl(
      win,
      buildSessionWindowUrl(sessionId, {
        devServer: DEV_SERVER,
        rendererIndexPath: DEV_SERVER ? undefined : resolveRendererIndex(),
        watch
      }),
      'Session window'
    )

    return win
  }

  // Open (or focus) a standalone window for a single chat session.
  function createSessionWindow(sessionId, { watch = false } = {}) {
    return sessionWindows.openOrFocus(sessionId, () => spawnSecondaryWindow({ sessionId, watch }))
  }

  // Popped-out in-app Browser: same webview + address bar as a docked Browser
  // tab, in its own OS window. One window per tab id (re-open focuses); closing
  // it tells the other renderers so they can dock the tab again.
  const browserWindows = createSessionWindowRegistry()

  function notifyBrowserPopoutClosed(tabId) {
    if (typeof tabId !== 'string' || !tabId) {
      return
    }

    for (const other of BrowserWindow.getAllWindows()) {
      if (!other.isDestroyed()) {
        other.webContents.send('hermes:browser-popout:closed', tabId)
      }
    }
  }

  function spawnBrowserWindow(tabId) {
    const icon = getAppIconPath()

    const win = new BrowserWindow({
      width: BROWSER_WINDOW_WIDTH,
      height: BROWSER_WINDOW_HEIGHT,
      minWidth: BROWSER_WINDOW_MIN_WIDTH,
      minHeight: BROWSER_WINDOW_MIN_HEIGHT,
      title: 'Hermes',
      titleBarStyle: 'hidden',
      titleBarOverlay: getTitleBarOverlayOptions(),
      trafficLightPosition: IS_MAC ? WINDOW_BUTTON_POSITION : undefined,
      ...chatWindowSurfaceOptions(),
      icon,
      show: false,
      webPreferences: chatWindowWebPreferences(PRELOAD_PATH)
    })

    translucencyBackedWindows.add(win)

    if (IS_MAC) {
      win.setWindowButtonPosition?.(WINDOW_BUTTON_POSITION)
    }

    wireWindowReveal(win)

    win.on('enter-full-screen', () => sendWindowStateChanged(true))
    win.on('leave-full-screen', () => sendWindowStateChanged(false))

    streamThrottle.register(win)
    wireCommonWindowHandlers(win, zoomWiringForWindowKind('chat'))
    attachRendererConsoleCapture(win, 'browser-window', rememberLog)

    installWindowRendererLifecycle(win, {
      kind: 'browser',
      callbacks: {
        log: rememberLog,
        reload: () => {
          win.webContents.reload()
        }
      },
      reloadWindowMs: RENDERER_RELOAD_WINDOW_MS,
      reloadMax: RENDERER_RELOAD_MAX,
      recentReloadTimesRef: rendererReloadTimesRef
    })

    win.on('closed', () => notifyBrowserPopoutClosed(tabId))

    loadWindowUrl(
      win,
      buildBrowserWindowUrl(tabId, {
        devServer: DEV_SERVER,
        rendererIndexPath: DEV_SERVER ? undefined : resolveRendererIndex()
      }),
      'Browser window'
    )

    return win
  }

  function createBrowserWindow(tabId) {
    return browserWindows.openOrFocus(tabId, () => spawnBrowserWindow(tabId))
  }

  // Additional full "instance" windows — peers of the primary that render the
  // COMPLETE app (sidebar, routing, its own draft) against the shared backend, so
  // a user can run multiple GUI windows at once (⌘⇧N / the "New Window" palette
  // command). Unlike the compact session windows they carry no `?win` flag; a
  // separate `peer=1` marker prevents them from replaying app-launch source
  // restoration after joining that shared backend. The primary mainWindow stays
  // the notification / deep-link / pet-overlay anchor and
  // is NOT tracked here. The set holds a strong reference so an open peer isn't
  // garbage-collected, and drops it on close.
  const instanceWindows = new Set<any>()

  // Cascade a new instance off whichever window spawned it so it doesn't land
  // exactly on top of its source. Falls back to the persisted primary geometry
  // when there's no live source window (e.g. all windows closed on macOS). The
  // pure cascade math lives in session-windows.ts (instanceWindowBounds).
  function nextInstanceBounds() {
    const source = BrowserWindow.getFocusedWindow() || getMainWindow()
    const fallback = computeWindowOptions(readWindowState(), screen.getAllDisplays())
    const base = source && !source.isDestroyed() ? source.getBounds() : null

    return instanceWindowBounds(base, fallback)
  }

  // Open a new full-chrome instance window. Mirrors createWindow()'s window
  // options (shared chatWindowWebPreferences + streamThrottle registration so a
  // streamed answer never stalls in the background) but is a peer, not the
  // primary: it never overwrites the mainWindow global, doesn't start the backend
  // (the renderer's getConnection() joins the already-running one), and loads the
  // plain renderer URL so the full app renders.
  function createInstanceWindow() {
    const icon = getAppIconPath()

    const win = new BrowserWindow({
      ...nextInstanceBounds(),
      minWidth: WINDOW_MIN_WIDTH,
      minHeight: WINDOW_MIN_HEIGHT,
      title: 'Hermes',
      titleBarStyle: 'hidden',
      titleBarOverlay: getTitleBarOverlayOptions(),
      trafficLightPosition: IS_MAC ? WINDOW_BUTTON_POSITION : undefined,
      ...chatWindowSurfaceOptions(),
      icon,
      show: false,
      webPreferences: chatWindowWebPreferences(PRELOAD_PATH)
    })

    instanceWindows.add(win)

    // Chat-surface registration: see applyWindowTranslucency.
    translucencyBackedWindows.add(win)

    if (IS_MAC) {
      win.setWindowButtonPosition?.(WINDOW_BUTTON_POSITION)
    }

    wireWindowReveal(win)

    // Per-window fullscreen chrome: send this window its own titlebar inset so its
    // traffic lights hide/show independently of the primary.
    win.on('enter-full-screen', () => sendWindowStateChanged(true, win))
    win.on('leave-full-screen', () => sendWindowStateChanged(false, win))

    streamThrottle.register(win)
    wireCommonWindowHandlers(win, zoomWiringForWindowKind('chat'))

    // Renderer lifecycle diagnostics + recovery (#81290), same policy as the
    // primary and session windows: a crashed instance renderer logs with its
    // window kind and reloads under the shared crash-loop budget.
    installWindowRendererLifecycle(win, {
      kind: 'instance',
      callbacks: {
        log: rememberLog,
        reload: () => {
          win.webContents.reload()
        }
      },
      reloadWindowMs: RENDERER_RELOAD_WINDOW_MS,
      reloadMax: RENDERER_RELOAD_MAX,
      recentReloadTimesRef: rendererReloadTimesRef
    })

    win.on('closed', () => {
      instanceWindows.delete(win)
    })

    attachRendererConsoleCapture(win, 'instance', rememberLog)
    loadWindowUrl(
      win,
      buildInstanceWindowUrl({
        devServer: DEV_SERVER,
        rendererIndexPath: DEV_SERVER ? undefined : resolveRendererIndex()
      }),
      'Instance window'
    )

    return win
  }

  // A macOS-only ambient wake cue. It is deliberately a gateway-less helper
  // window: the active renderer owns voice state and sends only the visual phase.
  const wakeIndicatorController = createWakeIndicatorWindowController({
    devServer: DEV_SERVER,
    isMac: IS_MAC,
    loadWindowUrl,
    log: rememberLog,
    preloadPath: PRELOAD_PATH,
    rendererIndex: resolveRendererIndex,
    wireWindow: window => wireCommonWindowHandlers(window, zoomWiringForWindowKind('wakeIndicator'))
  })

  return {
    createBrowserWindow,
    createInstanceWindow,
    createSessionWindow,
    focusWindow,
    wakeIndicatorController,
    wireCommonWindowHandlers,
    wireWindowReveal
  }
}
