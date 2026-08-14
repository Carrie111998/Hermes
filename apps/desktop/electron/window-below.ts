// window-below.ts — which OS window sits directly underneath a Hermes window.
//
// Backs the desktop-gated `read_window_below` tool: the renderer receives
// `window.read.request` from the gateway, asks main over IPC, and answers
// with this module's serialized result. Enumeration uses `get-windows`
// (front-to-back z-order on macOS/Windows/Linux-X11); the picking logic is a
// pure function so the OS-specific part stays a thin provider. Where that
// provider can't run at all, the answer is why — see `enumerationFailureNote`.
//
// Privacy contract (matches the tool schema): metadata only — app, title,
// bounds. Never pixels. On macOS, window titles require the Screen Recording
// permission; we pass titles through only when that permission is ALREADY
// granted and never trigger the prompt for it.

import { execFile } from 'node:child_process'
import { promisify } from 'node:util'

import { readHyprlandWindows } from './hyprland'

export interface EnumeratedWindow {
  app: string
  bounds: { x: number; y: number; width: number; height: number }
  id: number
  pid: number
  title: string
}

export interface WindowBelowResult {
  frontmost: { app: string; title: string } | null
  note?: string
  platform: string
  window: {
    app: string
    bounds: { x: number; y: number; width: number; height: number }
    id: number
    title: string
  } | null
}

export interface WindowBelowUnavailable {
  error: string
  platform: string
}

/**
 * Why enumeration just failed, in terms the user can act on.
 *
 * The generic "could not determine the window underneath" this replaces is a
 * dead end on Linux, where the two ways it fails have opposite fixes and
 * neither is guessable: a Wayland session withholds window identity from
 * applications outright, and an X11 session needs `xprop`/`xwininfo` present
 * because that is what the enumerator shells out to.
 *
 * A session with both `WAYLAND_DISPLAY` and `DISPLAY` is Wayland running
 * XWayland, where `xprop` can still answer — so it is treated as X11 and gets
 * the tooling advice rather than being told to change session type.
 */
export function enumerationFailureNote(platform: string, env: NodeJS.ProcessEnv): string {
  if (platform !== 'linux') {
    return 'Could not enumerate windows on this system.'
  }

  // Hyprland is asked over its own IPC, so reaching here means the socket
  // didn't answer — telling a Hyprland user to go and install xprop, or to
  // abandon Wayland, would send them in exactly the wrong direction.
  if (env.HYPRLAND_INSTANCE_SIGNATURE) {
    return (
      'Could not enumerate windows: Hyprland did not answer on its IPC socket. ' +
      'Check that `hyprctl clients` works from the same session Hermes is ' +
      'running in.'
    )
  }

  const wayland = env.XDG_SESSION_TYPE === 'wayland' || (Boolean(env.WAYLAND_DISPLAY) && !env.DISPLAY)

  if (wayland) {
    return (
      'Could not enumerate windows: this is a Wayland session, and Wayland does ' +
      'not let an application see other applications\u2019 windows. Log in to an ' +
      'X11/Xorg session, or run Hermes under XWayland with DISPLAY set.'
    )
  }

  return (
    'Could not enumerate windows: this needs the xprop and xwininfo commands ' +
    '(the x11-utils package on Debian/Ubuntu, xorg-x11-utils on Fedora).'
  )
}

const overlaps = (a: EnumeratedWindow['bounds'], b: EnumeratedWindow['bounds']): boolean =>
  a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height

/**
 * Pick the window directly underneath ours from a front-to-back window list.
 *
 * Walks past every window owned by our own process (all Hermes windows share
 * the main process pid), then takes the first other-process window whose
 * bounds overlap ours — "underneath" means visually behind, not merely next
 * in z-order on some other display. `frontmost` is the first other-process
 * window regardless of overlap: the app the user was last working in.
 */
export function pickWindowBelow(
  windows: EnumeratedWindow[],
  selfPid: number,
  selfBounds: EnumeratedWindow['bounds']
): { below: EnumeratedWindow | null; frontmost: EnumeratedWindow | null } {
  const others = windows.filter(w => w.pid !== selfPid)
  const frontmost = others[0] ?? null

  const selfIndex = windows.findIndex(w => w.pid === selfPid)
  const behind = selfIndex === -1 ? others : windows.slice(selfIndex + 1)
  const below = behind.find(w => w.pid !== selfPid && overlaps(w.bounds, selfBounds)) ?? null

  return { below, frontmost }
}

