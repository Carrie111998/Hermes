export type WindowControlsMode = 'hidden' | 'native' | 'system'

export const DEFAULT_WINDOW_CONTROLS_MODE: WindowControlsMode = 'system'

const COMPOSITOR_MANAGED_DESKTOPS = /(?:^|[:;])(hyprland|sway|river|niri|dwl)(?:$|[:;])/i

export function normalizeWindowControlsMode(value: unknown): WindowControlsMode {
  return value === 'hidden' || value === 'native' || value === 'system' ? value : DEFAULT_WINDOW_CONTROLS_MODE
}

/**
 * Known compositor-managed Wayland sessions do not need an in-app titlebar
 * control strip by default. Desktop environments such as KDE and GNOME keep
 * the native-control fallback because they are commonly used with decorations.
 */
export function isCompositorManagedWaylandSession(env: NodeJS.ProcessEnv): boolean {
  if (env.HYPRLAND_INSTANCE_SIGNATURE) {
    return true
  }

  const wayland = env.XDG_SESSION_TYPE === 'wayland' || Boolean(env.WAYLAND_DISPLAY)
  const desktops = `${env.XDG_CURRENT_DESKTOP ?? ''};${env.XDG_SESSION_DESKTOP ?? ''}`

  return wayland && COMPOSITOR_MANAGED_DESKTOPS.test(desktops)
}

export function nativeWindowControlsEnabled(mode: WindowControlsMode, compositorManaged: boolean): boolean {
  return mode === 'native' || (mode === 'system' && !compositorManaged)
}
