import { execFile as nodeExecFile, spawn as nodeSpawn } from 'node:child_process'
import type { ExecFileOptions, SpawnOptions } from 'node:child_process'

import { type BotDesktopProvider, normalizeBotDesktopProfile } from './bot-desktop-runtime'

export const BOT_DESKTOP_WINDOWS_RESOLUTION = '1280x800x24'
export const BOT_DESKTOP_WINDOWS_DISPLAY_START = 100
export const BOT_DESKTOP_WINDOWS_VNC_PORT_START = 5900
export const BOT_DESKTOP_WINDOWS_VIEWER_PORT_START = 6080
export const BOT_DESKTOP_WINDOWS_START_TIMEOUT_MS = 15_000
export const BOT_DESKTOP_WINDOWS_STOP_TIMEOUT_MS = 1_500

export interface BotDesktopWindowsDisplayInfo {
  platform: 'windows' | 'unsupported'
  provider: BotDesktopProvider | 'none'
  supported: boolean
  running: boolean
  display: string | null
  pid: number | null
  resolution: string
  error?: string
  distro?: string
  image?: string
  containerName?: string
  viewerUrl?: string
  viewerPort?: number
  vncPort?: number
  executionBoundary: BotDesktopProvider | 'none'
  /** The native Windows Hermes backend cannot inherit a Linux DISPLAY. */
  nativeBackendInherited: false
}

interface StreamLike {
  on(event: 'data', listener: (chunk: string | Buffer) => void): unknown
}

interface ChildLike {
  stdout?: StreamLike | null
  stderr?: StreamLike | null
  exitCode?: number | null
  once(event: 'error' | 'exit', listener: (...args: any[]) => void): unknown
  kill(signal?: NodeJS.Signals | number): boolean
}

type SpawnLike = (file: string, args: string[], options: SpawnOptions) => ChildLike
type ExecFileLike = (
  file: string,
  args: string[],
  options: ExecFileOptions,
  callback: (error: Error | null, stdout: string | Buffer, stderr: string | Buffer) => void
) => { kill?: (signal?: NodeJS.Signals | number) => boolean }

export interface CreateBotDesktopWindowsRuntimeOptions {
  platform?: NodeJS.Platform
  provider?: BotDesktopProvider
  wslExecutable?: string
  wslDistro?: string
  dockerExecutable?: string
  dockerImage?: string
  workspacePathForProfile?: (profile: string) => string
  displayStart?: number
  resolution?: string
  startTimeoutMs?: number
  stopTimeoutMs?: number
  spawn?: SpawnLike
  execFile?: ExecFileLike
  log?: (message: string) => void
}

interface WindowsDisplaySession {
  info: BotDesktopWindowsDisplayInfo
  child?: ChildLike
  linuxPid?: number
}

interface CommandResult {
  stdout: string
  stderr: string
}

function asText(value: string | Buffer): string {
  return typeof value === 'string' ? value : value.toString('utf8')
}

function posixQuote(value: string): string {
  return `'${value.replaceAll("'", "'\\''")}'`
}

function readyMarker(profile: string): string {
  return `/tmp/hermes-bot-desktop-xvfb-${profile}.ready`
}

function displayNumber(display: string): number {
  const match = String(display || '').match(/^:(\d+)$/)

  if (!match) {
    throw new Error('Bot Desktop Windows DISPLAY must look like :N.')
  }

  return Number(match[1])
}

function portForDisplay(display: string, start: number): number {
  return start + displayNumber(display)
}

function isWslDisplayConflict(error: unknown): boolean {
  const message = error instanceof Error ? error.message : String(error)

  return /already active for display|address already in use|cannot establish display/i.test(message)
}

export function botDesktopViewerUrl(viewerPort: number): string {
  return `http://127.0.0.1:${viewerPort}/vnc.html?autoconnect=1&resize=scale&view_clip=0&reconnect=1&reconnect_delay=500&host=127.0.0.1&port=${viewerPort}&path=websockify`
}

export function windowsPathToWslPath(rawPath: string): string {
  const value = String(rawPath || '').trim()
  const drivePath = value.match(/^([A-Za-z]):[\\/](.*)$/)

  if (drivePath) {
    return `/mnt/${drivePath[1].toLowerCase()}/${drivePath[2].replaceAll('\\', '/')}`
  }

  return value.replaceAll('\\', '/')
}

