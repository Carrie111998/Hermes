import { randomBytes, timingSafeEqual } from 'node:crypto'
import fs from 'node:fs'
import path from 'node:path'

const SERVICE_MODE_ARG_PREFIX = '--hermes-service-mode-token='
const SERVICE_MODE_GRANT_TTL_MS = 60_000
const TOKEN_PATTERN = /^[a-f0-9]{64}$/

type ServiceModeClosedReason =
  'expired-grant' | 'invalid-grant' | 'invalid-request' | 'missing-grant' | 'not-packaged' | 'not-requested'

type ServiceModeDecision = { active: true; reason: null } | { active: false; reason: ServiceModeClosedReason }

interface ServiceModeGrant {
  expiresAt: number
  token: string
}

interface ServiceModeActivationInput {
  argv: readonly string[]
  grant: null | ServiceModeGrant
  isPackaged: boolean
  now: number
}

function requestedToken(argv: readonly string[]): null | string {
  const arg = argv.find(value => value.startsWith(SERVICE_MODE_ARG_PREFIX))

  return arg ? arg.slice(SERVICE_MODE_ARG_PREFIX.length) : null
}

function equalToken(left: string, right: string): boolean {
  if (!TOKEN_PATTERN.test(left) || !TOKEN_PATTERN.test(right)) {
    return false
  }

  return timingSafeEqual(Buffer.from(left, 'utf8'), Buffer.from(right, 'utf8'))
}

/**
 * Authorize the packaged renderer debugger from a short-lived, one-shot grant.
 * A command-line argument by itself is deliberately insufficient.
 */
function resolveServiceModeActivation({
  argv,
  grant,
  isPackaged,
  now
}: ServiceModeActivationInput): ServiceModeDecision {
  const token = requestedToken(argv)

  if (!token) {
    return { active: false, reason: 'not-requested' }
  }

  if (!isPackaged) {
    return { active: false, reason: 'not-packaged' }
  }

  if (!TOKEN_PATTERN.test(token)) {
    return { active: false, reason: 'invalid-request' }
  }

  if (!grant) {
    return { active: false, reason: 'missing-grant' }
  }

  if (!Number.isFinite(grant.expiresAt) || grant.expiresAt < now) {
    return { active: false, reason: 'expired-grant' }
  }

  if (!equalToken(token, grant.token)) {
    return { active: false, reason: 'invalid-grant' }
  }

  return { active: true, reason: null }
}

function buildServiceModeRelaunchArgs(argv: readonly string[], token: string): string[] {
  return [...argv.filter(value => !value.startsWith(SERVICE_MODE_ARG_PREFIX)), `${SERVICE_MODE_ARG_PREFIX}${token}`]
}

function resolveServiceModePrompt({
  checkboxChecked,
  response
}: {
  checkboxChecked: boolean
  response: number
}): 'quit' | 'retry' | 'service' | 'standard' {
  if (response === 0) {
    return 'standard'
  }

  if (response === 1) {
    return checkboxChecked ? 'service' : 'retry'
  }

  return 'quit'
}

function writeServiceModeGrant(filePath: string, now = Date.now()): ServiceModeGrant {
  const grant = {
    expiresAt: now + SERVICE_MODE_GRANT_TTL_MS,
    token: randomBytes(32).toString('hex')
  }

  const tempPath = `${filePath}.${process.pid}.${randomBytes(6).toString('hex')}.tmp`

  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 })

  try {
    fs.writeFileSync(tempPath, `${JSON.stringify(grant)}\n`, { encoding: 'utf8', flag: 'wx', mode: 0o600 })
    fs.renameSync(tempPath, filePath)
    fs.chmodSync(filePath, 0o600)
  } finally {
    fs.rmSync(tempPath, { force: true })
  }

  return grant
}

/** Read then unlink even malformed content, so no grant can be replayed. */
function consumeServiceModeGrant(filePath: string): null | ServiceModeGrant {
  let raw: null | string = null

  try {
    raw = fs.readFileSync(filePath, 'utf8')
  } catch {
    return null
  } finally {
    fs.rmSync(filePath, { force: true })
  }

  try {
    const value = JSON.parse(raw)

    if (!value || typeof value !== 'object' || typeof value.token !== 'string' || typeof value.expiresAt !== 'number') {
      return null
    }

    return { expiresAt: value.expiresAt, token: value.token }
  } catch {
    return null
  }
}

interface ServiceModeRuntimeState {
  active: true
  host: '127.0.0.1'
  pid: number
  port: number
  startedAt: number
}

function writeServiceModeState(filePath: string, state: ServiceModeRuntimeState): void {
  const tempPath = `${filePath}.${state.pid}.${randomBytes(6).toString('hex')}.tmp`

  fs.mkdirSync(path.dirname(filePath), { recursive: true, mode: 0o700 })

  try {
    fs.writeFileSync(tempPath, `${JSON.stringify(state)}\n`, { encoding: 'utf8', flag: 'wx', mode: 0o600 })
    fs.rmSync(filePath, { force: true })
    fs.renameSync(tempPath, filePath)
    fs.chmodSync(filePath, 0o600)
  } finally {
    fs.rmSync(tempPath, { force: true })
  }
}

function removeServiceModeState(filePath: string, ownerPid?: number): void {
  if (ownerPid === undefined) {
    // A packaged Standard startup owns the single-instance lock and can safely
    // clear any marker left by a crashed or force-exited prior Service Mode run.
    fs.rmSync(filePath, { force: true })

    return
  }

  try {
    const state = JSON.parse(fs.readFileSync(filePath, 'utf8'))

    if (state?.pid === ownerPid) {
      fs.rmSync(filePath, { force: true })
    }
  } catch {
    // Shutdown cleanup is deliberately PID-bound; malformed or foreign state
    // is left for the next authoritative Standard startup to clear.
  }
}

function parseDevToolsActivePort(raw: string): null | number {
  const port = Number(raw.split(/\r?\n/, 1)[0]?.trim())

  return Number.isInteger(port) && port >= 1024 && port <= 65535 ? port : null
}

async function waitForDevToolsActivePort(
  filePath: string,
  { pollMs = 25, timeoutMs = 5_000 }: { pollMs?: number; timeoutMs?: number } = {}
): Promise<null | number> {
  const deadline = Date.now() + Math.max(0, timeoutMs)

  while (Date.now() <= deadline) {
    try {
      const port = parseDevToolsActivePort(fs.readFileSync(filePath, 'utf8'))

      if (port) {
        return port
      }
    } catch {
      // Chromium has not published the file yet.
    }

    if (Date.now() >= deadline) {
      break
    }

    await new Promise(resolve => setTimeout(resolve, Math.max(1, pollMs)))
  }

  return null
}

export {
  buildServiceModeRelaunchArgs,
  consumeServiceModeGrant,
  parseDevToolsActivePort,
  removeServiceModeState,
  resolveServiceModeActivation,
  resolveServiceModePrompt,
  SERVICE_MODE_ARG_PREFIX,
  SERVICE_MODE_GRANT_TTL_MS,
  waitForDevToolsActivePort,
  writeServiceModeGrant,
  writeServiceModeState
}
export type { ServiceModeDecision, ServiceModeGrant, ServiceModeRuntimeState }
