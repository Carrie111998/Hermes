/**
 * Which input model the HUD can safely use on this system.
 *
 * The HUD's default is per-element click-through: the window ignores the mouse
 * and turns solid only where the renderer's hit test finds a control under the
 * cursor. That design rests on `setIgnoreMouseEvents(false)` actually handing
 * the input region back. On X11 it does not — once a window has ignored the
 * mouse the X server keeps hit-testing straight through it, and no later call
 * restores it (the live diagnosis is in `startHudCursorFeed`). It is a one-way
 * door, so the only policy that works there is never to walk through it: keep
 * the HUD a normal solid window for its whole life.
 *
 * That trade is a real loss — a solid HUD swallows clicks in the faded band
 * above the composer that would otherwise reach the app underneath — so it is
 * scoped to the backend where the restore is known to be broken instead of
 * being applied to every Linux desktop.
 *
 * The windowing backend, not the session type, is what decides this. Electron
 * reaches the display server through Ozone, and this app appends no
 * `--ozone-platform` switch, so on Linux it takes Electron's default X11
 * backend — including on a Wayland session, where it runs as an XWayland client
 * and is an X11 window like any other. That is why the affected reports span
 * GNOME/ARM64 and KDE Plasma/x86_64 alike: both are X11 windows. A native
 * Wayland surface happens only when someone asks for one explicitly, the
 * one-way door has never been observed there, and so that case keeps the
 * existing click-through path.
 */

/**
 * `solid` keeps the HUD a normal window that never ignores the mouse.
 * `click-through` is the per-element design: ignore by default, solid under the
 * cursor.
 */
export type HudInputPolicy = 'click-through' | 'solid'

/** The Ozone backend Electron talks to the display server through. */
type Backend = 'wayland' | 'x11'

/**
 * The Ozone platform this process was asked for, or null when nothing asked.
 *
 * `--ozone-platform` names a backend outright and `--ozone-platform-hint`
 * (equivalently `ELECTRON_OZONE_PLATFORM_HINT`) asks for one, with `auto`
 * meaning "Wayland if the session is Wayland". The explicit switch wins over
 * the hint, and a repeated switch resolves last-one-wins, both matching
 * Chromium's own handling.
 */
function requestedOzonePlatform(env: NodeJS.ProcessEnv, argv: readonly string[]): null | string {
  let explicit: null | string = null
  let hint: null | string = null

  for (const arg of argv) {
    const match = /^--ozone-platform(-hint)?=(.+)$/.exec(arg)

    if (!match) {
      continue
    }

    if (match[1]) {
      hint = match[2].toLowerCase()
    } else {
      explicit = match[2].toLowerCase()
    }
  }

  return explicit ?? hint ?? env.ELECTRON_OZONE_PLATFORM_HINT?.toLowerCase() ?? null
}

/**
 * Whether the session itself is Wayland.
 *
 * Matches the reading already used for window enumeration in `window-below.ts`:
 * a session advertising `WAYLAND_DISPLAY` without a `DISPLAY` is Wayland with
 * no X server to fall back to.
 */
function sessionIsWayland(env: NodeJS.ProcessEnv): boolean {
  return env.XDG_SESSION_TYPE === 'wayland' || (Boolean(env.WAYLAND_DISPLAY) && !env.DISPLAY)
}

/**
 * The backend Electron will actually use on Linux.
 *
 * Absent any request this is X11 — Electron's Linux default — which is why a
 * Wayland session still lands here as an XWayland client unless it opted in.
 */
function linuxBackend(env: NodeJS.ProcessEnv, argv: readonly string[]): Backend {
  const requested = requestedOzonePlatform(env, argv)

  if (requested === 'wayland') {
    return 'wayland'
  }

  if (requested === 'auto') {
    return sessionIsWayland(env) ? 'wayland' : 'x11'
  }

  return 'x11'
}

/**
 * The input model the HUD should use.
 *
 * macOS and Windows keep the click-through design: `setIgnoreMouseEvents(true,
 * { forward: true })` is `@platform darwin,win32`, so the renderer goes on
 * seeing the cursor while the window ignores it and can re-arm whenever the
 * pointer comes back to the bar.
 */
export function hudInputPolicy(platform: string, env: NodeJS.ProcessEnv, argv: readonly string[]): HudInputPolicy {
  if (platform !== 'linux') {
    return 'click-through'
  }

  return linuxBackend(env, argv) === 'x11' ? 'solid' : 'click-through'
}
