/**
 * Open an http(s)/mailto URL from WSL onto the Windows host without
 * going through ``cmd.exe /c start``.
 *
 * ``cmd.exe /c start "" <url>`` is assembled unquoted by WSL binfmt
 * interop when the URL has no spaces. ``&`` in a query string then
 * becomes a command separator, so a chat link like
 * ``https://example.com/?a=1&calc&z=2`` runs ``calc`` on the host.
 *
 * ``rundll32.exe url.dll,FileProtocolHandler`` takes the URL as a
 * single argument and does not re-parse it as a cmd command line.
 */

export interface WslOpenUrlChild {
  on(event: 'error', listener: (error: Error) => void): unknown
  on(
    event: 'exit' | 'close',
    listener: (code: number | null, signal?: NodeJS.Signals | null) => void
  ): unknown
  unref?: () => void
}

export interface OpenWslExternalUrlDeps {
  spawn: (
    command: string,
    args: string[],
    options: { detached: boolean; stdio: 'ignore'; windowsHide: boolean }
  ) => WslOpenUrlChild
  fallback: (url: string) => void | Promise<void>
  log: (message: string) => void
}

export function wslWindowsOpenUrlArgs(url: string): { command: string; args: string[] } {
  return {
    command: 'rundll32.exe',
    args: ['url.dll,FileProtocolHandler', url]
  }
}

function isFailedTermination(code: number | null | undefined, signal?: NodeJS.Signals | null): boolean {
  if (signal) {
    return true
  }

  return typeof code === 'number' && code !== 0
}

/**
 * Production WSL launch seam. Callers inject spawn, fallback, and logging
 * so the real argv + failure path can be exercised without Electron.
 */
export function openWslExternalUrl(url: string, deps: OpenWslExternalUrlDeps): boolean {
  const launched = wslWindowsOpenUrlArgs(url)

  deps.log(`[link] opening via WSL→Windows: ${url}`)

  let fallbackUsed = false

  const fallbackOnce = (reason: string) => {
    if (fallbackUsed) {
      return
    }

    fallbackUsed = true
    deps.log(`[link] rundll32 FileProtocolHandler failed: ${reason}; falling back to xdg-open`)
    void Promise.resolve(deps.fallback(url)).catch((fallbackError: Error) => {
      deps.log(`[link] xdg-open failed: ${fallbackError.message}`)
    })
  }

  let proc: WslOpenUrlChild

  try {
    proc = deps.spawn(launched.command, launched.args, {
      detached: true,
      stdio: 'ignore',
      windowsHide: true
    })
  } catch (error) {
    const err = error as Error
    fallbackOnce(err?.message || 'spawn threw')

    return true
  }

  const onDone = (code: number | null, signal?: NodeJS.Signals | null) => {
    if (isFailedTermination(code, signal)) {
      fallbackOnce(signal ? `signal ${signal}` : `exited ${code}`)
    }
  }

  proc.on('error', error => {
    fallbackOnce(error.message)
  })
  proc.on('exit', onDone)
  proc.on('close', onDone)
  proc.unref?.()

  return true
}