function normalizeResolution(raw: string): string {
  const resolution = String(raw || BOT_DESKTOP_WINDOWS_RESOLUTION).trim()

  if (!/^\d{3,5}x\d{3,5}x(?:8|16|24|32)$/.test(resolution)) {
    throw new Error('Bot Desktop Windows resolution must be WIDTHxHEIGHTxDEPTH.')
  }

  return resolution
}

/**
 * The same bounded launcher is used by WSL and Docker. Every profile gets a
 * display number, VNC port, noVNC port, browser data directory, and cleanup
 * trap of its own. The launcher is intentionally shell-only so it works with
 * a stock WSL distribution and with a small Docker image.
 */
export function buildBotDesktopLinuxLauncher(
  profile: string,
  display: string,
  resolution: string,
  viewerPort: number,
  vncPort: number,
  chromeDataPath: string,
  marker?: string,
  viewerBindHost = '127.0.0.1'
): string {
  const finalReadyCommand = marker
    ? `printf '%s\\n' "$pid" > ${posixQuote(marker)}`
    : `printf '__HERMES_BOT_DESKTOP_READY__=%s\\n' "$pid"`

  const probe = `DISPLAY=${posixQuote(display)} xdpyinfo >/dev/null 2>&1`
  const viewerProbe = `curl --fail --silent --show-error --max-time 1 http://127.0.0.1:${viewerPort}/vnc.html >/dev/null 2>&1`

  const vncProbe = `python3 -c ${posixQuote(
    `import socket; s=socket.create_connection(('127.0.0.1', ${vncPort}), 1); s.close()`
  )} 2>/dev/null`

  const browserArgs = `--no-first-run --no-default-browser-check --disable-gpu --disable-dev-shm-usage --password-store=basic --user-data-dir=${posixQuote(chromeDataPath)} https://example.com`
  const fallbackTitle = `Hermes Bot Desktop (${profile})`

  return [
    'set -eu',
    `export DISPLAY=${posixQuote(display)}`,
    'unset WAYLAND_DISPLAY WAYLAND_SOCKET || true',
    'export XDG_SESSION_TYPE=x11 GDK_BACKEND=x11',
    `mkdir -p ${posixQuote(chromeDataPath)}`,
    'cleanup() { for child_pid in "${viewer_pid:-}" "${vnc_pid:-}" "${browser_pid:-}" "${wm_pid:-}" "${pid:-}"; do if [ -n "$child_pid" ]; then kill -TERM "$child_pid" 2>/dev/null || true; fi; done; }',
    'trap cleanup EXIT INT TERM',
    `Xvfb ${posixQuote(display)} -screen 0 ${posixQuote(resolution)} -nolisten tcp >/tmp/hermes-bot-desktop-xvfb-${profile}.log 2>&1 & pid=$!`,
    'ready=0',
    'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do :',
    '  if ! kill -0 "$pid" 2>/dev/null; then wait "$pid"; exit 1; fi',
    `  if ${probe}; then ready=1; break; fi`,
    '  sleep 0.05',
    'done',
    'if [ "$ready" -ne 1 ]; then kill -TERM "$pid" 2>/dev/null || true; wait "$pid" || true; exit 1; fi',
    'if command -v openbox >/dev/null 2>&1; then openbox --sm-disable >/tmp/hermes-bot-desktop-openbox.log 2>&1 & wm_pid=$!; else wm_pid=""; fi',
    `if command -v google-chrome >/dev/null 2>&1; then browser=google-chrome; elif command -v chromium >/dev/null 2>&1; then browser=chromium; elif command -v chromium-browser >/dev/null 2>&1; then browser=chromium-browser; else browser=""; fi`,
    `if [ -n "$browser" ]; then browser_sandbox=""; if [ "$(id -u)" -eq 0 ]; then browser_sandbox="--no-sandbox"; fi; $browser $browser_sandbox ${browserArgs} >/tmp/hermes-bot-desktop-browser-${profile}.log 2>&1 & browser_pid=$!; else xterm -T ${posixQuote(fallbackTitle)} -geometry 110x30+20+20 -e sh -lc ${posixQuote(`printf 'Linux Bot Desktop: ${profile}\\n\\n'; exec sh`)} >/tmp/hermes-bot-desktop-xterm-${profile}.log 2>&1 & browser_pid=$!; fi`,
    `env -u WAYLAND_DISPLAY -u WAYLAND_SOCKET x11vnc -display ${posixQuote(display)} -rfbport ${vncPort} -localhost -nopw -forever -shared -noxdamage -repeat >/tmp/hermes-bot-desktop-x11vnc-${profile}.log 2>&1 & vnc_pid=$!`,
    'vnc_ready=0',
    'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40; do :',
    `  if ${vncProbe}; then vnc_ready=1; break; fi`,
    '  if ! kill -0 "$vnc_pid" 2>/dev/null; then wait "$vnc_pid"; exit 1; fi',
    '  sleep 0.05',
    'done',
    'if [ "$vnc_ready" -ne 1 ]; then exit 1; fi',
    `websockify --web=/usr/share/novnc ${viewerBindHost}:${viewerPort} 127.0.0.1:${vncPort} >/tmp/hermes-bot-desktop-websockify-${profile}.log 2>&1 & viewer_pid=$!`,
    'viewer_ready=0',
    'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20 21 22 23 24 25 26 27 28 29 30 31 32 33 34 35 36 37 38 39 40; do :',
    `  if ${viewerProbe}; then viewer_ready=1; break; fi`,
    '  if ! kill -0 "$viewer_pid" 2>/dev/null; then wait "$viewer_pid"; exit 1; fi',
    '  sleep 0.05',
    'done',
    'if [ "$viewer_ready" -ne 1 ]; then exit 1; fi',
    finalReadyCommand,
    'wait "$pid"'
  ].join('; ')
}

