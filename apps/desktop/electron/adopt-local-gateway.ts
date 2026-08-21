/**
 * Decide whether Desktop should attach to an already-running local gateway
 * instead of spawning a loopback `hermes serve` (#91564).
 *
 * A Windows scheduled-task gateway (`hermes gateway`) already loads MCP
 * servers. Spawning a second serve under Hermes.exe duplicates every MCP.
 * Adopt only when the pid file names a live process AND loopback HTTP
 * answers — otherwise Desktop still needs its own serve for /api/ws.
 */

export const DEFAULT_LOCAL_GATEWAY_BASE_URLS = [
  'http://127.0.0.1:8642',
  'http://127.0.0.1:9119'
] as const

export function parseGatewayPidRecord(raw: string): null | { pid: number } {
  const text = String(raw ?? '').trim()
  if (!text) {
    return null
  }

  try {
    const parsed = JSON.parse(text) as { pid?: unknown }
    const pid = Number(parsed?.pid)
    if (Number.isInteger(pid) && pid > 0) {
      return { pid }
    }
  } catch {
    const pid = Number(text)
    if (Number.isInteger(pid) && pid > 0) {
      return { pid }
    }
  }

  return null
}

export function gatewayPidLooksLive(pid: number, exists: (pid: number) => boolean): boolean {
  if (!Number.isInteger(pid) || pid <= 0) {
    return false
  }

  return exists(pid)
}

export function shouldAdoptExistingLocalGateway(input: {
  httpReachable: boolean
  pidLive: boolean
}): boolean {
  return Boolean(input.pidLive && input.httpReachable)
}

export async function tryAdoptExistingLocalGateway(deps: {
  baseUrls?: readonly string[]
  pidExists: (pid: number) => boolean
  probeHttp: (baseUrl: string) => Promise<boolean>
  readPidFile: () => null | string
}): Promise<null | string> {
  const record = parseGatewayPidRecord(deps.readPidFile() ?? '')
  const pidLive = Boolean(record && gatewayPidLooksLive(record.pid, deps.pidExists))

  for (const baseUrl of deps.baseUrls ?? DEFAULT_LOCAL_GATEWAY_BASE_URLS) {
    const httpReachable = await deps.probeHttp(baseUrl)
    if (shouldAdoptExistingLocalGateway({ pidLive, httpReachable })) {
      return baseUrl
    }
  }

  return null
}
