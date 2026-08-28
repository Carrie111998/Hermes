import {
  connectionScopeKey,
  exactDesktopConnectionMode,
  localProfileEntry,
  modeIsRemoteLike,
  normalizeRemoteBaseUrl,
  normalizeRemoteHeaders,
  normalizeSshConfig,
  normAuthMode,
  resolveAuthMode,
  savedProfileSsh
} from './connection-config'
import { applyConnectionConfigAtomically } from './connection-config-apply'
import { resolvePersistedRemoteToken } from './hardening'

const PROFILE_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/

export interface DesktopSecretCodec {
  decryptSecret: (secret: unknown) => string
  encryptSecret: (value: unknown, options?: any) => unknown
}

export interface DesktopConnectionConfig {
  mode: 'cloud' | 'local' | 'remote' | 'ssh'
  profiles: Record<string, any>
  remote: Record<string, any>
}

interface ReadDesktopConnectionConfigSnapshotOptions {
  cachedConfig: null | unknown
  cachedMtime: null | number
  readText: () => string
  statMtime: () => number
  tighten: () => void
}

export function sanitizeConnectionProfiles(raw: Record<string, any>): Record<string, any> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return {}
  }

  const out: Record<string, any> = {}

  for (const [name, entry] of Object.entries(raw)) {
    if (!entry || typeof entry !== 'object') {
      continue
    }

    if (name !== 'default' && !PROFILE_NAME_RE.test(name)) {
      continue
    }

    if (entry.mode === 'ssh') {
      const ssh = normalizeSshConfig(entry)

      if (ssh) {
        if (entry.token && typeof entry.token === 'object') {
          ssh.token = entry.token
        }

        out[name] = ssh
      }

      continue
    }

    if (entry.mode !== 'local' && !modeIsRemoteLike(entry.mode)) {
      continue
    }

    const cleaned: Record<string, any> = { mode: entry.mode }

    if (entry.mode === 'local') {
      const savedSsh = normalizeSshConfig(entry.savedSsh)

      if (savedSsh) {
        cleaned.savedSsh = savedSsh
      }

      out[name] = cleaned

      continue
    }

    const url = String(entry.url || '').trim()

    if (url) {
      cleaned.url = url
    }

    if (cleaned.mode !== 'local') {
      cleaned.authMode = normAuthMode(entry.authMode)
    }

    if (entry.token && typeof entry.token === 'object') {
      cleaned.token = entry.token
    }

    const headers = normalizeRemoteHeaders(entry.headers)

    if (Object.keys(headers).length > 0) {
      cleaned.headers = headers
    }

    if (entry.mode === 'cloud') {
      const org = String(entry.org || '').trim()

      if (org) {
        cleaned.org = org
      }
    }

    out[name] = cleaned
  }

  return out
}

export function parseDesktopConnectionConfig(rawText: string): DesktopConnectionConfig {
  const fallback: DesktopConnectionConfig = { mode: 'local', profiles: {}, remote: {} }

  try {
    const parsed = JSON.parse(rawText)

    if (!parsed || typeof parsed !== 'object') {
      return fallback
    }

    const remote = parsed.remote && typeof parsed.remote === 'object' ? { ...parsed.remote } : {}
    remote.authMode = remote.authMode === 'oauth' ? 'oauth' : 'token'

    return {
      mode: parsed.mode === 'ssh' ? 'ssh' : modeIsRemoteLike(parsed.mode) ? parsed.mode : 'local',
      remote,
      profiles: sanitizeConnectionProfiles(parsed.profiles)
    }
  } catch {
    return fallback
  }
}

/** Actual cache/read/parse boundary used by main.ts and executable tests. */
export function readDesktopConnectionConfigSnapshot({
  cachedConfig,
  cachedMtime,
  readText,
  statMtime,
  tighten
}: ReadDesktopConnectionConfigSnapshotOptions): { config: DesktopConnectionConfig; mtime: null | number } {
  let mtime: null | number = null

  try {
    mtime = statMtime()
  } catch {
    mtime = null
  }

  if (cachedConfig && cachedMtime === mtime) {
    return { config: cachedConfig as DesktopConnectionConfig, mtime }
  }

  try {
    const raw = readText()

    tighten()

    return { config: parseDesktopConnectionConfig(raw), mtime }
  } catch {
    return { config: { mode: 'local', profiles: {}, remote: {} }, mtime }
  }
}

function buildRemoteBlock(
  remoteUrl: unknown,
  authMode: string,
  token: unknown,
  decryptSecret: DesktopSecretCodec['decryptSecret'],
  org?: unknown,
  headers?: object
): Record<string, any> {
  if (authMode !== 'oauth' && !decryptSecret(token)) {
    throw new Error('Remote gateway session token is required.')
  }

  const block: Record<string, any> = {
    url: normalizeRemoteBaseUrl(remoteUrl),
    authMode,
    token
  }

  const remoteHeaders = normalizeRemoteHeaders(headers)

  if (Object.keys(remoteHeaders).length > 0) {
    block.headers = remoteHeaders
  }

  const orgValue = typeof org === 'string' ? org.trim() : ''

  if (orgValue) {
    block.org = orgValue
  }

  return block
}

function buildSshBlock(input: any, existingBlock: any = {}): Record<string, any> {
  const merged = normalizeSshConfig({
    mode: 'ssh',
    host: input.sshHost ?? existingBlock.host,
    user: input.sshUser ?? existingBlock.user,
    port: input.sshPort ?? existingBlock.port,
    keyPath: input.sshKeyPath ?? existingBlock.keyPath,
    remoteHermesPath: input.sshRemoteHermesPath ?? existingBlock.remoteHermesPath,
    remoteProfile: input.sshRemoteProfile ?? existingBlock.remoteProfile
  })

  if (!merged) {
    throw new Error('SSH host is required.')
  }

  if (existingBlock.token && existingBlock.host === merged.host) {
    merged.token = existingBlock.token
  }

  return merged
}

