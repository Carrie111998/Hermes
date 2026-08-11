/**
 * Hermes Office (Claw3d) manager — spawn/manage the `fathah/hermes-office`
 * 3D interface: clone + install the repo under HERMES_HOME/hermes-office,
 * write its .env + ~/.openclaw/claw3d/settings.json from the active Hermes
 * connection, run the Next.js dev server (:3000) and the
 * hermes-gateway-adapter (:18989), and report status/logs to the renderer.
 *
 * Ported from the community `fathah/hermes-desktop` src/main/claw3d.ts,
 * adapted to this app's connection model (the connection resolver is injected
 * by electron/main.ts instead of reading fathah's installer config).
 *
 * Office needs a local/token gateway connection: the adapter authenticates
 * with the gateway's session token via the legacy `Bearer` header, which the
 * Hermes HTTP API accepts. OAuth-gated gateways are surfaced as unsupported.
 */
import type { ChildProcess} from 'child_process';
import { execFileSync, spawn, spawnSync } from 'child_process'
import { existsSync, mkdirSync, readdirSync, readFileSync, unlinkSync, writeFileSync } from 'fs'
import http from 'http'
import { createConnection } from 'net'
import { homedir } from 'os'
import { join } from 'path'

const HERMES_OFFICE_REPO = 'https://github.com/fathah/hermes-office'
const DEFAULT_PORT = 3000
const DEFAULT_ADAPTER_PORT = 18989
const CLAW3D_SETTINGS_DIR = join(homedir(), '.openclaw', 'claw3d')

function hermesHome(): string {
  return process.env.HERMES_HOME || join(homedir(), '.hermes')
}

const HERMES_OFFICE_DIR = join(hermesHome(), 'hermes-office')
const DEV_PID_FILE = join(hermesHome(), 'claw3d-dev.pid')
const ADAPTER_PID_FILE = join(hermesHome(), 'claw3d-adapter.pid')
const PORT_FILE = join(hermesHome(), 'claw3d-port')

/** Connection facts the renderer-facing manager needs (injected by main). */
export interface OfficeConnection {
  baseUrl: string
  token: string
  authMode: 'token' | 'oauth' | 'none' | string
}

type ConnectionResolver = (profile?: string) => Promise<OfficeConnection>

let _resolveConnection: ConnectionResolver | null = null

/** Inject the connection resolver (called once from electron/main.ts). */
export function setConnectionResolver(resolver: ConnectionResolver | null): void {
  _resolveConnection = resolver
}

async function resolveConnection(profile?: string): Promise<OfficeConnection> {
  if (!_resolveConnection) {
    return { baseUrl: `http://127.0.0.1:8642`, token: '', authMode: 'none' }
  }

  return _resolveConnection(profile)
}

let devServerProcess: ChildProcess | null = null
let adapterProcess: ChildProcess | null = null
let devServerLogs = ''
let adapterLogs = ''
let devServerError = ''
let adapterError = ''

export interface ResolvedCommand {
  command: string
  windowsScript: boolean
}

export interface CommandInvocation {
  command: string
  args: string[]
  windowsVerbatimArguments?: boolean
}

type Claw3dScript = 'dev' | 'hermes-adapter'

const CLAW3D_SCRIPT_ARGS: Record<Claw3dScript, string[]> = {
  dev: ['server/index.js', '--dev'],
  'hermes-adapter': ['server/hermes-gateway-adapter.js']
}

export function isWindowsCommandScript(command: string): boolean {
  return /\.(cmd|bat)$/i.test(command)
}

export function pickWindowsCommandCandidate(candidates: string[]): ResolvedCommand | null {
  const normalized = candidates.map(candidate => candidate.trim()).filter(Boolean)
  const executable = normalized.find(candidate => /\.exe$/i.test(candidate))

  if (executable) {return { command: executable, windowsScript: false }}
  const script = normalized.find(isWindowsCommandScript)

  if (script) {return { command: script, windowsScript: true }}
  const fallback = normalized[0]

  return fallback ? { command: fallback, windowsScript: false } : null
}