function commandError(file: string, error: Error, stderr: string): Error {
  const detail = stderr.trim()

  return new Error(`${file} failed: ${error.message}${detail ? `: ${detail}` : ''}`)
}

function runCommand(execFile: ExecFileLike, file: string, args: string[], timeoutMs: number): Promise<CommandResult> {
  return new Promise((resolve, reject) => {
    let settled = false
    let child: { kill?: (signal?: NodeJS.Signals | number) => boolean } | undefined

    const timer = setTimeout(() => {
      if (settled) {
        return
      }

      settled = true

      try {
        child?.kill?.('SIGTERM')
      } catch {
        // The timeout is already the result we need to report.
      }

      reject(new Error(`${file} timed out after ${timeoutMs}ms`))
    }, timeoutMs)

    try {
      child = execFile(
        file,
        args,
        { timeout: timeoutMs, maxBuffer: 64 * 1024, windowsHide: true },
        (error, stdout, stderr) => {
          if (settled) {
            return
          }

          settled = true
          clearTimeout(timer)
          const stdoutText = asText(stdout)
          const stderrText = asText(stderr)

          if (error) {
            reject(commandError(file, error, stderrText))

            return
          }

          resolve({ stdout: stdoutText, stderr: stderrText })
        }
      )
    } catch (error) {
      if (settled) {
        return
      }

      settled = true
      clearTimeout(timer)
      reject(error)
    }
  })
}

function waitForChildExit(child: ChildLike, timeoutMs: number): Promise<void> {
  if (child.exitCode !== undefined && child.exitCode !== null) {
    return Promise.resolve()
  }

  return new Promise((resolve, reject) => {
    let settled = false

    const timer = setTimeout(() => {
      if (settled) {
        return
      }

      settled = true
      reject(new Error(`process did not exit within ${timeoutMs}ms`))
    }, timeoutMs)

    const finish = () => {
      if (settled) {
        return
      }

      settled = true
      clearTimeout(timer)
      resolve()
    }

    child.once('exit', finish)
    child.once('error', finish)
  })
}

