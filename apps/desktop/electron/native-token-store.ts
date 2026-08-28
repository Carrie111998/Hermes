/**
 * native-token-store.ts
 *
 * The encrypted-at-rest persistence seam for RFC 8252 native OAuth tokens:
 * NativeTokenSet → JSON → safeStorage blob → store file, and back again on the
 * next launch.
 *
 * Kept standalone (no `import 'electron'`) so the whole restart path unit-tests
 * with the `electron` vitest project — the same pattern as native-oauth.ts.
 * main.ts owns the electron-coupled halves and injects them: the safeStorage
 * encrypt/decrypt pair and the userData store-file read/write.
 *
 * The parser direction is the load-bearing detail. What lands on disk is the
 * *normalized* camelCase NativeTokenSet, so the reload boundary is
 * parseStoredTokenSet(). Gateway `/auth/native/token` responses are snake_case
 * and stay with parseTokenResponse(); crossing the two made the decrypted set
 * throw on every launch, which surfaced as "signed out after restart" (#73271).
 */

import { type NativeTokenSet, parseStoredTokenSet } from './native-oauth'

/** One encrypted blob as written per gateway base URL. */
export interface StoredTokenSecret {
  encoding?: string
  value?: string
}

/**
 * The narrow set of side effects main.ts owns. Everything here is injected so
 * the store/load round trip can be exercised without an Electron runtime, and
 * so production keeps using safeStorage unchanged.
 */
export interface NativeTokenStoreIo {
  /**
   * Encrypt one plaintext blob. main.ts passes the strict safeStorage helper,
   * which THROWS when the OS keychain is unavailable — that must stay loud.
   * A `null` return is treated as the same authoritative failure: the caller
   * throws rather than persisting an empty entry over good tokens.
   */
  encrypt: (plaintext: string) => StoredTokenSecret | null
  /** Decrypt a stored payload; returns '' when it cannot be read. */
  decrypt: (secret: any) => string
  /** Raw store-file text. Throws when the file is absent — treated as empty. */
  readStoreText: () => string
  /** Optional predecessor retained for recovery after an interrupted publish. */
  readBackupStoreText?: () => string
  /** The caller must provide an atomic, owner-only publication primitive. */
  writeStoreText: (text: string) => void
  rememberLog?: (message: string) => void
}

/** Parse a native token store without accepting arrays or scalar JSON. */
function parseStoreText(text: string): Record<string, any> | null {
  try {
    const parsed = JSON.parse(text)
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed) ? parsed : null
  } catch {
    return null
  }
}

/**
 * Read the newest valid store. A malformed primary is recoverable from the
 * predecessor retained by the atomic publisher; an absent or unrecoverable
 * store is empty for the read path.
 */
function readStore(io: NativeTokenStoreIo): Record<string, any> {
  try {
    const parsed = parseStoreText(io.readStoreText())
    if (parsed) {
      return parsed
    }
  } catch {
    // Missing/corrupt primary: try the retained predecessor below.
  }

  try {
    const backup = io.readBackupStoreText ? parseStoreText(io.readBackupStoreText()) : null
    return backup || {}
  } catch {
    return {}
  }
}

/**
 * Read before a mutation. Unlike the load path, a corrupt store must not be
 * silently replaced by an empty object and thereby discard other gateways.
 */
function readStoreForWrite(io: NativeTokenStoreIo): Record<string, any> {
  let primary: string

  try {
    primary = io.readStoreText()
  } catch (error) {
    if ((error as any)?.code === 'ENOENT') {
      return {}
    }
    throw error
  }

  const parsed = parseStoreText(primary)
  if (parsed) {
    return parsed
  }

  try {
    const backup = io.readBackupStoreText ? parseStoreText(io.readBackupStoreText()) : null
    if (backup) {
      return backup
    }
  } catch {
    // Report the primary corruption below; no valid predecessor exists.
  }

  throw new Error('Native OAuth token store is corrupt; refusing to overwrite it.')
}

/**
 * A gateway URL safe to write into a log line.
 *
 * normalizeRemoteBaseUrl() strips query, fragment, and trailing slashes but
 * NOT userinfo, so a configured gateway can legitimately carry
 * `user:password@` all the way down to this store. Interpolating that into a
 * failure log would spill the credentials into the desktop log file, so drop
 * the userinfo and keep only what makes the line useful — scheme, host, port,
 * path. A value URL cannot parse never falls back to the raw input: echoing it
 * is the exact leak this guards against.
 */
function redactGatewayUrl(baseUrl: string): string {
  try {
    const parsed = new URL(baseUrl)

    parsed.username = ''
    parsed.password = ''

    // `host` already carries a non-default port.
    return `${parsed.protocol}//${parsed.host}${parsed.pathname}`
  } catch {
    return '<invalid gateway URL>'
  }
}

/**
 * Write (or, with `tokens === null`, drop) one gateway's token set, merging
 * into whatever other gateways are already stored.
 */
export function persistNativeTokenSet(baseUrl: string, tokens: NativeTokenSet | null, io: NativeTokenStoreIo): void {
  const store = readStoreForWrite(io)

  if (tokens) {
    // Encrypt the whole set as one blob so the refresh token never lands in
    // plaintext on disk. An unusable keychain is an authoritative write
    // failure and must surface to the caller, not be logged away.
    const secret = io.encrypt(JSON.stringify(tokens))

    if (!secret) {
      throw new Error('Secure token storage returned no encrypted payload; refusing to overwrite stored native tokens.')
    }

    store[baseUrl] = secret
  } else {
    delete store[baseUrl]
  }

  // main.ts supplies a temp-file + rename publisher. Do not catch this: the
  // in-memory cache must not claim a token was persisted when publication failed.
  io.writeStoreText(JSON.stringify(store))
}

/** Re-encode every stored gateway through one atomic store publication. */
export function rewriteNativeTokenStore(
  shouldRewrite: (secret: StoredTokenSecret) => boolean,
  reencode: (secret: StoredTokenSecret) => StoredTokenSecret,
  io: NativeTokenStoreIo
): boolean {
  const store = readStoreForWrite(io)
  const entries = Object.entries(store)
  if (!entries.some(([, value]) => shouldRewrite(value))) {
    return false
  }

  io.writeStoreText(JSON.stringify(Object.fromEntries(entries.map(([key, value]) => [key, reencode(value)]))))
  return true
}

/**
 * Reconstruct a gateway's token set from the stored encrypted payload. Returns
 * null when nothing is stored, when the blob cannot be decrypted, or when it
 * does not parse — never a partially-populated set.
 */
export function loadNativeTokenSet(baseUrl: string, io: NativeTokenStoreIo): NativeTokenSet | null {
  // The UNREDACTED url is the store key — redaction is for logs only.
  const secret = readStore(io)[baseUrl]

  if (!secret) {
    return null
  }

  try {
    const plaintext = io.decrypt(secret)

    if (!plaintext) {
      // A keychain that is merely locked/unavailable right now must not cost
      // the user their refresh token — leave the entry for the next attempt.
      io.rememberLog?.(
        `[native-oauth] failed to decrypt stored tokens for ${redactGatewayUrl(baseUrl)}; keeping stored entry for retry`
      )

      return null
    }

    // Stored blobs are normalized camelCase sets, never raw gateway responses.
    return parseStoredTokenSet(JSON.parse(plaintext))
  } catch (error) {
    const detail = error instanceof Error ? error.message : String(error)

    io.rememberLog?.(`[native-oauth] failed to load stored tokens for ${redactGatewayUrl(baseUrl)}: ${detail}`)

    return null
  }
}