export function resolveCommandOnPath(command: string, envPath: string): ResolvedCommand | null {
  const lookupCommand = process.platform === 'win32' ? 'where.exe' : 'which'

  const result = spawnSync(lookupCommand, [command], {
    encoding: 'utf8',
    env: { ...process.env, PATH: envPath },
    timeout: 5000,
    windowsHide: true
  })

  if (result.error || result.status !== 0 || !result.stdout) {return null}
  const candidates = result.stdout.split(/\r?\n/)

  if (process.platform === 'win32') {return pickWindowsCommandCandidate(candidates)}
  const resolved = candidates.map(candidate => candidate.trim()).find(Boolean)

  return resolved ? { command: resolved, windowsScript: false } : null
}

export function resolveCommand(command: string, envPath: string): ResolvedCommand {
  const resolved = resolveCommandOnPath(command, envPath)

  if (resolved) {return resolved}

  return { command, windowsScript: process.platform === 'win32' && isWindowsCommandScript(command) }
}

export function buildWindowsScriptCommandLine(
  command: string,
  args: string[],
  options: { verbatim?: boolean } = {}
): { command: string; args: string[]; windowsVerbatimArguments?: boolean } {
  if (options.verbatim && isWindowsCommandScript(command)) {
    return { command, args, windowsVerbatimArguments: true }
  }

  return { command, args }
}

export function createNpmCommandInvocation(resolved: ResolvedCommand, args: string[]): CommandInvocation {
  return buildWindowsScriptCommandLine(resolved.command, args, {
    verbatim: resolved.windowsScript
  })
}

export function createClaw3dScriptInvocation(script: Claw3dScript, nodeCommand: string): CommandInvocation {
  return {
    command: nodeCommand,
    args: CLAW3D_SCRIPT_ARGS[script]
  }
}

export function setClaw3dPort(port: number): void {
  safeWriteFile(PORT_FILE, String(port))
}

export function getClaw3dPort(): number {
  try {
    const parsed = parseInt(readFileSync(PORT_FILE, 'utf-8').trim(), 10)

    if (!isNaN(parsed) && parsed > 0 && parsed < 65536) {return parsed}
  } catch {
    /* fresh */
  }

  return DEFAULT_PORT
}

export function adapterPortFromWsUrl(url: string): number {
  try {
    const parsed = new URL(url)
    const port = parsed.port ? parseInt(parsed.port, 10) : NaN

    if (!isNaN(port)) {return port + 21}
  } catch {
    /* not a URL */
  }

  return DEFAULT_ADAPTER_PORT
}

export function buildOfficeEnv(opts: {
  port: number
  url: string
  apiUrl: string
  apiKey: string
  model: string
  adapterPort?: number
}): string {
  const adapterPort = opts.adapterPort ?? adapterPortFromWsUrl(opts.url)

  return [
    '# Auto-configured by Hermes Desktop',
    `PORT=${opts.port}`,
    `HOST=127.0.0.1`,
    `NEXT_PUBLIC_GATEWAY_URL=${opts.url}`,
    `CLAW3D_GATEWAY_URL=${opts.url}`,
    `CLAW3D_GATEWAY_TOKEN=${opts.apiKey}`,
    `CLAW3D_GATEWAY_ADAPTER_TYPE=hermes`,
    `HERMES_API_URL=${opts.apiUrl}`,
    `HERMES_API_KEY=${opts.apiKey}`,
    `HERMES_ADAPTER_PORT=${adapterPort}`,
    `HERMES_MODEL=${opts.model || 'hermes'}`,
    `HERMES_AGENT_NAME=Hermes`,
    ''
  ].join('\n')
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value))
}

/** Derive the gateway WS URL (with token) from the HTTP base URL — mirrors
 *  connection-config's buildGatewayWsUrl so Office's UI vars point at a live
 *  WebSocket the same way the rest of the app connects. */