function waitForWslReady(child: ChildLike, timeoutMs: number): Promise<number> {
  return new Promise((resolve, reject) => {
    let settled = false
    let output = ''
    let errorOutput = ''

    const timer = setTimeout(() => {
      if (settled) {
        return
      }

      settled = true
      reject(new Error(`WSL Xvfb did not become ready within ${timeoutMs}ms`))
    }, timeoutMs)

    const fail = (error: Error) => {
      if (settled) {
        return
      }

      settled = true
      clearTimeout(timer)
      reject(error)
    }

    const onData = (chunk: string | Buffer) => {
      output += asText(chunk)
      const match = output.match(/__HERMES_BOT_DESKTOP_READY__=(\d+)/)

      if (!match || settled) {
        return
      }

      settled = true
      clearTimeout(timer)
      resolve(Number(match[1]))
    }

    child.stdout?.on('data', onData)
    child.stderr?.on('data', chunk => {
      errorOutput += asText(chunk)
    })
    child.once('error', error => fail(error instanceof Error ? error : new Error(String(error))))
    child.once('exit', (code, signal) => {
      if (settled) {
        return
      }

      const detail = errorOutput.trim()
      fail(
        new Error(
          `WSL Bot Desktop exited before ready (code=${String(code)}, signal=${String(signal)})${detail ? `: ${detail}` : ''}`
        )
      )
    })
  })
}

async function terminateChild(child: ChildLike, timeoutMs: number): Promise<void> {
  try {
    child.kill('SIGTERM')
  } catch {
    return
  }

  try {
    await waitForChildExit(child, timeoutMs)
  } catch {
    child.kill('SIGKILL')
  }
}

function createUnsupportedInfo(): BotDesktopWindowsDisplayInfo {
  return {
    platform: 'unsupported',
    provider: 'none',
    supported: false,
    running: false,
    display: null,
    pid: null,
    resolution: BOT_DESKTOP_WINDOWS_RESOLUTION,
    error: 'Bot Desktop WSL/Docker runtime is available only when Hermes Desktop runs on Windows.',
    executionBoundary: 'none',
    nativeBackendInherited: false
  }
}

