import { classifyRemotePreviewTarget } from './remote-preview-classifier'

const COMMON_DEV_SERVER_PORTS = new Set([
  4173, 4174, 4200, 4321, 5000, 5173, 5174, 8000, 8080, 8081, 8888, 9000
])

export interface SshPreviewForwardDeps {
  pickLocalPort: () => Promise<number>
  forward: (localPort: number, remotePort: number) => Promise<void>
  cancelForward: (localPort: number, remotePort: number) => Promise<void>
}

export interface SshPreviewForwarder {
  rewrite: (rawTarget: string) => Promise<string | null>
  close: () => Promise<void>
}

export function isRemotePreviewForwardingRequested(value: unknown): value is true {
  return value === true
}

export function createOrReuseSshPreviewForwarder(
  existing: SshPreviewForwarder | undefined,
  deps: SshPreviewForwardDeps
): SshPreviewForwarder {
  return existing ?? createSshPreviewForwarder(deps)
}

export async function createOrReplaceSshPreviewForwarder(
  existing: SshPreviewForwarder | undefined,
  deps: SshPreviewForwardDeps,
  reuseExisting: boolean
): Promise<SshPreviewForwarder> {
  if (reuseExisting) {
    return createOrReuseSshPreviewForwarder(existing, deps)
  }

  if (existing) {
    try {
      await existing.close()
    } catch {
      // best effort; replacing the dashboard must not be blocked by stale preview cleanup
    }
  }

  return createSshPreviewForwarder(deps)
}

export async function remotePreviewTargetForForwarding(
  rawTarget: string,
  remoteForward: unknown,
  forwarder: SshPreviewForwarder | undefined
): Promise<string | null | undefined> {
  if (!isRemotePreviewForwardingRequested(remoteForward)) {
    return undefined
  }

  const classification = isLocalPreviewUrl(rawTarget)

  if (!classification) {
    return rawTarget
  }

  if (!COMMON_DEV_SERVER_PORTS.has(classification.remotePort)) {
    return null
  }

  if (!forwarder) {
    return null
  }

  return forwarder.rewrite(rawTarget)
}

function isForwardBindCollision(error: unknown) {
  const message = error instanceof Error ? error.message : String(error || '')

  return /address already in use|cannot listen to port|bind.*failed/i.test(message)
}

function defaultRemotePort(protocol: string) {
  return protocol === 'https:' ? 443 : 80
}

export function isLocalPreviewUrl(rawTarget: string): { remotePort: number } | null {
  const classification = classifyRemotePreviewTarget(rawTarget)

  if (!classification?.isHttp || !classification.isLocal || classification.remotePort === null) {
    return null
  }

  return { remotePort: classification.remotePort }
}

export function createSshPreviewForwarder(
  deps: SshPreviewForwardDeps,
  { attempts = 3 }: { attempts?: number } = {}
): SshPreviewForwarder {
  const forwards = new Map<number, { localPort: number; remotePort: number }>()
  const pending = new Map<number, Promise<number>>()
  let closed = false

  async function openForward(remotePort: number) {
    let lastError: unknown

    for (let attempt = 0; attempt < Math.max(1, attempts); attempt += 1) {
      const localPort = await deps.pickLocalPort()

      try {
        await deps.forward(localPort, remotePort)
        forwards.set(remotePort, { localPort, remotePort })

        return localPort
      } catch (error) {
        lastError = error

        // A forward can fail after the SSH side has accepted the request. Always
        // make the candidate disposable before retrying or surfacing the error.
        try {
          await deps.cancelForward(localPort, remotePort)
        } catch {
          // best effort; the owning SSH connection teardown remains authoritative
        }

        if (!isForwardBindCollision(error) || attempt === Math.max(1, attempts) - 1) {
          throw error
        }
      }
    }

    throw lastError
  }

  async function ensureForward(remotePort: number) {
    if (closed) {
      throw new Error('SSH preview forwarding has been closed.')
    }

    const existing = forwards.get(remotePort)

    if (existing) {
      return existing.localPort
    }

    const active = pending.get(remotePort)

    if (active) {
      return active
    }

    const operation = openForward(remotePort)
    pending.set(remotePort, operation)

    try {
      return await operation
    } finally {
      if (pending.get(remotePort) === operation) {
        pending.delete(remotePort)
      }
    }
  }

  return {
    async rewrite(rawTarget) {
      const classification = isLocalPreviewUrl(rawTarget)

      if (!classification || !COMMON_DEV_SERVER_PORTS.has(classification.remotePort)) {
        return null
      }

      const localPort = await ensureForward(classification.remotePort)
      const url = new URL(String(rawTarget || '').trim())
      url.hostname = '127.0.0.1'
      url.port = String(localPort)

      return url.toString()
    },

    async close() {
      if (closed) {
        return
      }

      closed = true
      await Promise.allSettled([...pending.values()])
      const activeForwards = [...forwards.values()]
      forwards.clear()

      await Promise.all(
        activeForwards.map(async ({ localPort, remotePort }) => {
          try {
            await deps.cancelForward(localPort, remotePort)
          } catch {
            // best effort; SSH close drops any remaining forwards
          }
        })
      )
    }
  }
}