type GetWindowsModule = {
  openWindows: (options?: { accessibilityPermission?: boolean; screenRecordingPermission?: boolean }) => Promise<
    Array<{
      bounds?: { height?: number; width?: number; x?: number; y?: number }
      id?: number
      owner?: { name?: string; processId?: number }
      title?: string
    }>
  >
}

let getWindowsModule: Promise<GetWindowsModule> | null = null

const execFileAsync = promisify(execFile)

const loadGetWindows = (): Promise<GetWindowsModule> => {
  getWindowsModule ??= import('get-windows')

  return getWindowsModule
}

// `get-windows` uses a bundled Swift executable on macOS. If that helper is
// absent or cannot launch from a packaged app, use the same CoreGraphics API
// through macOS' built-in JavaScript bridge. `/usr/bin/osascript` is part of the
// OS, needs no bundled native payload, and CGWindowListCopyWindowInfo exposes
// app/bounds/z-order without an Accessibility or Screen Recording prompt.
// Window titles are still stripped below unless Screen Recording was already
// granted to Hermes.
const MAC_WINDOW_ENUMERATION_JXA = `
ObjC.import("CoreGraphics");

function run() {
  var raw = $.CGWindowListCopyWindowInfo(
    $.kCGWindowListOptionOnScreenOnly | $.kCGWindowListExcludeDesktopElements,
    $.kCGNullWindowID
  );
  var bridged = ObjC.castRefToObject(raw);
  var windows = [];

  function unwrap(dictionary, key, fallback) {
    var value = dictionary && dictionary.objectForKey(key);
    if (!value) return fallback;
    var unwrapped = ObjC.unwrap(value);
    return unwrapped === undefined || unwrapped === null ? fallback : unwrapped;
  }

  for (var index = 0; index < bridged.count; index += 1) {
    var window = bridged.objectAtIndex(index);
    var bounds = window.objectForKey("kCGWindowBounds");
    var layer = Number(unwrap(window, "kCGWindowLayer", 0));
    var alpha = Number(unwrap(window, "kCGWindowAlpha", 0));
    var width = Number(unwrap(bounds, "Width", 0));
    var height = Number(unwrap(bounds, "Height", 0));

    // Layer zero is an ordinary application window. Excluding menu bar,
    // notification, and desktop layers keeps those system surfaces from being
    // mistaken for the app underneath the HUD.
    if (layer !== 0 || alpha <= 0 || width <= 0 || height <= 0) continue;

    windows.push({
      app: String(unwrap(window, "kCGWindowOwnerName", "")),
      bounds: {
        x: Number(unwrap(bounds, "X", 0)),
        y: Number(unwrap(bounds, "Y", 0)),
        width: width,
        height: height
      },
      id: Number(unwrap(window, "kCGWindowNumber", 0)),
      pid: Number(unwrap(window, "kCGWindowOwnerPID", 0)),
      title: String(unwrap(window, "kCGWindowName", ""))
    });
  }

  return JSON.stringify(windows);
}
`

export interface MacJxaCommand {
  args: string[]
  file: string
  options: { encoding: 'utf8'; maxBuffer: number; timeout: number }
}

export type MacJxaExecutor = (command: MacJxaCommand) => Promise<string>

const executeMacJxa: MacJxaExecutor = async command => {
  const result = await execFileAsync(command.file, command.args, command.options)

  return String(result.stdout ?? '')
}

const asRecord = (value: unknown): Record<string, unknown> | null =>
  typeof value === 'object' && value !== null && !Array.isArray(value) ? (value as Record<string, unknown>) : null

const finiteNumber = (value: unknown): number => (typeof value === 'number' && Number.isFinite(value) ? value : 0)

export function parseMacWindowEnumeration(stdout: string, titlesAvailable: boolean): EnumeratedWindow[] | null {
  let parsed: unknown

  try {
    parsed = JSON.parse(stdout)
  } catch {
    return null
  }

  if (!Array.isArray(parsed)) {
    return null
  }

  const windows: EnumeratedWindow[] = []

  for (const value of parsed) {
    const window = asRecord(value)
    const bounds = asRecord(window?.bounds)

    if (!window || !bounds) {
      continue
    }

    const width = finiteNumber(bounds.width)
    const height = finiteNumber(bounds.height)

    if (width <= 0 || height <= 0) {
      continue
    }

    windows.push({
      app: typeof window.app === 'string' ? window.app : '',
      bounds: {
        x: finiteNumber(bounds.x),
        y: finiteNumber(bounds.y),
        width,
        height
      },
      id: finiteNumber(window.id),
      pid: finiteNumber(window.pid),
      title: titlesAvailable && typeof window.title === 'string' ? window.title : ''
    })
  }

  return windows
}

