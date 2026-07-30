/**
 * Bounded boot-time WebSocket connect retries for a local (or otherwise
 * already-spawned) Hermes backend.
 *
 * A single connect attempt can lose a race against a transient event-loop
 * stall: the server accepts, the client times out and disconnects, then the
 * deferred `gateway.ready` send fails (`ready_send_failed`). Killing the
 * backend at that point recreates the stall on the next spawn (#74874).
 * Retrying the dial against the same live backend is the cheap recovery.
 */

export const BOOT_GATEWAY_CONNECT_ATTEMPTS = 3
export const BOOT_GATEWAY_CONNECT_RETRY_DELAY_MS = 2_000

export async function connectGatewayWithBootRetries(
  connect: (wsUrl: string) => Promise<void>,
  wsUrl: string,
  options: {
    attempts?: number
    delayMs?: number
    isCancelled?: () => boolean
    sleep?: (ms: number) => Promise<void>
  } = {}
): Promise<void> {
  const attempts = options.attempts ?? BOOT_GATEWAY_CONNECT_ATTEMPTS
  const delayMs = options.delayMs ?? BOOT_GATEWAY_CONNECT_RETRY_DELAY_MS
  const sleep =
    options.sleep ??
    ((ms: number) =>
      new Promise<void>(resolve => {
        setTimeout(resolve, ms)
      }))

  let lastError: unknown

  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    if (options.isCancelled?.()) {
      return
    }

    try {
      await connect(wsUrl)

      return
    } catch (error) {
      lastError = error

      if (options.isCancelled?.()) {
        return
      }

      if (attempt >= attempts) {
        break
      }

      await sleep(delayMs)
    }
  }

  throw lastError instanceof Error ? lastError : new Error(String(lastError))
}
