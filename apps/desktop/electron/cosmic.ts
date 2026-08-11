// cosmic.ts — window enumeration for the COSMIC desktop (cosmic-comp).
//
// `read_window_below` normally enumerates through `get-windows`, whose Linux
// backend shells out to `xprop` and reads `_NET_CLIENT_LIST_STACKING`. That is
// an X11 protocol, and Wayland deliberately refuses to tell one application
// about another's windows — so under a native-Wayland COSMIC session the X11
// path enumerates nothing. COSMIC does not expose a native window-enumeration
// IPC the way Hyprland does (no socket, no D-Bus window list, no
// `foreign-toplevel-management` in cosmic-comp 1.0), so there is no Wayland
// protocol we can speak to read other clients.
//
// The working path on COSMIC is therefore XWayland: when Hermes Desktop runs
// under XWayland, `get-windows`/`xprop` answers normally. Hermes already lets
// a COSMIC user opt into that via `desktop.ozone_platform_hint: x11` (see
// issue #84011 / PR #84013), which bridges `ELECTRON_OZONE_PLATFORM_HINT` at
// launch. This module is the provider for COSMIC: it only ever answers on
// COSMIC, and it reuses the existing X11 enumerator (the right tool under
// XWayland). Everywhere else it returns null so the established path stays
// the default — same contract as `hyprland.ts`.
//
// Why not just let the X11 fallback run? Two reasons. First, the failure note
// differs: a native-Wayland COSMIC user should be told to set
// `ozone_platform_hint: x11`, not "log into an X11 session" (which COSMIC
// barely supports). Second, naming COSMIC explicitly in the provider chain
// makes the support surface self-documenting and testable, matching how
// Hyprland is handled.

import type { EnumeratedWindow } from './window-below'

/** True when the active session is the COSMIC desktop. */
export function isCosmic(env: NodeJS.ProcessEnv): boolean {
  const current = (env.XDG_CURRENT_DESKTOP ?? '').toLowerCase()
  const session = (env.XDG_SESSION_DESKTOP ?? '').toLowerCase()

  return current.includes('cosmic') || session.includes('cosmic')
}

/**
 * Enumerate every window COSMIC can see, front-to-back, or null when this is
 * not a COSMIC session (so the caller falls through to the generic path).
 *
 * On COSMIC the only viable enumeration is the X11 one, which speaks to
 * XWayland. We hand the work to the shared `get-windows` enumerator rather than
 * reimplementing its platform quirks (e.g. its Linux list arrives back-to-front
 * and must be reversed). When Hermes is a native-Wayland client there is
 * nothing to enumerate and `get-windows` returns null — the caller then
 * surfaces the COSMIC-specific guidance from `enumerationFailureNote`.
 */
export async function readCosmicWindows(
  selfPid: number,
  titlesAvailable: boolean,
  env: NodeJS.ProcessEnv,
  enumerate: (titlesAvailable: boolean) => Promise<EnumeratedWindow[] | null>
): Promise<EnumeratedWindow[] | null> {
  if (!isCosmic(env)) {
    return null
  }

  return enumerate(titlesAvailable)
}