export async function enumerateViaMacJxa(
  titlesAvailable: boolean,
  execute: MacJxaExecutor = executeMacJxa
): Promise<EnumeratedWindow[] | null> {
  try {
    const stdout = await execute({
      args: ['-l', 'JavaScript', '-e', MAC_WINDOW_ENUMERATION_JXA],
      file: '/usr/bin/osascript',
      options: { encoding: 'utf8', maxBuffer: 2 * 1024 * 1024, timeout: 3000 }
    })

    return parseMacWindowEnumeration(stdout, titlesAvailable)
  } catch {
    return null
  }
}

/**
 * Enumerate windows and serialize the one underneath `selfBounds`.
 *
 * `titlesAvailable` is the macOS Screen Recording grant (pass true on other
 * platforms, where titles are free). When enumeration itself is unavailable
 * (Wayland, missing xprop, addon load failure) this answers with the reason
 * rather than nothing, so the agent can tell the user what to fix instead of
 * reporting a blank failure.
 */
async function enumerateViaGetWindows(titlesAvailable: boolean): Promise<EnumeratedWindow[] | null> {
  let raw

  try {
    const { openWindows } = await loadGetWindows()
    raw = await openWindows(
      process.platform === 'darwin'
        ? { accessibilityPermission: false, screenRecordingPermission: titlesAvailable }
        : undefined
    )
  } catch {
    return null
  }

  if (!Array.isArray(raw)) {
    return null
  }

  // get-windows documents openWindows() as front-to-back, and macOS/Windows
  // honor that (CGWindowList / EnumWindows order). Its lib/linux.js, however,
  // iterates `_NET_CLIENT_LIST_STACKING` in raw xprop order, which EWMH
  // defines as bottom-to-top — so the Linux list arrives back-to-front and
  // must be reversed to match. (Verified against get-windows 9.3.0.)
  const ordered = process.platform === 'linux' ? [...raw].reverse() : raw

  return ordered.map(w => ({
    app: w.owner?.name ?? '',
    bounds: {
      x: w.bounds?.x ?? 0,
      y: w.bounds?.y ?? 0,
      width: w.bounds?.width ?? 0,
      height: w.bounds?.height ?? 0
    },
    id: w.id ?? 0,
    pid: w.owner?.processId ?? 0,
    title: w.title ?? ''
  }))
}

type WindowEnumerator = (titlesAvailable: boolean) => Promise<EnumeratedWindow[] | null>

export async function enumerateWindowsWithFallback(
  titlesAvailable: boolean,
  {
    platform = process.platform,
    primary = enumerateViaGetWindows,
    macFallback = enumerateViaMacJxa
  }: {
    platform?: NodeJS.Platform
    primary?: WindowEnumerator
    macFallback?: WindowEnumerator
  } = {}
): Promise<EnumeratedWindow[] | null> {
  const windows = await primary(titlesAvailable)

  if (windows !== null || platform !== 'darwin') {
    return windows
  }

  return macFallback(titlesAvailable)
}

export async function readWindowBelow(
  selfPid: number,
  selfBounds: EnumeratedWindow['bounds'],
  titlesAvailable: boolean,
  {
    enumerate = enumerateWindowsWithFallback,
    readHyprland = readHyprlandWindows
  }: {
    enumerate?: typeof enumerateWindowsWithFallback
    readHyprland?: typeof readHyprlandWindows
  } = {}
): Promise<WindowBelowResult | WindowBelowUnavailable> {
  // Hyprland first, and only ever on Hyprland — its own IPC sees native Wayland
  // windows, which the X11 enumerator below cannot, and it answers null
  // everywhere else so the established path stays the default.
  const windows = (await readHyprland(selfPid)) ?? (await enumerate(titlesAvailable))

  if (!windows) {
    return {
      error: enumerationFailureNote(process.platform, process.env),
      platform: process.platform
    }
  }

  const { below, frontmost } = pickWindowBelow(windows, selfPid, selfBounds)

  const result: WindowBelowResult = {
    frontmost: frontmost ? { app: frontmost.app, title: frontmost.title } : null,
    platform: process.platform,
    window: below ? { app: below.app, bounds: below.bounds, id: below.id, title: below.title } : null
  }

  if (process.platform === 'darwin' && !titlesAvailable) {
    result.note =
      'Window titles are hidden: macOS reveals other apps\u2019 titles only with the ' +
      'Screen Recording permission, which Hermes does not request for this.'
  }

  return result
}
