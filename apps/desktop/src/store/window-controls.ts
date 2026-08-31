import { atom } from 'nanostores'

export type WindowControlsMode = 'hidden' | 'native' | 'system'

const DEFAULT_WINDOW_CONTROLS_MODE: WindowControlsMode = 'system'

function normalize(value: unknown): WindowControlsMode {
  return value === 'hidden' || value === 'native' || value === 'system' ? value : DEFAULT_WINDOW_CONTROLS_MODE
}

const bridge = typeof window !== 'undefined' ? window.hermesDesktop?.windowControls : undefined

/** Main-process preference for Electron's right-side window control overlay. */
export const $windowControlsMode = atom<WindowControlsMode>(normalize(bridge?.mode))

/** This option only changes a Linux WCO; other platforms keep their native chrome. */
export const WINDOW_CONTROLS_SUPPORTED = bridge?.supported === true

export function setWindowControlsMode(mode: WindowControlsMode): void {
  const next = normalize(mode)
  $windowControlsMode.set(next)
  bridge?.setMode(next)
}

bridge?.onChanged(mode => $windowControlsMode.set(normalize(mode)))