export function createBotDesktopWindowsRuntime(options: CreateBotDesktopWindowsRuntimeOptions = {}) {
  const platform = options.platform ?? process.platform
  const provider = options.provider ?? 'wsl'
  const wslExecutable = options.wslExecutable ?? 'wsl.exe'
  const wslDistro = options.wslDistro ?? 'Ubuntu'
  const dockerExecutable = options.dockerExecutable ?? 'docker.exe'
  const dockerImage = options.dockerImage ?? 'hermes-bot-desktop-xvfb:local'
  const workspacePathForProfile = options.workspacePathForProfile ?? (profile => `/tmp/hermes-bot-desktop-${profile}`)
  const displayStart = options.displayStart ?? BOT_DESKTOP_WINDOWS_DISPLAY_START

  if (!Number.isInteger(displayStart) || displayStart < 1 || displayStart > 9999) {
    throw new Error('Bot Desktop Windows displayStart must be an integer between 1 and 9999.')
  }

  const resolution = normalizeResolution(options.resolution ?? BOT_DESKTOP_WINDOWS_RESOLUTION)
  const startTimeoutMs = Math.max(1000, Number(options.startTimeoutMs) || BOT_DESKTOP_WINDOWS_START_TIMEOUT_MS)
  const stopTimeoutMs = Math.max(100, Number(options.stopTimeoutMs) || BOT_DESKTOP_WINDOWS_STOP_TIMEOUT_MS)
  const spawn = options.spawn ?? ((file, args, spawnOptions) => nodeSpawn(file, args, spawnOptions))

  const execFile =
    options.execFile ?? ((file, args, execOptions, callback) => nodeExecFile(file, args, execOptions, callback))

  const log = options.log ?? (() => undefined)
  const sessions = new Map<string, WindowsDisplaySession>()
  const starts = new Map<string, Promise<BotDesktopWindowsDisplayInfo>>()
  const containerInstance = `${process.pid}-${Date.now()}`
  let nextContainer = 0
  let nextDisplay = displayStart

  const baseInfo = (display: string | null): BotDesktopWindowsDisplayInfo => ({
    platform: 'windows',
    provider,
    supported: true,
    running: false,
    display,
    pid: null,
    resolution,
    ...(provider === 'wsl' ? { distro: wslDistro } : { image: dockerImage }),
    executionBoundary: provider,
    nativeBackendInherited: false
  })

  const allocateDisplay = () => {
    const value = nextDisplay
    nextDisplay += 1

    return `:${value}`
  }

  const startWsl = async (profile: string, initialDisplay: string): Promise<BotDesktopWindowsDisplayInfo> => {
    let display = initialDisplay
    let lastError: unknown

    for (let attempt = 0; attempt < 16; attempt += 1) {
      try {
        await runCommand(
          execFile,
          wslExecutable,
          ['-d', wslDistro, '--exec', 'sh', '-lc', `DISPLAY=${posixQuote(display)} xdpyinfo >/dev/null 2>&1`],
          Math.min(startTimeoutMs, 1500)
        )
        log(`[bot-desktop] WSL display ${display} is already in use; selecting a fresh display.`)
        display = allocateDisplay()

        continue
      } catch {
        // The real launcher below reports a missing WSL/X11 dependency.
      }

      const viewerPort = portForDisplay(display, BOT_DESKTOP_WINDOWS_VIEWER_PORT_START)
      const vncPort = portForDisplay(display, BOT_DESKTOP_WINDOWS_VNC_PORT_START)
      const chromeDataPath = `${windowsPathToWslPath(workspacePathForProfile(profile))}/linux-chrome-profile`

      const child = spawn(
        wslExecutable,
        [
          '-d',
          wslDistro,
          '--exec',
          'sh',
          '-lc',
          buildBotDesktopLinuxLauncher(profile, display, resolution, viewerPort, vncPort, chromeDataPath)
        ],
        { stdio: ['ignore', 'pipe', 'pipe'], windowsHide: true }
      )

      try {
        const linuxPid = await waitForWslReady(child, startTimeoutMs)

        const info: BotDesktopWindowsDisplayInfo = {
          ...baseInfo(display),
          running: true,
          pid: linuxPid,
          viewerPort,
          vncPort,
          viewerUrl: botDesktopViewerUrl(viewerPort)
        }

        sessions.set(profile, { info, child, linuxPid })
        child.once('exit', (code, signal) => {
          const current = sessions.get(profile)

          if (!current || current.child !== child) {
            return
          }

          current.info = {
            ...current.info,
            running: false,
            pid: null,
            error: `WSL Bot Desktop exited (${String(signal || code || 'unknown')}).`
          }
        })

        return info
      } catch (error) {
        lastError = error
        await terminateChild(child, stopTimeoutMs)

        if (!isWslDisplayConflict(error) || attempt === 15) {
          throw error
        }

        log(`[bot-desktop] WSL display ${display} is busy; retrying with a fresh display.`)
        display = allocateDisplay()
      }
    }

    throw lastError instanceof Error ? lastError : new Error('Bot Desktop WSL display could not be allocated.')
  }

  const startDocker = async (profile: string, display: string): Promise<BotDesktopWindowsDisplayInfo> => {
    const containerName = `hermes-bot-desktop-xvfb-${containerInstance}-${nextContainer++}-${profile}`
    const marker = readyMarker(profile)
    const viewerPort = portForDisplay(display, BOT_DESKTOP_WINDOWS_VIEWER_PORT_START)
    const vncPort = portForDisplay(display, BOT_DESKTOP_WINDOWS_VNC_PORT_START)
    let runAttempted = false

    try {
      const existing = await runCommand(
        execFile,
        dockerExecutable,
        ['ps', '--all', '--filter', `name=^${containerName}$`, '--format', '{{.Names}}'],
        startTimeoutMs
      )

      if (existing.stdout.trim()) {
        throw new Error(`Docker container name already exists; refusing to remove it: ${containerName}`)
      }

      runAttempted = true
      await runCommand(
        execFile,
        dockerExecutable,
        [
          'run',
          '--detach',
          '--name',
          containerName,
          '--publish',
          `127.0.0.1:${viewerPort}:${viewerPort}`,
          '--volume',
          `${workspacePathForProfile(profile)}:/workspace`,
          dockerImage,
          'sh',
          '-lc',
          buildBotDesktopLinuxLauncher(
            profile,
            display,
            resolution,
            viewerPort,
            vncPort,
            '/workspace/linux-chrome-profile',
            marker,
            '0.0.0.0'
          )
        ],
        startTimeoutMs
      )
      await runCommand(
        execFile,
        dockerExecutable,
        [
          'exec',
          containerName,
          'sh',
          '-lc',
          `DISPLAY=${posixQuote(display)} xdpyinfo >/dev/null 2>&1 && test -s ${posixQuote(marker)}`
        ],
        startTimeoutMs
      )

      const info: BotDesktopWindowsDisplayInfo = {
        ...baseInfo(display),
        running: true,
        containerName,
        viewerPort,
        vncPort,
        viewerUrl: botDesktopViewerUrl(viewerPort)
      }

      sessions.set(profile, { info })

      return info
    } catch (error) {
      if (runAttempted) {
        try {
          await runCommand(execFile, dockerExecutable, ['rm', '--force', containerName], stopTimeoutMs)
        } catch {
          log(`[bot-desktop] Docker cleanup could not confirm removal: ${containerName}`)
        }
      }

      throw error
    }
  }

  const start = async (profile: string): Promise<BotDesktopWindowsDisplayInfo> => {
    if (platform !== 'win32') {
      return createUnsupportedInfo()
    }

    const display = allocateDisplay()

    try {
      return provider === 'wsl' ? await startWsl(profile, display) : await startDocker(profile, display)
    } catch (error) {
      const failed: BotDesktopWindowsDisplayInfo = {
        ...baseInfo(null),
        supported: false,
        error: error instanceof Error ? error.message : String(error)
      }

      sessions.set(profile, { info: failed })

      return failed
    }
  }

  const ensure = (profileInput: string): Promise<BotDesktopWindowsDisplayInfo> => {
    const profile = normalizeBotDesktopProfile(profileInput)
    const current = sessions.get(profile)

    if (current?.info.running) {
      return Promise.resolve({ ...current.info })
    }

    const pending = starts.get(profile)

    if (pending) {
      return pending.then(info => ({ ...info }))
    }

    const operation = start(profile).finally(() => starts.delete(profile))
    starts.set(profile, operation)

    return operation.then(info => ({ ...info }))
  }

  const getInfo = (profileInput: string): BotDesktopWindowsDisplayInfo => {
    const profile = normalizeBotDesktopProfile(profileInput)

    if (platform !== 'win32') {
      return createUnsupportedInfo()
    }

    return { ...(sessions.get(profile)?.info ?? baseInfo(null)) }
  }

  const stop = async (profileInput: string): Promise<BotDesktopWindowsDisplayInfo> => {
    const profile = normalizeBotDesktopProfile(profileInput)
    const current = sessions.get(profile)

    if (!current) {
      return getInfo(profile)
    }

    sessions.delete(profile)

    if (provider === 'wsl' && current.child) {
      if (current.linuxPid) {
        try {
          await runCommand(
            execFile,
            wslExecutable,
            ['-d', wslDistro, '--exec', 'kill', '-TERM', String(current.linuxPid)],
            stopTimeoutMs
          )
        } catch (error) {
          log(`[bot-desktop] WSL stop command failed: ${error instanceof Error ? error.message : String(error)}`)
        }
      }

      try {
        await waitForChildExit(current.child, stopTimeoutMs)
      } catch {
        current.child.kill('SIGKILL')
      }
    }

    if (provider === 'docker' && current.info.containerName) {
      try {
        await runCommand(execFile, dockerExecutable, ['stop', '--time', '1', current.info.containerName], stopTimeoutMs)
      } catch (error) {
        log(`[bot-desktop] Docker stop command failed: ${error instanceof Error ? error.message : String(error)}`)
      }

      try {
        await runCommand(execFile, dockerExecutable, ['rm', '--force', current.info.containerName], stopTimeoutMs)
      } catch {
        log(`[bot-desktop] Docker cleanup could not confirm removal: ${current.info.containerName}`)
      }
    }

    return { ...current.info, running: false, pid: null }
  }

  const closeAll = async (): Promise<void> => {
    await Promise.all([...sessions.keys()].map(profile => stop(profile)))
  }

  return { closeAll, ensure, getInfo, stop }
}

export type BotDesktopWindowsRuntime = ReturnType<typeof createBotDesktopWindowsRuntime>
