import type { ChildProcess } from 'node:child_process'

type Spawn = (command: string, args: string[], options: Record<string, unknown>) => ChildProcess

type ShellOpenExternal = (url: string) => Promise<void>

export function openExternalUrlForPlatform(
  url: string,
  {
    isWsl,
    spawn,
    shellOpenExternal,
    onError = () => undefined
  }: {
    isWsl: boolean
    spawn: Spawn
    shellOpenExternal: ShellOpenExternal
    onError?: (error: unknown) => void
  }
): boolean {
  if (isWsl) {
    const proc = spawn('explorer.exe', [url], {
      detached: true,
      stdio: 'ignore',
      windowsHide: true
    })

    proc.on('error', error => {
      onError(error)
      void shellOpenExternal(url).catch(fallback => {
        onError(fallback)
        void fallback
      })
    })
    proc.unref()
    return true
  }

  void shellOpenExternal(url).catch(() => undefined)
  return true
}
