/**
 * secret-storage-policy.ts
 *
 * Single owner of the "do we use the OS keychain at all?" decision for
 * desktop-stored secrets (remote gateway tokens, CF Access headers, native
 * OAuth token sets).
 *
 * Why this exists: Electron safeStorage on macOS parks a per-app key
 * ("Hermes Key") in the login keychain. On machines with a locked, missing,
 * or corrupted default keychain, ANY safeStorage touch — including
 * isEncryptionAvailable() — makes macOS throw a blocking "Keychain Not
 * Found" / password dialog on every launch. That is an unacceptable default
 * for a chat app, so keychain-backed encryption is the safe default:
 *
 *   - Setting ON (default): secrets use strict safeStorage encryption, with
 *     loud failure when the keychain is unavailable.
 *   - Setting OFF: plaintext is an explicit, user-confirmed escape hatch;
 *     safeStorage is not touched while the setting is off.
 *
 * Legacy blobs written before the flag existed are safeStorage-encoded on
 * disk. If a user explicitly turns encryption OFF we attempt ONE migration
 * pass (decrypt → rewrite as plain). The pass is recorded in the same settings
 * file whether or not it succeeds, so a broken keychain costs at most one
 * prompt on the first post-update launch — never one per launch.
 *
 * Kept standalone (no `import 'electron'`) so it unit-tests under the
 * electron vitest project, same pattern as native-token-store.ts. main.ts
 * injects the file path and fs.
 */

export interface SecretStoragePolicy {
  /** Keychain-backed encryption enabled; defaults on, with explicit opt-out. */
  on: boolean
  /** One-shot legacy-blob migration already attempted. */
  migrated: boolean
}

export const SECRET_STORAGE_POLICY_FILE = 'secure-token-storage.json'

export interface SecretStoragePolicyIo {
  readText: () => string
  writeText: (text: string) => void
}

/**
 * Parse policy JSON without allowing JSON's last-key-wins behavior to turn a
 * duplicate member into an apparently valid policy. JSON.parse remains the
 * final syntax validator; this small lexical pass only tracks object member
 * names, including members nested in otherwise-invalid input.
 */
function parseJsonRejectingDuplicateKeys(text: string): unknown {
  let index = 0

  const skipWhitespace = () => {
    while (/\s/.test(text[index] || '')) {
      index += 1
    }
  }

  const scanString = () => {
    const start = index
    index += 1

    while (index < text.length) {
      const character = text[index]

      if (character === '\\') {
        index += 2

        continue
      }

      index += 1

      if (character === '"') {
        return text.slice(start, index)
      }
    }

    throw new Error('Unterminated JSON string')
  }

  const scanValue = (): void => {
    skipWhitespace()
    const character = text[index]

    if (character === '"') {
      scanString()

      return
    }

    if (character === '{') {
      index += 1
      skipWhitespace()
      const keys = new Set<string>()

      if (text[index] === '}') {
        index += 1

        return
      }

      while (index < text.length) {
        skipWhitespace()

        if (text[index] !== '"') {
          throw new Error('JSON object member name must be a string')
        }

        const key = JSON.parse(scanString()) as string

        if (keys.has(key)) {
          throw new Error(`Duplicate JSON object member: ${key}`)
        }

        keys.add(key)
        skipWhitespace()

        if (text[index] !== ':') {
          throw new Error('JSON object member is missing a colon')
        }

        index += 1
        scanValue()
        skipWhitespace()

        if (text[index] === '}') {
          index += 1

          return
        }

        if (text[index] !== ',') {
          throw new Error('JSON object member is missing a comma')
        }

        index += 1
      }

      throw new Error('Unterminated JSON object')
    }

    if (character === '[') {
      index += 1
      skipWhitespace()

      if (text[index] === ']') {
        index += 1

        return
      }

      while (index < text.length) {
        scanValue()
        skipWhitespace()

        if (text[index] === ']') {
          index += 1

          return
        }

        if (text[index] !== ',') {
          throw new Error('JSON array member is missing a comma')
        }

        index += 1
      }

      throw new Error('Unterminated JSON array')
    }

    const start = index

    while (index < text.length && !/[\s,}\]]/.test(text[index])) {
      index += 1
    }

    if (start === index) {
      throw new Error('Missing JSON value')
    }
  }

  scanValue()
  skipWhitespace()

  if (index !== text.length) {
    throw new Error('Trailing JSON content')
  }

  return JSON.parse(text)
}

/**
 * Normalize whatever is on disk into a policy. Anything unreadable,
 * unparseable, or hand-mangled is the secure default: encryption ON,
 * migration not yet attempted. A persisted `on: false` is honored only after
 * user has explicitly selected the plaintext escape hatch. The persisted
 * object must contain exactly the two boolean fields this module writes;
 * malformed field types, missing fields, and unknown fields all fall back to
 * the secure default rather than silently selecting plaintext.
 */
export function readSecretStoragePolicy(io: SecretStoragePolicyIo): SecretStoragePolicy {
  try {
    const parsed = parseJsonRejectingDuplicateKeys(io.readText())

    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed) && Object.keys(parsed).length === 2) {
      const candidate = parsed as { on?: unknown; migrated?: unknown }

      if (typeof candidate.on === 'boolean' && typeof candidate.migrated === 'boolean') {
        return { on: candidate.on, migrated: candidate.migrated }
      }
    }
  } catch {
    // fall through to default
  }

  return { on: true, migrated: false }
}

export function writeSecretStoragePolicy(policy: SecretStoragePolicy, io: SecretStoragePolicyIo): void {
  io.writeText(JSON.stringify({ on: policy.on === true, migrated: policy.migrated === true }))
}

/** One stored secret blob as it appears on disk. */
interface StoredSecret {
  encoding?: string
  value?: string
}

/**
 * Decide what to do with one stored blob under the current policy.
 *
 *   - 'keep'    — blob is fine as-is under this policy.
 *   - 'migrate' — safeStorage blob while encryption is OFF and migration has
 *                 not run: caller should decrypt once and rewrite as plain.
 *   - 'drop'    — safeStorage blob while encryption is OFF and the migration
 *                 pass already ran (i.e. it could not be decrypted last
 *                 time): treat as absent WITHOUT touching safeStorage, so a
 *                 dead keychain never prompts again.
 */
export function classifyStoredSecret(
  secret: StoredSecret | null | undefined,
  policy: SecretStoragePolicy
): 'keep' | 'migrate' | 'drop' {
  if (!secret || typeof secret !== 'object' || secret.encoding !== 'safeStorage') {
    return 'keep'
  }

  if (policy.on) {
    return 'keep'
  }

  return policy.migrated ? 'drop' : 'migrate'
}
