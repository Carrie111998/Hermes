export type WindowCloseFlush = () => boolean | Promise<boolean>

export interface WindowCloseBridge {
  onRequest: (listener: (requestId: string) => void) => () => void
  resolve: (requestId: string, allowed: boolean) => void
}

interface Registration {
  flush: WindowCloseFlush
  inFlight: Promise<boolean> | undefined
  unregisterWhenSettled: boolean
}

/**
 * Per-renderer registry for close-sensitive work. The native-process side asks
 * this registry to drain before it permits a BrowserWindow or app to close.
 */
export function createWindowCloseBarrier(bridge: WindowCloseBridge) {
  const registrations = new Set<Registration>()
  let stopListening: (() => void) | undefined

  const requestFlush = async (requestId: string): Promise<void> => {
    const current = [...registrations]
    const results = await Promise.all(
      current.map(async registration => {
        let flush: Promise<boolean>

        try {
          flush = Promise.resolve(registration.flush())
        } catch {
          flush = Promise.resolve(false)
        }

        registration.inFlight = flush

        try {
          return await flush
        } catch {
          return false
        } finally {
          registration.inFlight = undefined

          if (registration.unregisterWhenSettled) {
            registrations.delete(registration)
          }
        }
      })
    )

    bridge.resolve(requestId, results.every(Boolean))
  }

  return {
    install(): () => void {
      if (!stopListening) {
        stopListening = bridge.onRequest(requestId => {
          void requestFlush(requestId)
        })
      }

      return () => {
        stopListening?.()
        stopListening = undefined
      }
    },
    register(flush: WindowCloseFlush): () => void {
      const registration: Registration = { flush, inFlight: undefined, unregisterWhenSettled: false }
      registrations.add(registration)

      return () => {
        registration.unregisterWhenSettled = true

        if (!registration.inFlight) {
          registrations.delete(registration)
        }
      }
    }
  }
}

const bridge = window.hermesDesktop?.closeBarrier
const rendererCloseBarrier = bridge ? createWindowCloseBarrier(bridge) : undefined

/** Install once at full-renderer boot, before panes can register controllers. */
export function installWindowCloseBarrier(): void {
  rendererCloseBarrier?.install()
}

/** Register live work that must settle before this renderer's window can close. */
export function registerWindowCloseBarrier(flush: WindowCloseFlush): () => void {
  return rendererCloseBarrier?.register(flush) ?? (() => undefined)
}