export function gatewayWsUrl(baseUrl: string, token: string): string {
  try {
    const parsed = new URL(baseUrl)
    const wsScheme = parsed.protocol === 'https:' ? 'wss' : 'ws'
    const prefix = parsed.pathname.replace(/\/+$/, '')

    return `${wsScheme}://${parsed.host}${prefix}/api/ws?token=${encodeURIComponent(token)}`
  } catch {
    return baseUrl
  }
}

export function buildOfficeSettings(
  existing: Record<string, unknown>,
  opts: { url: string; apiKey: string }
): Record<string, unknown> {
  const existingGateway = isRecord(existing.gateway) ? existing.gateway : {}
  const existingProfiles = isRecord(existingGateway.profiles) ? existingGateway.profiles : {}
  const hermesProfile = { url: opts.url, token: opts.apiKey }

  return {
    ...existing,
    adapter: 'hermes',
    url: opts.url,
    token: opts.apiKey,
    gateway: {
      ...existingGateway,
      url: opts.url,
      token: opts.apiKey,
      adapterType: 'hermes',
      profiles: { ...existingProfiles, hermes: hermesProfile },
      lastKnownGood: { url: opts.url, token: opts.apiKey, adapterType: 'hermes' }
    }
  }
}

export function writeOfficeFileIfChanged(filePath: string, content: string): boolean {
  try {
    if (existsSync(filePath) && readFileSync(filePath, 'utf-8') === content) {return false}
  } catch {
    /* fall through to repair */
  }

  safeWriteFile(filePath, content)

  return true
}

async function writeClaw3dSettings(profile?: string): Promise<void> {
  const conn = await resolveConnection(profile)

  if (conn.authMode === 'oauth') {return} // surfaced via status; never write a dead ticket

  try {
    mkdirSync(CLAW3D_SETTINGS_DIR, { recursive: true })
    const settingsPath = join(CLAW3D_SETTINGS_DIR, 'settings.json')
    let existing: Record<string, unknown> = {}

    try {
      existing = JSON.parse(readFileSync(settingsPath, 'utf-8'))
    } catch {
      /* fresh */
    }

    const settings = buildOfficeSettings(existing, {
      url: conn.baseUrl,
      apiKey: conn.token
    })

    writeOfficeFileIfChanged(settingsPath, JSON.stringify(settings, null, 2))
  } catch {
    /* non-fatal */
  }

  try {
    if (existsSync(HERMES_OFFICE_DIR)) {
      const envPath = join(HERMES_OFFICE_DIR, '.env')
      writeOfficeFileIfChanged(
        envPath,
        buildOfficeEnv({
          port: getClaw3dPort(),
          url: gatewayWsUrl(conn.baseUrl, conn.token),
          apiUrl: conn.baseUrl,
          apiKey: conn.token,
          model: 'hermes',
          adapterPort: DEFAULT_ADAPTER_PORT
        })
      )
    }
  } catch {
    /* non-fatal */
  }
}

function probeTcp(port: number, host = '127.0.0.1', timeoutMs = 300): Promise<boolean> {
  return new Promise(resolve => {
    const socket = createConnection({ port, host })
    socket.setTimeout(timeoutMs)
    socket.on('connect', () => {
      socket.destroy()
      resolve(true)
    })
    socket.on('error', () => {
      socket.destroy()
      resolve(false)
    })
    socket.on('timeout', () => {
      socket.destroy()
      resolve(false)
    })
  })
}

function probeHttp(url: string, timeoutMs = 1500): Promise<boolean> {
  return new Promise(resolve => {
    const req = http.request(url, { method: 'GET', timeout: timeoutMs }, res => {
      res.resume()
      resolve(true)
    })

    req.on('error', () => resolve(false))
    req.on('timeout', () => {
      req.destroy()
      resolve(false)
    })
    req.end()
  })
}

export interface Claw3dStatus {
  cloned: boolean
  installed: boolean
  devServerRunning: boolean
  adapterRunning: boolean
  running: boolean
  port: number
  portInUse: boolean
  url: string
  error: string
  oauthUnsupported: boolean
}

