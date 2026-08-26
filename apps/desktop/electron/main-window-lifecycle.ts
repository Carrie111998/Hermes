type MainWindowLike = {
  isDestroyed: () => boolean
}

type FocusableMainWindowLike = MainWindowLike & {
  focus: () => unknown
  isMinimized: () => boolean
  isVisible: () => boolean
  restore: () => unknown
  show: () => unknown
}

type HideableMainWindowLike = MainWindowLike & {
  hide: () => unknown
  isVisible: () => boolean
}

export function hideMainWindow(window: HideableMainWindowLike | null | undefined): boolean {
  if (!window || window.isDestroyed() || !window.isVisible()) {
    return false
  }

  window.hide()

  return true
}

export function focusMainWindow(window: FocusableMainWindowLike): void {
  if (!window || window.isDestroyed()) {
    return
  }

  if (window.isMinimized()) {
    window.restore()
  }

  if (!window.isVisible()) {
    window.show()
  }

  window.focus()
}

type EnsureMainWindowOptions<T extends MainWindowLike> = {
  isReady: boolean
  createWindow: () => unknown
  focusWindow: (window: T) => unknown
  focusExisting?: boolean
}

export function ensureMainWindow<T extends MainWindowLike>(
  window: T | null | undefined,
  { isReady, createWindow, focusWindow, focusExisting = true }: EnsureMainWindowOptions<T>
) {
  if (!window || window.isDestroyed()) {
    // a closed electron window stays truthy, so replace it before invoking native methods.
    if (isReady) {
      createWindow()
    }

    return
  }

  if (focusExisting) {
    focusWindow(window)
  }
}
