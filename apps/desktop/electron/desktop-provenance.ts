import crypto from 'node:crypto'

export const DIRECT_ACTION_PROVENANCE_VERSION = 2
export const DIRECT_ACTION_GESTURE_TTL_MS = 10 * 60_000
const DIRECT_ACTION_GESTURE_DEDUPE_MS = 500

export interface DesktopPromptPayload {
  version: 2
  event_id: string
  observed_at: string
  installation_id: string
  os_account: string
  app_identity: string
  app_instance_id: string
  window_id: string
  text_hash: string
}

function canonicalValue(value: unknown): unknown {
  if (Array.isArray(value)) {
    return value.map(canonicalValue)
  }

  if (value && typeof value === 'object') {
    return Object.fromEntries(
      Object.entries(value as Record<string, unknown>)
        .sort(([left], [right]) => (left < right ? -1 : left > right ? 1 : 0))
        .map(([key, child]) => [key, canonicalValue(child)])
    )
  }

  return value
}

export function canonicalDesktopPayload(payload: DesktopPromptPayload): Buffer {
  return Buffer.from(JSON.stringify(canonicalValue(payload)), 'utf8')
}

export function desktopTextHash(text: string): string {
  return crypto.createHash('sha256').update(text, 'utf8').digest('hex')
}

export function desktopPublicKeyFingerprint(publicKeyPem: string): string {
  const der = crypto.createPublicKey(publicKeyPem).export({ type: 'spki', format: 'der' })

  return crypto.createHash('sha256').update(der).digest('hex')
}

export function generateDesktopSigningIdentity(): { privateKeyPem: string; publicKeyPem: string } {
  const { privateKey, publicKey } = crypto.generateKeyPairSync('ed25519')

  return {
    privateKeyPem: privateKey.export({ type: 'pkcs8', format: 'pem' }).toString(),
    publicKeyPem: publicKey.export({ type: 'spki', format: 'pem' }).toString()
  }
}

export function signDesktopPayload(privateKeyPem: string, payload: DesktopPromptPayload): string {
  return crypto.sign(null, canonicalDesktopPayload(payload), privateKeyPem).toString('base64url')
}

export function verifyDesktopPayload(publicKeyPem: string, payload: DesktopPromptPayload, signature: string): boolean {
  try {
    return crypto.verify(null, canonicalDesktopPayload(payload), publicKeyPem, Buffer.from(signature, 'base64url'))
  } catch {
    return false
  }
}

export interface TrustedGestureReceipt {
  eventId: string
  expiresAt: number
  gestureToken: string
  observedAt: string
  textHash: string
  webContentsId: number
  windowId: string
}

/**
 * A native trusted gesture mints one route-independent event identity.
 * Transport and runtime-session recovery reuse the immutable receipt, while
 * changed content, another window, retirement, or expiry fail closed.
 */
export class TrustedGestureLedger {
  private readonly receipts = new Map<string, TrustedGestureReceipt>()
  private readonly currentByWebContents = new Map<number, string>()

  private prune(now: number): void {
    for (const [token, receipt] of this.receipts) {
      if (receipt.expiresAt < now) {
        this.receipts.delete(token)

        if (this.currentByWebContents.get(receipt.webContentsId) === token) {
          this.currentByWebContents.delete(receipt.webContentsId)
        }
      }
    }
  }

  begin(
    webContentsId: number,
    windowId: string,
    textHash: string,
    now = Date.now()
  ): TrustedGestureReceipt | null {
    if (!textHash || !windowId) {
      return null
    }

    this.prune(now)
    const currentToken = this.currentByWebContents.get(webContentsId)
    const current = currentToken ? this.receipts.get(currentToken) : null

    if (
      current &&
      current.windowId === windowId &&
      current.textHash === textHash &&
      Date.parse(current.observedAt) + DIRECT_ACTION_GESTURE_DEDUPE_MS >= now
    ) {
      return { ...current }
    }

    if (currentToken) {
      this.receipts.delete(currentToken)
    }

    const gestureToken = crypto.randomBytes(32).toString('base64url')

    const receipt: TrustedGestureReceipt = {
      eventId: crypto.randomUUID(),
      expiresAt: now + DIRECT_ACTION_GESTURE_TTL_MS,
      gestureToken,
      observedAt: new Date(now).toISOString(),
      textHash,
      webContentsId,
      windowId
    }

    this.receipts.set(gestureToken, receipt)
    this.currentByWebContents.set(webContentsId, gestureToken)

    return { ...receipt }
  }

  mint(
    webContentsId: number,
    gestureToken: string,
    textHash: string,
    now = Date.now()
  ): TrustedGestureReceipt | null {
    this.prune(now)
    const receipt = this.receipts.get(gestureToken)

    return receipt &&
      receipt.webContentsId === webContentsId &&
      receipt.textHash === textHash
      ? { ...receipt }
      : null
  }

  retire(
    webContentsId: number,
    gestureToken: string,
    eventId: string
  ): boolean {
    const receipt = this.receipts.get(gestureToken)

    if (
      !receipt ||
      receipt.webContentsId !== webContentsId ||
      receipt.eventId !== eventId
    ) {
      return false
    }

    this.receipts.delete(gestureToken)

    if (this.currentByWebContents.get(webContentsId) === gestureToken) {
      this.currentByWebContents.delete(webContentsId)
    }

    return true
  }
}
