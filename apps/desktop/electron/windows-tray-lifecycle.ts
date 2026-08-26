type Platform = NodeJS.Platform

export function shouldCreateWindowsTray(platform: Platform): boolean {
  return platform === 'win32'
}

/** Windows does not emit app.before-quit for OS shutdown, restart, or logout. */
export function shouldTreatSessionEndAsFinalQuit(platform: Platform): boolean {
  return platform === 'win32'
}

export function shouldHideMainWindowOnClose({
  platform,
  isQuitting,
  trayAvailable
}: {
  platform: Platform
  isQuitting: boolean
  trayAvailable: boolean
}): boolean {
  return platform === 'win32' && trayAvailable && !isQuitting
}

export function shouldStartMainWindowHidden({
  platform,
  argv,
  trayAvailable
}: {
  platform: Platform
  argv: readonly string[]
  trayAvailable: boolean
}): boolean {
  return platform === 'win32' && trayAvailable && argv.includes('--hidden')
}