export function coerceDesktopConnectionConfig(
  input: any = {},
  existing: DesktopConnectionConfig,
  secrets: DesktopSecretCodec,
  options: { persistToken?: boolean } = {}
): DesktopConnectionConfig {
  const persistToken = options.persistToken !== false
  const key = connectionScopeKey(input.profile)
  const mode = exactDesktopConnectionMode(input.mode)

  if (!mode) {
    throw new Error(`Unsupported connection mode: ${String(input.mode || '')}`)
  }

  const remoteLike = modeIsRemoteLike(mode)
  const rawExistingBlock = key ? existing.profiles?.[key] || {} : existing.remote || {}
  const existingMode = key ? existing.profiles?.[key]?.mode : existing.mode
  const leavingCloud = existingMode === 'cloud' && mode !== 'cloud'
  const leavingSsh = rawExistingBlock.mode === 'ssh' && mode !== 'ssh' && mode !== 'local'
  const existingBlock = leavingCloud || leavingSsh ? {} : rawExistingBlock
  const remoteUrl = String(input.remoteUrl ?? existingBlock.url ?? '').trim()
  const authMode = resolveAuthMode(input.remoteAuthMode, existingBlock.authMode)
  const cloudOrg = mode === 'cloud' ? String(input.cloudOrg ?? existingBlock.org ?? '').trim() : ''
  const incomingToken = typeof input.remoteToken === 'string' ? input.remoteToken.trim() : ''

  const remoteHeaders =
    input.remoteHeaders && typeof input.remoteHeaders === 'object' ? input.remoteHeaders : existingBlock.headers

  const nextToken = resolvePersistedRemoteToken({
    incomingToken,
    persistToken,
    existingToken: existingBlock.token,
    allowPlainText: input.allowPlainTextToken,
    encryptSecret: secrets.encryptSecret as any
  })

  if (mode === 'ssh') {
    const sshBlock = buildSshBlock(input, savedProfileSsh(existing, key) || rawExistingBlock)

    if (key) {
      return {
        mode: existing.mode === 'ssh' || modeIsRemoteLike(existing.mode) ? existing.mode : 'local',
        remote: existing.remote || {},
        profiles: { ...(existing.profiles || {}), [key]: sshBlock }
      }
    }

    return { mode: 'ssh', remote: sshBlock, profiles: existing.profiles || {} }
  }

  if (key) {
    const profiles = { ...(existing.profiles || {}) }

    if (remoteLike) {
      profiles[key] = {
        mode,
        ...buildRemoteBlock(remoteUrl, authMode, nextToken, secrets.decryptSecret, cloudOrg, remoteHeaders)
      }
    } else {
      const localEntry = localProfileEntry(rawExistingBlock)

      if (localEntry) {
        profiles[key] = localEntry
      } else {
        delete profiles[key]
      }
    }

    return {
      mode: existing.mode === 'ssh' || modeIsRemoteLike(existing.mode) ? existing.mode : 'local',
      remote: existing.remote || {},
      profiles
    }
  }

  const nextRemote = remoteLike
    ? buildRemoteBlock(remoteUrl, authMode, nextToken, secrets.decryptSecret, cloudOrg, remoteHeaders)
    : existingMode === 'ssh'
      ? rawExistingBlock
      : { url: remoteUrl ? normalizeRemoteBaseUrl(remoteUrl) : remoteUrl, authMode, token: nextToken }

  return { mode, remote: nextRemote, profiles: existing.profiles || {} }
}

interface SaveDesktopConnectionConfigOptions {
  input: any
  readConfig: () => DesktopConnectionConfig
  secrets: DesktopSecretCodec
  writeConfig: (config: DesktopConnectionConfig) => void
}

export function saveDesktopConnectionConfig({
  input,
  readConfig,
  secrets,
  writeConfig
}: SaveDesktopConnectionConfigOptions): DesktopConnectionConfig {
  const config = coerceDesktopConnectionConfig(input, readConfig(), secrets)

  writeConfig(config)

  return config
}

interface ApplyDesktopConnectionConfigOptions<TRegistry> extends SaveDesktopConnectionConfigOptions {
  apply: (config: DesktopConnectionConfig, scope: string) => Promise<void>
  preflight?: (config: DesktopConnectionConfig) => Promise<unknown>
  readRegistry: () => TRegistry
  reconcileRegistry: (registry: TRegistry, config: DesktopConnectionConfig) => TRegistry
  writeRegistry: (registry: TRegistry) => void
}

export async function applyDesktopConnectionConfig<TRegistry>({
  apply,
  input,
  preflight,
  readConfig,
  readRegistry,
  reconcileRegistry,
  secrets,
  writeConfig,
  writeRegistry
}: ApplyDesktopConnectionConfigOptions<TRegistry>): Promise<DesktopConnectionConfig> {
  const previousConfig = readConfig()
  const previousRegistry = readRegistry()
  const config = coerceDesktopConnectionConfig(input, previousConfig, secrets)
  const key = connectionScopeKey(input?.profile)
  const scope = key || ''
  const nextRegistry = key ? previousRegistry : reconcileRegistry(previousRegistry, config)

  await applyConnectionConfigAtomically({
    previousConfig,
    previousRegistry,
    nextConfig: config,
    nextRegistry,
    preflight: preflight ? () => preflight(config) : undefined,
    writeConfig,
    writeRegistry,
    apply: () => apply(config, scope)
  })

  return config
}
