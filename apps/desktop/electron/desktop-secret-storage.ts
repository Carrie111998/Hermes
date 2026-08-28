import type { StoredTokenSecret } from './native-token-store'
import type { SecretStoragePolicy } from './secret-storage-policy'

type SafeStorage = {
  encryptString(value: string): Buffer
  decryptString(value: Buffer): string
}

type StoredSecret = StoredTokenSecret

type DesktopSecretStorageDeps = {
  getPolicy: () => SecretStoragePolicy
  safeStorage: SafeStorage
  classifyStoredSecret: (secret: unknown, policy: SecretStoragePolicy) => string
  normalizeRemoteHeaders: (headers: unknown) => Record<string, unknown>
  safeStorageEncoding: string
  encryptStrict: (value: string, safeStorage: SafeStorage, options?: object) => StoredTokenSecret | null
}

export function createDesktopSecretStorage({
  getPolicy,
  safeStorage,
  classifyStoredSecret,
  normalizeRemoteHeaders,
  safeStorageEncoding,
  encryptStrict
}: DesktopSecretStorageDeps) {
  function encryptDesktopSecret(value: unknown, options: object = {}) {
    if (!getPolicy().on) {
      const raw = String(value || '')

      return raw ? { encoding: 'plain', value: raw } : null
    }

    return encryptStrict(String(value || ''), safeStorage, options)
  }

  function decryptDesktopSecret(secret: StoredSecret | null | undefined) {
    if (!secret || typeof secret !== 'object') {
      return ''
    }

    const value = String(secret.value || '')

    if (!value) {
      return ''
    }

    if (secret.encoding === safeStorageEncoding) {
      // Legacy blob under an opted-out policy: once the one-shot migration pass
      // has run, never touch safeStorage again — a dead keychain would otherwise
      // prompt on every read. Before that pass, decryption is allowed so the
      // migration itself (and this launch's reads) can recover the value.
      if (classifyStoredSecret(secret, getPolicy()) === 'drop') {
        return ''
      }

      try {
        return safeStorage.decryptString(Buffer.from(value, 'base64'))
      } catch {
        return ''
      }
    }

    // Any other encoding (a hand-edited config, or one written by a pre-release
    // build) is returned verbatim on purpose: this fallback is what lets such a
    // config connect at all. Not a plaintext-writing path — nothing in this file
    // persists a token this way.
    return value
  }

  function decryptRemoteHeaders(headers: unknown) {
    const normalized = normalizeRemoteHeaders(headers)
    const out: Record<string, string> = {}

    for (const [name, secret] of Object.entries(normalized)) {
      const value = decryptDesktopSecret(secret as StoredSecret)

      if (value) {
        out[name] = value
      }
    }

    return out
  }

  /**
   * Turn an editor payload of remote gateway headers into stored secret
   * envelopes. The payload map is authoritative (a name missing from it is
   * cleared); per-name values are:
   *   - non-empty string  → new plaintext value, encrypted like a token
   *   - null              → keep the currently stored envelope for that name
   *                         (the editor shows a set-but-hidden secret)
   *   - envelope object   → stored verbatim (hand-edited import path)
   * Name filtering (forbidden/managed headers) happens in
   * normalizeRemoteHeaders at the registry/config layer.
   */
  function encryptIncomingRemoteHeaders(
    raw: Record<string, unknown> | null | undefined,
    existing: unknown,
    options: { allowPlainText?: boolean } = {}
  ) {
    const out: Record<string, unknown> = {}
    const stored = normalizeRemoteHeaders(existing)

    for (const [name, value] of Object.entries(raw || {})) {
      const key = String(name || '').trim()

      if (!key) {
        continue
      }

      if (typeof value === 'string') {
        const trimmed = value.trim()

        if (trimmed) {
          out[key] = encryptDesktopSecret(trimmed, { allowPlainText: options.allowPlainText === true })
        }

        continue
      }

      if (value === null) {
        if (stored[key]) {
          out[key] = stored[key]
        }

        continue
      }

      if (value && typeof value === 'object') {
        out[key] = value
      }
    }

    return out
  }

  return {
    encryptDesktopSecret,
    decryptDesktopSecret,
    decryptRemoteHeaders,
    encryptIncomingRemoteHeaders
  }
}
