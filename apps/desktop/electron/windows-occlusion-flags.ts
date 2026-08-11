/**
 * Chromium command-line switches that keep the Windows UI task runner from
 * stalling when a Hermes window is minimized or fully occluded (#83420).
 *
 * After stream-scoped throttling landed, idle chat windows return to Chromium's
 * default occlusion/backgrounding path. On Windows that path can park the
 * browser main thread on a WaitableEvent with no message-pump wake — silent
 * freeze, no exception. The perf harness already uses these same flags when
 * the window sits behind the IDE; production needs the Windows-only subset.
 *
 * Intentionally omitted (those pinned visibility forever and burned ~20% CPU
 * at idle):
 *   - disable-background-timer-throttling
 *   - static webPreferences.backgroundThrottling: false
 *
 * Stream-throttle still dials setBackgroundThrottling for live turns.
 */

export interface CommandLineSwitch {
  switchName: string
  value?: string
}

export function windowsOcclusionCommandLineSwitches(
  platform: NodeJS.Platform | string = process.platform
): CommandLineSwitch[] {
  if (platform !== 'win32') {
    return []
  }

  return [
    { switchName: 'disable-backgrounding-occluded-windows' },
    { switchName: 'disable-features', value: 'CalculateNativeWinOcclusion' }
  ]
}

export function applyWindowsOcclusionCommandLineSwitches(
  appendSwitch: (switchName: string, value?: string) => void,
  platform: NodeJS.Platform | string = process.platform
): void {
  for (const flag of windowsOcclusionCommandLineSwitches(platform)) {
    if (flag.value === undefined) {
      appendSwitch(flag.switchName)
    } else {
      appendSwitch(flag.switchName, flag.value)
    }
  }
}
