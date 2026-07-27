/**
 * Ctrl/Cmd+wheel window zoom preference.
 *
 * This is device-local presentation state, independent of the active Hermes
 * profile. Electron owns and persists the setting so it is effective at cold
 * launch, before the Appearance page has mounted; the renderer only mirrors
 * the current value for the settings UI.
 */

import { atom } from 'nanostores'

export const $zoomScrollEnabled = atom<boolean>(true)

export function setZoomScrollEnabled(enabled: boolean): void {
  $zoomScrollEnabled.set(enabled)
  window.hermesDesktop?.zoom?.setScrollEnabled(enabled)
}

if (typeof window !== 'undefined' && window.hermesDesktop?.zoom) {
  void window.hermesDesktop.zoom.getScrollEnabled().then(enabled => $zoomScrollEnabled.set(enabled))
}