export interface Claw3dSetupProgress {
  step: number
  totalSteps: number
  title: string
  detail: string
  log: string
}

function isProcessRunning(pid: number): boolean {
  try {
    process.kill(pid, 0)

    return true
  } catch {
    return false
  }
}

function readPid(file: string): number | null {
  try {
    const pid = parseInt(readFileSync(file, 'utf-8').trim(), 10)

    return isNaN(pid) ? null : pid
  } catch {
    return null
  }
}

function writePid(file: string, pid: number): void {
  safeWriteFile(file, String(pid))
}

function cleanupPid(file: string): void {
  try {
    unlinkSync(file)
  } catch {
    /* ignore */
  }
}

function isDevServerRunning(): boolean {
  if (devServerProcess && !devServerProcess.killed) {return true}
  const pid = readPid(DEV_PID_FILE)

  if (pid && isProcessRunning(pid)) {return true}
  cleanupPid(DEV_PID_FILE)

  return false
}

function isAdapterRunning(): boolean {
  if (adapterProcess && !adapterProcess.killed) {return true}
  const pid = readPid(ADAPTER_PID_FILE)

  if (pid && isProcessRunning(pid)) {return true}
  cleanupPid(ADAPTER_PID_FILE)

  return false
}

function stripAnsi(input: string): string {
  // eslint-disable-next-line no-control-regex
  return input.replace(/\u001b\[[0-9;]*m/g, '')
}

function safeWriteFile(filePath: string, content: string): void {
  try {
    mkdirSync(join(filePath, '..'), { recursive: true })
    writeFileSync(filePath, content)
  } catch {
    /* non-fatal */
  }
}

function findNpm(envPath: string): ResolvedCommand {
  if (process.platform === 'win32') {
    const resolved = resolveCommandOnPath('npm', envPath)

    if (resolved) {return resolved}
  }

  const home = homedir()

  const candidates = [
    ...(process.platform === 'win32'
      ? [
          process.env.NVM_SYMLINK ? join(process.env.NVM_SYMLINK, 'npm.cmd') : undefined,
          join(home, 'AppData', 'Roaming', 'npm', 'npm.cmd'),
          process.env.ProgramFiles ? join(process.env.ProgramFiles, 'nodejs', 'npm.cmd') : undefined,
          process.env['ProgramFiles(x86)'] ? join(process.env['ProgramFiles(x86)'], 'nodejs', 'npm.cmd') : undefined
        ]
      : []),
    join(home, '.volta', 'bin', 'npm'),
    join(home, '.asdf', 'shims', 'npm'),
    join(home, '.local', 'share', 'fnm', 'aliases', 'default', 'bin', 'npm'),
    join(home, '.fnm', 'aliases', 'default', 'bin', 'npm'),
    '/usr/local/bin/npm',
    '/opt/homebrew/bin/npm'
  ].filter((candidate): candidate is string => Boolean(candidate))

  const nvmDir = process.env.NVM_DIR || join(home, '.nvm')
  const nvmVersions = join(nvmDir, 'versions', 'node')

  if (existsSync(nvmVersions)) {
    try {
      const versions = readdirSync(nvmVersions)
        .filter(d => d.startsWith('v'))
        .sort()
        .reverse()

      for (const v of versions) {candidates.unshift(join(nvmVersions, v, 'bin', 'npm'))}
    } catch {
      /* non-fatal */
    }
  }

  for (const candidate of candidates) {
    if (existsSync(candidate)) {
      return { command: candidate, windowsScript: process.platform === 'win32' && isWindowsCommandScript(candidate) }
    }
  }

  if (process.platform !== 'win32') {
    const resolved = resolveCommandOnPath('npm', envPath)

    if (resolved) {return resolved}
  }

  return resolveCommand('npm', envPath)
}

function envPath(): string {
  const hermesNodeBin = join(hermesHome(), 'node', 'bin')
  const parts = [hermesNodeBin, process.env.PATH || '']

  return parts.filter(Boolean).join(process.platform === 'win32' ? ';' : ':')
}

export async function getClaw3dStatus(profile?: string): Promise<Claw3dStatus> {
  const conn = await resolveConnection(profile)
  const cloned = existsSync(join(HERMES_OFFICE_DIR, 'package.json'))
  const installed = existsSync(join(HERMES_OFFICE_DIR, 'node_modules'))

  if (installed) {void writeClaw3dSettings(profile)}
  const port = getClaw3dPort()
  const devRunning = isDevServerRunning()
  const portInUse = devRunning ? false : await probeTcp(port)
  const adapterUp = isAdapterRunning()

  return {
    cloned,
    installed,
    devServerRunning: devRunning,
    adapterRunning: adapterUp,
    running: devRunning && adapterUp,
    port,
    portInUse,
    url: conn.baseUrl,
    error: devServerError || adapterError || '',
    oauthUnsupported: conn.authMode === 'oauth'
  }
}

export async function setupClaw3d(
  onProgress: (progress: Claw3dSetupProgress) => void,
  profile?: string
): Promise<void> {
  const totalSteps = 2
  let log = ''

  function emit(step: number, title: string, text: string): void {
    log += text
    onProgress({ step, totalSteps, title, detail: text.trim().slice(0, 120), log })
  }

  const env = { ...process.env, PATH: envPath(), HOME: homedir(), TERM: 'dumb' }
  const git = resolveCommand('git', env.PATH)

  const cloned = existsSync(join(HERMES_OFFICE_DIR, 'package.json'))

  if (!cloned) {
    emit(1, 'Cloning Hermes Office repository…', 'Cloning from GitHub…\n')
    await new Promise<void>((resolve, reject) => {
      const gitClone = buildWindowsScriptCommandLine(git.command, ['clone', HERMES_OFFICE_REPO, HERMES_OFFICE_DIR], {
        verbatim: git.windowsScript
      })

      const proc = spawn(gitClone.command, gitClone.args, {
        cwd: homedir(),
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
        windowsVerbatimArguments: gitClone.windowsVerbatimArguments
      })

      proc.stdout?.on('data', (data: Buffer) =>
        emit(1, 'Cloning Hermes Office repository…', stripAnsi(data.toString()))
      )
      proc.stderr?.on('data', (data: Buffer) =>
        emit(1, 'Cloning Hermes Office repository…', stripAnsi(data.toString()))
      )
      proc.on('close', code => {
        if (code === 0) {
          emit(1, 'Cloning Hermes Office repository…', 'Clone complete.\n')
          resolve()
        } else {
          reject(new Error(`git clone failed (exit code ${code})`))
        }
      })
      proc.on('error', err => reject(new Error(`Failed to run git: ${err.message}`)))
    })
  } else {
    emit(1, 'Hermes Office already cloned', 'Repository already exists, pulling latest…\n')
    await new Promise<void>(resolve => {
      const gitPull = buildWindowsScriptCommandLine(git.command, ['pull', '--ff-only'], { verbatim: git.windowsScript })

      const proc = spawn(gitPull.command, gitPull.args, {
        cwd: HERMES_OFFICE_DIR,
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
        windowsHide: true,
        windowsVerbatimArguments: gitPull.windowsVerbatimArguments
      })

      proc.stdout?.on('data', (data: Buffer) => emit(1, 'Updating Hermes Office…', stripAnsi(data.toString())))
      proc.stderr?.on('data', (data: Buffer) => emit(1, 'Updating Hermes Office…', stripAnsi(data.toString())))
      proc.on('close', () => resolve())
      proc.on('error', () => resolve())
    })
  }

  emit(2, 'Installing dependencies…', 'Running npm install…\n')
  const npm = createNpmCommandInvocation(findNpm(env.PATH), ['install'])
  await new Promise<void>((resolve, reject) => {
    const proc = spawn(npm.command, npm.args, {
      cwd: HERMES_OFFICE_DIR,
      env,
      stdio: ['ignore', 'pipe', 'pipe'],
      windowsHide: true,
      windowsVerbatimArguments: npm.windowsVerbatimArguments
    })

    proc.stdout?.on('data', (data: Buffer) => emit(2, 'Installing dependencies…', stripAnsi(data.toString())))
    proc.stderr?.on('data', (data: Buffer) => emit(2, 'Installing dependencies…', stripAnsi(data.toString())))
    proc.on('close', code => {
      if (code === 0) {
        emit(2, 'Installing dependencies…', 'Dependencies installed successfully.\n')
        resolve()
      } else {
        reject(new Error(`npm install failed (exit code ${code})`))
      }
    })
    proc.on('error', err => reject(new Error(`Failed to run npm: ${err.message}`)))
  })

  await writeClaw3dSettings(profile)
}

function killProcessTree(proc: ChildProcess): void {
  if (!proc.pid) {return}

  if (process.platform === 'win32') {
    try {
      execFileSync('taskkill', ['/F', '/T', '/PID', String(proc.pid)], { stdio: 'ignore' })
    } catch {
      try {
        proc.kill('SIGKILL')
      } catch {
        /* already dead */
      }
    }
  } else {
    try {
      process.kill(-proc.pid, 'SIGTERM')
    } catch {
      try {
        proc.kill('SIGTERM')
      } catch {
        /* already dead */
      }
    }

    const pid = proc.pid
    setTimeout(() => {
      try {
        process.kill(-pid, 'SIGKILL')
      } catch {
        try {
          process.kill(pid, 'SIGKILL')
        } catch {
          /* already dead */
        }
      }
    }, 3000)
  }
}

export function startDevServer(): boolean {
  if (isDevServerRunning()) {return true}

  if (!existsSync(join(HERMES_OFFICE_DIR, 'node_modules'))) {return false}

  devServerError = ''
  devServerLogs = ''
  const port = getClaw3dPort()
  const env = { ...process.env, PATH: envPath(), HOME: homedir(), TERM: 'dumb', PORT: String(port) }
  const node = resolveCommand('node', env.PATH)
  const devScript = createClaw3dScriptInvocation('dev', node.command)

  const proc = spawn(devScript.command, devScript.args, {
    cwd: HERMES_OFFICE_DIR,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: true,
    windowsHide: true,
    windowsVerbatimArguments: devScript.windowsVerbatimArguments
  })

  devServerProcess = proc

  if (proc.pid) {writePid(DEV_PID_FILE, proc.pid)}

  proc.stdout?.on('data', (data: Buffer) => {
    devServerLogs += stripAnsi(data.toString())

    if (devServerLogs.length > 2000) {devServerLogs = devServerLogs.slice(-2000)}
  })
  proc.stderr?.on('data', (data: Buffer) => {
    const text = stripAnsi(data.toString())
    devServerLogs += text

    if (devServerLogs.length > 2000) {devServerLogs = devServerLogs.slice(-2000)}

    if (/error|EADDRINUSE|ENOENT|failed|fatal/i.test(text) && !/warning/i.test(text)) {
      devServerError = text.trim().slice(0, 300)
    }
  })
  proc.on('close', code => {
    if (code && code !== 0 && !devServerError) {
      devServerError = `Dev server exited with code ${code}. Check if port ${port} is available.`
    }

    devServerProcess = null
    cleanupPid(DEV_PID_FILE)
  })

  proc.unref()

  return true
}

export function stopDevServer(): void {
  if (devServerProcess) {
    killProcessTree(devServerProcess)
    devServerProcess = null
  }

  const pid = readPid(DEV_PID_FILE)

  if (pid) {
    try {
      process.kill(-pid, 'SIGTERM')
    } catch {
      try {
        process.kill(pid, 'SIGTERM')
      } catch {
        /* already dead */
      }
    }
  }

  cleanupPid(DEV_PID_FILE)
}

export function startAdapter(profile?: string): boolean {
  if (isAdapterRunning()) {return true}

  if (!existsSync(join(HERMES_OFFICE_DIR, 'node_modules'))) {return false}

  adapterError = ''
  adapterLogs = ''
  const snapshot = lastConnectionSnapshot

  const env = {
    ...process.env,
    PATH: envPath(),
    HOME: homedir(),
    TERM: 'dumb',
    HERMES_ADAPTER_PORT: String(DEFAULT_ADAPTER_PORT),
    ...(snapshot && snapshot.authMode !== 'oauth'
      ? { HERMES_API_URL: snapshot.baseUrl, HERMES_API_KEY: snapshot.token }
      : {})
  }

  const node = resolveCommand('node', env.PATH)
  const adapterScript = createClaw3dScriptInvocation('hermes-adapter', node.command)

  const proc = spawn(adapterScript.command, adapterScript.args, {
    cwd: HERMES_OFFICE_DIR,
    env,
    stdio: ['ignore', 'pipe', 'pipe'],
    detached: true,
    windowsHide: true,
    windowsVerbatimArguments: adapterScript.windowsVerbatimArguments
  })

  adapterProcess = proc

  if (proc.pid) {writePid(ADAPTER_PID_FILE, proc.pid)}

  proc.stdout?.on('data', (data: Buffer) => {
    adapterLogs += stripAnsi(data.toString())

    if (adapterLogs.length > 2000) {adapterLogs = adapterLogs.slice(-2000)}
  })
  proc.stderr?.on('data', (data: Buffer) => {
    const text = stripAnsi(data.toString())
    adapterLogs += text

    if (adapterLogs.length > 2000) {adapterLogs = adapterLogs.slice(-2000)}

    if (/error|EADDRINUSE|ENOENT|failed|fatal/i.test(text) && !/warning/i.test(text)) {
      adapterError = text.trim().slice(0, 300)
    }
  })
  proc.on('close', code => {
    if (code && code !== 0 && !adapterError) {
      adapterError = `Hermes adapter exited with code ${code}`
    }

    adapterProcess = null
    cleanupPid(ADAPTER_PID_FILE)
  })

  proc.unref()

  return true
}

export function stopAdapter(): void {
  if (adapterProcess) {
    killProcessTree(adapterProcess)
    adapterProcess = null
  }

  const pid = readPid(ADAPTER_PID_FILE)

  if (pid) {
    try {
      process.kill(-pid, 'SIGTERM')
    } catch {
      try {
        process.kill(pid, 'SIGTERM')
      } catch {
        /* already dead */
      }
    }
  }

  cleanupPid(ADAPTER_PID_FILE)
}

let lastConnectionSnapshot: OfficeConnection | null = null

/** Keep a cheap synchronous snapshot for startAdapter's fast path. */
export function setConnectionSnapshot(conn: OfficeConnection | null): void {
  lastConnectionSnapshot = conn
}

export async function startAll(profile?: string): Promise<{ success: boolean; error?: string }> {
  if (!existsSync(join(HERMES_OFFICE_DIR, 'node_modules'))) {
    return { success: false, error: 'Hermes Office is not installed. Please install it first.' }
  }

  const conn = await resolveConnection(profile)
  setConnectionSnapshot(conn)

  if (conn.authMode === 'oauth') {
    return {
      success: false,
      error: 'Office requires a local/token gateway connection (OAuth-gated gateways are not supported yet).'
    }
  }

  await writeClaw3dSettings(profile)

  const devOk = startDevServer()

  if (!devOk) {return { success: false, error: `Failed to start dev server on port ${getClaw3dPort()}` }}

  const adapterOk = startAdapter(profile)

  if (!adapterOk) {return { success: false, error: 'Failed to start Hermes adapter' }}

  return { success: true }
}

export function stopAll(): void {
  stopDevServer()
  stopAdapter()
  devServerError = ''
  adapterError = ''
  lastConnectionSnapshot = null
}

export function getClaw3dLogs(): string {
  return [
    devServerLogs ? `=== Dev Server ===\n${devServerLogs}` : '',
    adapterLogs ? `=== Adapter ===\n${adapterLogs}` : ''
  ]
    .filter(Boolean)
    .join('\n\n')
}
