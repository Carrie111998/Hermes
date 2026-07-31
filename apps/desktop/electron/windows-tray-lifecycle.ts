type Platform = NodeJS.Platform

export function shouldCreateWindowsTray(platform: Platform): boolean {
  return platform === 'win32'
}

export function shouldHideMainWindowOnClose({
  platform,
  isQuitting
}: {
  platform: Platform
  isQuitting: boolean
}): boolean {
  return platform === 'win32' && !isQuitting
}

export function shouldStartMainWindowHidden({
  platform,
  argv
}: {
  platform: Platform
  argv: readonly string[]
}): boolean {
  return platform === 'win32' && argv.includes('--hidden')
}
