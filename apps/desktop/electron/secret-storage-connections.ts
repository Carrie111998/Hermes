import type { SecretStoragePolicy } from './secret-storage-policy'

export function createSecretStorageConnections(deps: Record<string, any>): Record<string, any> {
  let {
    DESKTOP_CONNECTIONS_REGISTRY_PATH,
    DESKTOP_CONNECTION_CONFIG_PATH,
    DESKTOP_PROFILE_CONFIG_PATH,
    HERMES_HOME,
    PROFILE_NAME_RE,
    SAFE_STORAGE_ENCODING,
    SECRET_STORAGE_POLICY_FILE,
    app,
    applyRemoteRequestHeaders,
    broadcastConnectionsChanged,
    buildGatewayWsUrl,
    buildGatewayWsUrlWithTicket,
    classifyStoredSecret,
    connectionConfigCache,
    connectionConfigCacheMtime,
    connectionDialFieldsChanged,
    connectionInstallIds,
    connectionRegistryCache,
    connectionRegistryCacheMtime,
    connectionScopeKey,
    createDesktopSecretStorage,
    dialog,
    encryptDesktopSecretStrict,
    fs,
    gatewayAuthProviders,
    gatewayTicketFailure,
    helper,
    hostLabelFromBaseUrl,
    localProfileEntry,
    makeNousCloudBackendDownError,
    makeUnsignedOauthError,
    mergeConnectionInput,
    migrateV1ToRegistry,
    modeIsRemoteLike,
    normAuthMode,
    normalizeConnectionInput,
    normalizeRegistry,
    normalizeRemoteBaseUrl,
    normalizeRemoteHeaders,
    normalizeSshConfig,
    oauthGuardMayHardFail,
    oauthSessionIsLive,
    oauthTicketFailureAuthMessage,
    path,
    profileRemoteOverride,
    profileSshOverride,
    readSecretStoragePolicy,
    reconcileRegistryDrift,
    rememberLog,
    remoteHeaderRulesInstalled,
    remoteRequestMatchesBaseUrl,
    remoteWsHeaderStore,
    resolveAuthMode,
    resolvePersistedRemoteToken,
    resolveRemoteBackend,
    rewriteNativeTokenStore,
    safeStorage,
    savedProfileSsh,
    session,
    spawn,
    stopRegistryConnectionBackends,
    tightenSecretFileMode,
    tokenPreview,
    upsertConnection,
    writeFileAtomic,
    writeSecretFileAtomic,
    writeSecretStoragePolicy,
    hasOauthSessionCookie,
    hasLiveOauthSession,
    openOauthLoginWindow,
    _nativeTokenStoreIo,
    hasNativeSession,
    mintGatewayWsTicket,
    hasLivePortalSession,
    hasPortalAccessToken,
    renewPortalAccessSilently,
    managedConnectionUpdateGate,
    persistSshConnectionToken
  } = deps

  // Silent per-agent sign-in: open the selected agent dashboard's /login in the
  // SAME OAuth partition. Because the user already holds a live portal session
  // there, the agent's /oauth/authorize auto-approves (org member) and 302s back,
  // setting that agent's gateway session cookie WITHOUT a second interactive
  // prompt. Reuses openOauthLoginWindow — the window self-closes the instant the
  // agent's session cookie lands (a silent flow finishes in well under a second;
  // if the portal session were absent it would fall through to an interactive
  // login, which the discovery gate already prevents). Returns once the agent's
  // gateway session cookie is present.
  async function cloudAgentSilentSignIn(dashboardUrl) {
    const baseUrl = normalizeRemoteBaseUrl(dashboardUrl)

    // Pre-req: a live portal session must exist, or this would surface an
    // interactive prompt rather than a silent cascade. Discovery already gates on
    // this, but a selection can arrive after the session lapsed.
    if (!(await hasLivePortalSession())) {
      const err = new Error('Your Hermes Cloud session has expired. Sign in to Hermes Cloud again.') as any
      err.needsCloudLogin = true
      throw err
    }

    // The cascade rides the portal's auto-approve, which needs the short-lived
    // access state just like discovery. If only renewal material survived the
    // restart, mint a fresh access token first so the hidden cascade window
    // auto-SSOs instead of stalling on an interactive chooser (#73495).
    if (!(await hasPortalAccessToken())) {
      await renewPortalAccessSilently()
    }

    await openOauthLoginWindow(baseUrl, { silent: true })

    return { baseUrl, connected: await hasOauthSessionCookie(baseUrl) }
  }

  // ---------------------------------------------------------------------------
  // Keychain-backed secret storage (secret-storage-policy.ts owns the decision).
  // Default ON: new persisted secrets use safeStorage. Settings → Gateway
  // exposes an explicit plaintext escape hatch; flipping it re-encodes stored
  // secrets in place.
  // ---------------------------------------------------------------------------
  const SECRET_STORAGE_POLICY_PATH = path.join(app.getPath('userData'), SECRET_STORAGE_POLICY_FILE)

  const _secretStoragePolicyIo = {
    readText: () => fs.readFileSync(SECRET_STORAGE_POLICY_PATH, 'utf8'),
    writeText: (text: string) => writeSecretFileAtomic(SECRET_STORAGE_POLICY_PATH, text, { encoding: 'utf8' })
  }

  let _secretStoragePolicy: SecretStoragePolicy | null = null

  function secretStoragePolicy(): SecretStoragePolicy {
    if (!_secretStoragePolicy) {
      _secretStoragePolicy = readSecretStoragePolicy(_secretStoragePolicyIo)
    }

    return _secretStoragePolicy
  }

  function setSecretStoragePolicy(next: SecretStoragePolicy) {
    const normalized = { on: next.on === true, migrated: next.migrated === true }
    // Publish the policy before changing the process cache. If the atomic write
    // fails, this process must continue using the previously committed policy.
    writeSecretStoragePolicy(normalized, _secretStoragePolicyIo)
    _secretStoragePolicy = normalized
  }

  const { encryptDesktopSecret, decryptDesktopSecret, decryptRemoteHeaders, encryptIncomingRemoteHeaders } =
    createDesktopSecretStorage({
      getPolicy: secretStoragePolicy,
      safeStorage,
      classifyStoredSecret,
      normalizeRemoteHeaders,
      safeStorageEncoding: SAFE_STORAGE_ENCODING,
      encryptStrict: encryptDesktopSecretStrict
    })

  /**
   * Keychain availability as the renderer should see it. With encryption
   * opted out this must NOT probe safeStorage — isEncryptionAvailable() is
   * itself a keychain touch that raises the macOS dialog this feature exists
   * to avoid. We report `true` so no plain-text warning banners fire: storing
   * plaintext is the user's explicit choice, not the default mode.
   */
  function probeSecureTokenStorage(): boolean {
    if (!secretStoragePolicy().on) {
      return true
    }

    try {
      return Boolean(safeStorage.isEncryptionAvailable())
    } catch {
      return false
    }
  }

  /**
   * Rewrite every stored desktop secret (v1 connection.json token/headers +
   * per-profile overrides, v2 registry connections, native OAuth token store)
   * through `reencode`. Returns true when any store was rewritten. Shared by
   * the one-shot legacy migration and the Settings encryption toggle.
   */
  function rewriteAllStoredSecrets(shouldRewrite: (secret: any) => boolean, reencode: (secret: any) => any): boolean {
    let touched = false

    // Older connection.json files stored tokens as bare strings, and global
    // remote headers could likewise be bare strings. Normalize those legacy
    // shapes before the caller's policy predicate sees them; otherwise both the
    // migration pass and the encryption toggle mistake plaintext for an unknown
    // encoding and leave it on disk.
    const normalizeStoredSecret = (secret: any) => {
      if (typeof secret !== 'string') {
        return secret
      }

      const value = secret.trim()

      return value ? { encoding: 'plain', value } : secret
    }

    const needsRewrite = (secret: any) => shouldRewrite(normalizeStoredSecret(secret))
    const rewriteSecret = (secret: any) => reencode(normalizeStoredSecret(secret))

    const normalizeStoredBlock = (block: any) => {
      if (!block || typeof block !== 'object') {
        return block
      }

      const next = { ...block }

      if (block.token) {
        next.token = normalizeStoredSecret(block.token)
      }

      if (block.headers && typeof block.headers === 'object') {
        next.headers = normalizeRemoteHeaders(block.headers)
      }

      return next
    }

    const rewriteBlock = (block: any) => {
      const normalized = normalizeStoredBlock(block)

      if (!normalized || typeof normalized !== 'object') {
        return normalized
      }

      const next = { ...normalized, ...(normalized.token ? { token: rewriteSecret(normalized.token) } : {}) }

      if (normalized.headers && typeof normalized.headers === 'object') {
        next.headers = Object.fromEntries(
          Object.entries(normalized.headers).map(([k, v]) => [k, rewriteSecret(v)])
        )
      }

      return next
    }

    const blockNeedsRewrite = (o: any) => {
      const normalized = normalizeStoredBlock(o)

      return (
        needsRewrite(normalized?.token) ||
        Object.values(normalized?.headers && typeof normalized.headers === 'object' ? normalized.headers : {}).some(
          needsRewrite
        )
      )
    }

    // v1 connection.json.
    const config = readDesktopConnectionConfig({ failOnCorrupt: true })

    if (blockNeedsRewrite(config.remote) || Object.values(config.profiles || {}).some(blockNeedsRewrite)) {
      touched = true
      writeDesktopConnectionConfig({
        ...config,
        remote: rewriteBlock(config.remote),
        profiles: Object.fromEntries(Object.entries(config.profiles || {}).map(([k, v]) => [k, rewriteBlock(v)]))
      })
    }

    // v2 connections.json registry.
    const registry = readDesktopConnectionsRegistry()

    if (registry.connections?.some(blockNeedsRewrite)) {
      touched = true
      writeDesktopConnectionsRegistry({ ...registry, connections: registry.connections.map(rewriteBlock) })
    }

    // Native OAuth token store: baseUrl → blob. The store module rejects a
    // corrupt primary before mutation and publishes the complete replacement via
    // the atomic owner-only writer supplied above.
    if (rewriteNativeTokenStore(needsRewrite, rewriteSecret, _nativeTokenStoreIo())) {
      touched = true
    }

    return touched
  }

  const SECRET_STORAGE_TRANSITION_PATH = path.join(app.getPath('userData'), 'secret-storage-transition.json')

  type SecretStorageTransition = { targetOn: boolean; targetMigrated: boolean }

  function writeSecretStorageTransition(transition: SecretStorageTransition) {
    writeSecretFileAtomic(SECRET_STORAGE_TRANSITION_PATH, JSON.stringify(transition), { encoding: 'utf8' })
  }

  function readSecretStorageTransition(): SecretStorageTransition | null {
    try {
      const parsed = JSON.parse(fs.readFileSync(SECRET_STORAGE_TRANSITION_PATH, 'utf8'))
      if (typeof parsed?.targetOn !== 'boolean' || typeof parsed?.targetMigrated !== 'boolean') {
        throw new Error('invalid transition state')
      }
      return parsed
    } catch (error) {
      if ((error as any)?.code === 'ENOENT') {
        return null
      }
      throw new Error(
        `Secret storage transition marker is corrupt: ${error instanceof Error ? error.message : String(error)}`
      )
    }
  }

  function clearSecretStorageTransition() {
    try {
      fs.rmSync(SECRET_STORAGE_TRANSITION_PATH, { force: true })
    } catch (error) {
      throw new Error(
        `Secret storage transition completed but its recovery marker could not be removed: ${error instanceof Error ? error.message : String(error)}`
      )
    }
  }

  function decryptSecretForMigration(secret: any): string {
    if (secret?.encoding !== SAFE_STORAGE_ENCODING || !secret.value) {
      throw new Error('SafeStorage migration encountered an invalid encrypted secret.')
    }

    try {
      const plaintext = safeStorage.decryptString(Buffer.from(String(secret.value), 'base64'))
      if (!plaintext) {
        throw new Error('SafeStorage returned no plaintext during migration.')
      }

      return plaintext
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      throw new Error(`SafeStorage decryption failed; migration remains pending: ${detail}`)
    }
  }

  /**
   * Run the multi-store rewrite behind a durable marker. The marker is written
   * before the first destination and removed only after every destination and
   * the target policy have been atomically published. A restart can therefore
   * resume an interrupted transition from the marker without trusting memory.
   */
  function runSecretStorageTransition(
    targetOn: boolean,
    targetMigrated: boolean,
    shouldRewrite: (secret: any) => boolean,
    reencode: (secret: any) => any
  ) {
    writeSecretStorageTransition({ targetOn, targetMigrated })
    rewriteAllStoredSecrets(shouldRewrite, reencode)
    setSecretStoragePolicy({ on: targetOn, migrated: targetMigrated })
    clearSecretStorageTransition()
  }

  function recoverSecretStorageTransition() {
    const transition = readSecretStorageTransition()
    if (!transition) {
      return
    }

    const shouldRewrite = transition.targetOn
      ? (secret: any) => secret?.encoding === 'plain' && Boolean(secret.value)
      : (secret: any) => secret?.encoding === SAFE_STORAGE_ENCODING
    const reencode = transition.targetOn
      ? (secret: any) => (shouldRewrite(secret) ? encryptDesktopSecretStrict(String(secret.value), safeStorage) : secret)
      : (secret: any) => {
          if (!shouldRewrite(secret)) {
            return secret
          }
          const plaintext = decryptSecretForMigration(secret)
          return plaintext ? { encoding: 'plain', value: plaintext } : secret
        }

    runSecretStorageTransition(transition.targetOn, transition.targetMigrated, shouldRewrite, reencode)
  }

  /**
   * One-shot legacy migration. The default policy is secure and migrates legacy
   * plaintext blobs when possible; an explicit `{ on: false, migrated: true }`
   * remains a durable opt-out and is never silently overridden. Both directions
   * use the same recovery marker, and `migrated` is not committed after failure.
   */
  function migrateLegacyEncryptedSecretsOnce() {
    try {
      recoverSecretStorageTransition()
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      rememberLog(`[secret-storage] recovery transition failed; migration remains pending: ${detail}`)
      return
    }

    const policy = secretStoragePolicy()

    if (policy.migrated) {
      return
    }

    const targetOn = policy.on
    const shouldRewrite = targetOn
      ? (secret: any) => secret?.encoding === 'plain' && Boolean(secret.value)
      : (secret: any) => classifyStoredSecret(secret, policy) === 'migrate'
    const reencode = targetOn
      ? (secret: any) => (shouldRewrite(secret) ? encryptDesktopSecretStrict(String(secret.value), safeStorage) : secret)
      : (secret: any) => {
          if (!shouldRewrite(secret)) {
            return secret
          }
          const plaintext = decryptSecretForMigration(secret)
          return plaintext ? { encoding: 'plain', value: plaintext } : secret
        }

    try {
      runSecretStorageTransition(targetOn, true, shouldRewrite, reencode)
    } catch (error) {
      const detail = error instanceof Error ? error.message : String(error)
      rememberLog(`[secret-storage] legacy migration pass failed; migration remains pending: ${detail}`)
      return
    }
  }

  /**
   * Settings → Gateway toggle: flip keychain-backed encryption and re-encode
   * every stored secret to match. The marker is durable before the first rewrite,
   * and policy publication is the final step, so a crash cannot commit a secure
   * policy over a partially migrated set.
   */
  function applySecretStorageEncryption(on: boolean) {
    const enable = on === true

    if (secretStoragePolicy().on === enable) {
      return { on: enable }
    }

    if (enable) {
      const needsEncrypt = (secret: any) => secret?.encoding === 'plain' && Boolean(secret.value)

      if (
        !(() => {
          try {
            return Boolean(safeStorage.isEncryptionAvailable())
          } catch {
            return false
          }
        })()
      ) {
        throw new Error(
          'OS keychain encryption is unavailable on this machine, so stored gateway secrets cannot be encrypted.'
        )
      }

      runSecretStorageTransition(true, true, needsEncrypt, secret =>
        needsEncrypt(secret) ? encryptDesktopSecretStrict(String(secret.value), safeStorage) : secret
      )
      return { on: true }
    }

    const needsDecrypt = (secret: any) => secret?.encoding === SAFE_STORAGE_ENCODING

    runSecretStorageTransition(false, true, needsDecrypt, secret => {
      if (!needsDecrypt(secret)) {
        return secret
      }

      const plaintext = decryptSecretForMigration(secret)
      return plaintext ? { encoding: 'plain', value: plaintext } : secret
    })

    return { on: false }
  }

  function rememberRemoteWsHeaders(wsUrl, headers = {}) {
    remoteWsHeaderStore.remember(wsUrl, headers)
  }

  function headersForRemoteRequest(requestUrl) {
    const exactWsHeaders = remoteWsHeaderStore.headersFor(requestUrl)

    if (exactWsHeaders && Object.keys(exactWsHeaders).length > 0) {
      return exactWsHeaders
    }

    const config = readDesktopConnectionConfig()

    if (modeIsRemoteLike(config.mode) && config.remote?.url) {
      const headers = decryptRemoteHeaders(config.remote.headers)

      if (Object.keys(headers).length > 0 && remoteRequestMatchesBaseUrl(requestUrl, config.remote.url)) {
        return headers
      }
    }

    return {}
  }

  function installRemoteHeaderRules() {
    if (remoteHeaderRulesInstalled) {
      return
    }

    remoteHeaderRulesInstalled = true
    session.defaultSession.webRequest.onBeforeSendHeaders((details, callback) => {
      applyRemoteRequestHeaders(details, callback, headersForRemoteRequest)
    })
  }

  // Validate + normalize the per-profile remote overrides map read from disk.
  // Drops malformed names/entries and keeps only the recognized fields so a
  // hand-edited or stale connection.json can't inject junk into resolution.
  function sanitizeConnectionProfiles(raw: Record<string, any>) {
    if (!raw || typeof raw !== 'object') {
      return {}
    }

    const out = {}

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

      const cleaned: {
        mode: 'remote' | 'local' | 'cloud'
        url?: string
        authMode?: string
        token?: object
        headers?: object
        org?: string
        savedSsh?: object
      } = {
        mode: modeIsRemoteLike(entry.mode) ? entry.mode : 'local'
      }

      if (cleaned.mode === 'local') {
        const savedSsh = normalizeSshConfig(entry.savedSsh)

        if (savedSsh) {
          cleaned.savedSsh = savedSsh
        }
      }

      const url = String(entry.url || '').trim()

      if (url) {
        cleaned.url = url
      }

      cleaned.authMode = normAuthMode(entry.authMode)

      if ((entry as any).token && typeof entry.token === 'object') {
        cleaned.token = entry.token
      }

      const headers = normalizeRemoteHeaders((entry as any).headers)

      if (Object.keys(headers).length > 0) {
        cleaned.headers = headers
      }

      // Preserve the Hermes Cloud org tag on cloud-mode entries so Settings can
      // reopen into the same org for a per-profile cloud connection.
      if (cleaned.mode === 'cloud') {
        const org = String(entry.org || '').trim()

        if (org) {
          cleaned.org = org
        }
      }

      out[name] = cleaned
    }

    return out
  }

  function readDesktopConnectionConfig(options: { failOnCorrupt?: boolean } = {}) {
    // Check if file changed on disk since last read (e.g. modified by another
    // process or an external tool).  Our own writes update the cache inline
    // via writeDesktopConnectionConfig, but external changes would be missed.
    let mtime = null

    try {
      mtime = fs.statSync(DESKTOP_CONNECTION_CONFIG_PATH).mtimeMs
    } catch {
      mtime = null
    }

    if (connectionConfigCache && connectionConfigCacheMtime === mtime) {
      return connectionConfigCache
    }

    let config = { mode: 'local', remote: {}, profiles: {} }
    let raw: string | null = null

    try {
      raw = fs.readFileSync(DESKTOP_CONNECTION_CONFIG_PATH, 'utf8')
      // Tighten an install written before this file was owner-only. Every write
      // now goes out at 0600, but a file already on disk keeps its old 0644 bits
      // until something chmods it, and waiting for the user's next Settings save
      // would leave it group/other-readable indefinitely. Runs on a cache miss
      // only (once per launch, plus after an external edit); chmod moves ctime,
      // not mtime, so it cannot invalidate the cache it sits inside.
      //
      // Deliberately BEFORE JSON.parse, not after: a truncated or hand-mangled
      // connection.json still contains the token bytes, and parse throws into the
      // catch below, which swallows the error and falls back to local mode. With
      // the tighten after the parse, exactly the file that is both corrupt AND
      // world-readable would be the one file never tightened — and nothing would
      // ever retry it, because the fallback config is not written back. The chmod
      // needs only the path, so it has no reason to wait for valid JSON.
      tightenSecretFileMode(DESKTOP_CONNECTION_CONFIG_PATH)

      const parsed = JSON.parse(raw)

      // Legacy plaintext is migrated before the first window by
      // migrateLegacyEncryptedSecretsOnce(). This read path remains deliberately
      // side-effect free: it only parses and tightens the existing file, while
      // the durable transition marker owns all re-encoding writes.
      if (parsed && typeof parsed === 'object') {
        const remote = parsed.remote && typeof parsed.remote === 'object' ? parsed.remote : {}
        // authMode lives on the remote sub-object: 'oauth' (cookie + ws-ticket)
        // or 'token' (legacy static session token). Default to 'token' for
        // backward compatibility with configs written before OAuth support.
        remote.authMode = remote.authMode === 'oauth' ? 'oauth' : 'token'
        config = {
          mode: parsed.mode === 'ssh' ? 'ssh' : modeIsRemoteLike(parsed.mode) ? parsed.mode : 'local',
          remote,
          // Per-profile remote overrides: each profile may point at its own
          // backend (local spawn or its own remote URL). Preserved verbatim so
          // profileRemoteOverride() can resolve them; normalized lazily on save.
          profiles: sanitizeConnectionProfiles(parsed.profiles)
        }
      }
    } catch (error) {
      if (options.failOnCorrupt && (raw !== null || (error as { code?: string })?.code !== 'ENOENT')) {
        const detail = error instanceof Error ? error.message : String(error)
        rememberLog(
          `[secret-storage] corrupt connection.json was preserved at ${DESKTOP_CONNECTION_CONFIG_PATH}; migration remains pending: ${detail}`
        )
        throw new Error(`Corrupt connection.json; preserved for recovery and refusing migration completion: ${detail}`)
      }

      // Missing or malformed connection settings should fall back to local.
    }

    connectionConfigCache = config
    connectionConfigCacheMtime = mtime

    return config
  }

  function writeDesktopConnectionConfig(config) {
    fs.mkdirSync(path.dirname(DESKTOP_CONNECTION_CONFIG_PATH), { recursive: true })
    // Owner-only, not writeFileAtomic: this is the single choke point for every
    // connection.json write (the IPC save/apply handlers and
    // persistSshConnectionToken all land here), and the file carries the
    // safeStorage-encrypted gateway token plus its URL and SSH host/user/keyPath.
    // safeStorage keeps the token opaque; 0600 keeps the whole record — and the
    // fields that are NOT encrypted — off other local accounts, matching
    // native-oauth-tokens.json and desktop-installation.json.
    writeSecretFileAtomic(DESKTOP_CONNECTION_CONFIG_PATH, JSON.stringify(config, null, 2))
    connectionConfigCache = config
    connectionConfigCacheMtime = fs.statSync(DESKTOP_CONNECTION_CONFIG_PATH).mtimeMs
  }

  // ── v2 connection registry (multi-source) ──────────────────────────────────

  /**
   * Read the v2 registry, importing from v1 connection.json exactly once (when
   * connections.json does not exist yet). Same mtime-cache + tighten-mode
   * discipline as readDesktopConnectionConfig; a corrupt registry degrades to
   * local-only via normalizeRegistry rather than throwing at boot.
   *
   * An EXISTING registry is additionally reconciled against v1 when the two have
   * drifted — see reconcileRegistryDrift. The one-shot migration cannot cover a
   * user who registered nothing and then pointed Settings -> Gateway at a remote,
   * and until that heals, every launch re-homes them onto a local backend.
   */
  function readDesktopConnectionsRegistry() {
    let mtime = null

    try {
      mtime = fs.statSync(DESKTOP_CONNECTIONS_REGISTRY_PATH).mtimeMs
    } catch {
      mtime = null
    }

    if (connectionRegistryCache && connectionRegistryCacheMtime === mtime) {
      return connectionRegistryCache
    }

    let registry

    if (mtime === null) {
      // First run on this build: import the v1 single-connection config. The v1
      // file is NOT modified or deleted — older builds keep reading it. The
      // migration is deterministic over the v1 input, so even if two processes
      // race the first run (updater relaunch, second window), both derive the
      // same registry and the later atomic write is a no-op content-wise.
      registry = migrateV1ToRegistry(readDesktopConnectionConfig())

      try {
        writeDesktopConnectionsRegistry(registry)
      } catch {
        // Write failed (full disk, read-only userData). Keep the migrated
        // registry in memory so list/save keep working this session instead of
        // hard-failing every hermes:connections:* call.
        connectionRegistryCache = registry
        connectionRegistryCacheMtime = null
      }

      return connectionRegistryCache
    }

    try {
      // Same rationale as connection.json: tighten BEFORE parse so a corrupt
      // file that still holds token bytes gets its mode fixed anyway.
      tightenSecretFileMode(DESKTOP_CONNECTIONS_REGISTRY_PATH)
      registry = normalizeRegistry(JSON.parse(fs.readFileSync(DESKTOP_CONNECTIONS_REGISTRY_PATH, 'utf8')))
    } catch {
      // Whole-file corruption (truncated write, mangled hand-edit). The
      // degraded local-only registry keeps boot working, but the file BYTES are
      // the user's connection data — preserve them in a sidecar BEFORE any
      // later write (drift reconcile, connection save) overwrites the file
      // (#94246: recovery must never be data loss).
      preserveCorruptRegistrySidecar()
      registry = normalizeRegistry(null)
    }

    if (registry?.quarantined?.length) {
      rememberLog(
        `[connections] ${registry.quarantined.length} malformed registry entr${registry.quarantined.length === 1 ? 'y was' : 'ies were'} quarantined (kept under "quarantined" in connections.json); healthy connections loaded normally.`
      )
    }

    // Heal v1 -> v2 drift: the v1 global route names a remote this registry has
    // never heard of, so the live descriptor resolves to no connectionId and the
    // launch pick sends the window somewhere else. Persist so the repair is a
    // one-time event rather than a recomputation on every read; a failed write
    // still returns the healed registry for this session.
    const reconciled = reconcileRegistryDrift(registry, readDesktopConnectionConfig())

    if (reconciled.changed) {
      registry = reconciled.registry

      try {
        writeDesktopConnectionsRegistry(registry)

        return connectionRegistryCache
      } catch {
        connectionRegistryCache = registry
        connectionRegistryCacheMtime = null

        return registry
      }
    }

    connectionRegistryCache = registry
    connectionRegistryCacheMtime = mtime

    return registry
  }

  // Copy an unparseable connections.json aside (once per corruption event) so a
  // later registry write can never destroy the only copy of the user's saved
  // connections (#94246). Best effort: failure to preserve must not block boot.
  function preserveCorruptRegistrySidecar() {
    try {
      const rawText = fs.readFileSync(DESKTOP_CONNECTIONS_REGISTRY_PATH, 'utf8')

      if (!rawText.trim()) {
        return
      }

      const sidecar = `${DESKTOP_CONNECTIONS_REGISTRY_PATH}.corrupt-${new Date().toISOString().replace(/[:.]/g, '-')}`

      if (!fs.existsSync(sidecar)) {
        fs.writeFileSync(sidecar, rawText, { mode: 0o600 })
      }

      rememberLog(
        `[connections] connections.json could not be parsed; preserved the original file at ${sidecar} and continuing with a local-only registry. No connection data was deleted.`
      )
    } catch {
      // The read itself failed (missing file, permissions) — nothing to save.
    }
  }

  function writeDesktopConnectionsRegistry(registry) {
    fs.mkdirSync(path.dirname(DESKTOP_CONNECTIONS_REGISTRY_PATH), { recursive: true })
    // Owner-only for the same reason as connection.json: entries carry
    // safeStorage-encrypted tokens plus URLs and SSH host/user/keyPath.
    writeSecretFileAtomic(DESKTOP_CONNECTIONS_REGISTRY_PATH, JSON.stringify(registry, null, 2))
    connectionRegistryCache = registry
    connectionRegistryCacheMtime = fs.statSync(DESKTOP_CONNECTIONS_REGISTRY_PATH).mtimeMs
  }

  /**
   * Renderer-facing view of a registry entry: token bytes never cross the IPC
   * boundary — the renderer gets a preview + set flag, mirroring
   * sanitizeDesktopConnectionConfig.
   */
  function sanitizeRegistryConnection(entry) {
    const { token, headers, ...rest } = entry
    const decrypted = decryptDesktopSecret(token)
    // Last-known stable backend identity (from roster enumeration / Test) so
    // Settings can hint "Same backend as <label>" on connections that are two
    // addresses for one box. Display-only; absent until a probe has seen it.
    const knownInstallId = connectionInstallIds.get(entry.id)?.id

    return {
      ...rest,
      tokenSet: Boolean(decrypted),
      tokenPreview: tokenPreview(decrypted),
      ...(knownInstallId ? { installId: knownInstallId } : {}),
      // Header VALUES are secrets (Cloudflare Access client secrets etc.) and
      // never cross the IPC boundary — the renderer only needs the names to
      // render the edit form.
      headerNames: headers && typeof headers === 'object' ? Object.keys(headers) : []
    }
  }

  function sanitizeConnectionsRegistry(registry = readDesktopConnectionsRegistry()) {
    // Same keyring signal the v1 sanitize exposes: lets the Connections panel
    // offer the plain-text opt-in on keyring-less Linux instead of failing.
    // Policy-aware: never touches safeStorage while encryption is opted out.
    const secureTokenStorage = probeSecureTokenStorage()

    return {
      version: registry.version,
      primary: registry.primary,
      launchMode: registry.launchMode,
      lastUsed: registry.lastUsed,
      secureTokenStorage,
      connections: registry.connections.map(sanitizeRegistryConnection),
      // Surface quarantined-entry NOTICES only (reason + best-effort label) —
      // the raw entries can carry token envelopes and stay in the file (#94246).
      quarantined: (registry.quarantined || []).map(q => ({
        reason: String(q?.reason || 'unknown'),
        label:
          q && q.entry && typeof q.entry === 'object' && typeof (q.entry as any).label === 'string'
            ? (q.entry as any).label
            : ''
      }))
    }
  }

  /**
   * Save (create or edit) a registry connection from a renderer payload.
   * Edits merge over the stored entry (mergeConnectionInput) so fields the
   * editor doesn't carry — cloud `org`, ssh `remoteHermesPath`/`remoteProfile` —
   * survive a rename. Token handling mirrors coerceDesktopConnectionConfig: an
   * incoming plaintext token is encrypted (honoring the same allowPlainTextToken
   * opt-in seam as Settings → Gateway); an absent token field inherits the
   * stored envelope on edit; switching auth away from 'token' clears it
   * (normalizeConnectionInput drops tokens on non-token entries).
   */
  async function saveRegistryConnection(input: any = {}) {
    const registry = readDesktopConnectionsRegistry()
    const existing = input.id ? registry.connections.find(c => c.id === input.id) : null
    const incomingToken = typeof input.token === 'string' ? input.token.trim() : ''

    const token = resolvePersistedRemoteToken({
      incomingToken,
      persistToken: true,
      existingToken: existing?.token,
      allowPlainText: input.allowPlainTextToken,
      encryptSecret: encryptDesktopSecret
    })

    // Extra gateway headers arrive as plaintext strings from the editor (or
    // envelopes from a hand-edited import). Encrypt plaintext values the same
    // way tokens are stored; a null/empty value drops that header. An absent
    // `headers` field inherits the stored set via mergeConnectionInput.
    const headers =
      input.headers && typeof input.headers === 'object'
        ? encryptIncomingRemoteHeaders(input.headers, existing?.headers, {
            allowPlainText: input.allowPlainTextToken
          })
        : input.headers

    const merged = mergeConnectionInput({ ...input, token, headers }, existing)
    const entry = normalizeConnectionInput(merged, registry)

    // Token-auth remotes must actually have a token to be dialable. OAuth and
    // cloud entries authenticate via cookies/native tokens instead.
    if (entry.kind === 'remote' && entry.authMode !== 'oauth' && !decryptDesktopSecret(entry.token)) {
      throw new Error('Remote gateway session token is required.')
    }

    if (existing && connectionDialFieldsChanged(existing, entry)) {
      managedConnectionUpdateGate.assertCanMutate(entry.id)
    }

    writeDesktopConnectionsRegistry(upsertConnection(registry, entry))

    // A dial-material edit (endpoint/auth/ssh routing — NOT a label rename)
    // leaves pooled backends under `conn:<id>::*` and renderer sockets pointing
    // at the OLD target while the UI shows the new one. Recycle them: stop this
    // connection's pooled backends/tunnels and tell renderers to dispose+redial
    // their secondaries for this connection id.
    if (existing && connectionDialFieldsChanged(existing, entry)) {
      await stopRegistryConnectionBackends(entry.id)
      broadcastConnectionsChanged({ connectionId: entry.id, reason: 'updated' })
    } else {
      // Every OTHER successful save (a brand-new connection, a label rename)
      // must still republish the registry snapshot, or windows that didn't
      // perform the save — and the switcher menu fed by $connectionsRegistry —
      // keep painting the stale list until reload (#95393). 'saved' is a pure
      // registry-refresh signal: no sockets moved, so listeners must not
      // dispose or redial anything for it.
      broadcastConnectionsChanged({ connectionId: entry.id, reason: 'saved' })
    }

    return sanitizeRegistryConnection(entry)
  }

  // Returns the desktop's chosen profile name, or null when unset. "default" is
  // a valid stored value (pins the root HERMES_HOME explicitly); null means "no
  // preference" and preserves the legacy launch (no --profile flag).
  function readActiveDesktopProfile() {
    try {
      const raw = fs.readFileSync(DESKTOP_PROFILE_CONFIG_PATH, 'utf8')
      const parsed = JSON.parse(raw)
      const name = parsed && typeof parsed.profile === 'string' ? parsed.profile.trim() : ''

      if (name && (name === 'default' || PROFILE_NAME_RE.test(name))) {
        return name
      }
    } catch {
      // Missing or malformed → no preference.
    }

    return null
  }

  function writeActiveDesktopProfile(name) {
    const value = typeof name === 'string' ? name.trim() : ''

    if (value && value !== 'default' && !PROFILE_NAME_RE.test(value)) {
      throw new Error(`Invalid profile name: ${value}`)
    }

    fs.mkdirSync(path.dirname(DESKTOP_PROFILE_CONFIG_PATH), { recursive: true })
    writeFileAtomic(DESKTOP_PROFILE_CONFIG_PATH, JSON.stringify({ profile: value || null }, null, 2))

    return value || null
  }

  // Sanitize a connection config into the renderer-facing shape. With no
  // `profile` this describes the global/default connection (the existing
  // behavior); with a `profile` it describes that profile's per-profile remote
  // override (or an empty "local/inherit" view when the profile has none).
  async function sanitizeDesktopConnectionConfig(config = readDesktopConnectionConfig(), profile = null) {
    const key = connectionScopeKey(profile)
    const scoped = key ? config.profiles?.[key] || null : null
    const block = key ? scoped || {} : config.remote || {}

    const envOverride = key ? false : Boolean(process.env.HERMES_DESKTOP_REMOTE_URL)
    const savedMode = key ? scoped?.mode : config.mode
    const ssh = savedMode === 'ssh' ? normalizeSshConfig(block) : null

    const savedSsh = savedMode === 'local' ? (key ? savedProfileSsh(config, key) : normalizeSshConfig(block)) : null

    const remoteToken = decryptDesktopSecret(block.token)
    const authMode = normAuthMode(block.authMode)
    const remoteUrl = envOverride ? String(process.env.HERMES_DESKTOP_REMOTE_URL || '') : String(block.url || '')
    const mode = envOverride ? 'remote' : savedMode === 'ssh' ? 'ssh' : modeIsRemoteLike(savedMode) ? savedMode : 'local'

    // Whether the OS keyring (safeStorage) can encrypt the saved token. When
    // false the renderer knows to offer the plain-text opt-in in Settings →
    // Gateway. With keychain encryption explicitly opted out this reports true
    // WITHOUT touching safeStorage — probing is itself a keychain touch that
    // raises the macOS password dialog (see probeSecureTokenStorage).
    const secureTokenStorage = probeSecureTokenStorage()

    // Whether the currently saved token is stored in plain text (the explicit
    // keyring-less opt-out path). The env override supplies its token from the
    // environment, not the saved block, so it never reports as plain text here.
    const remoteTokenPlainText = !envOverride && block.token?.encoding === 'plain'

    let remoteOauthConnected = false

    if (authMode === 'oauth' && remoteUrl) {
      try {
        // Display signal: treat a live RT cookie as "connected" even if the AT
        // cookie has lapsed — the gateway refreshes the AT on the next request,
        // so the session is still usable. A stored native bearer token (cookieless
        // RFC 8252 flow) counts as connected too — otherwise a completed native
        // sign-in shows "not connected" in Settings. The authoritative liveness
        // check is the ws-ticket mint in resolveRemoteBackend at actual connect time.
        remoteOauthConnected = oauthSessionIsLive(hasNativeSession(remoteUrl), await hasLiveOauthSession(remoteUrl))
      } catch {
        remoteOauthConnected = false
      }
    }

    return {
      mode,
      // Echo the scope back so the UI knows which profile (if any) this reflects.
      profile: key,
      remoteAuthMode: authMode,
      remoteOauthConnected,
      remoteUrl,
      // The persisted Hermes Cloud org (slug/id) for a cloud connection, or '' for
      // remote/local. Lets Settings → Gateway reopen into the same org.
      cloudOrg: mode === 'cloud' ? String(block.org || '') : '',
      remoteTokenPreview: tokenPreview(remoteToken),
      remoteTokenSet: Boolean(remoteToken),
      // Whether the OS keyring can encrypt a token; drives the plain-text opt-in
      // affordance in Settings → Gateway on keyring-less Linux.
      secureTokenStorage,
      // Whether the saved token is currently persisted in plain text.
      remoteTokenPlainText,
      sshHost: (ssh || savedSsh)?.host || '',
      sshUser: (ssh || savedSsh)?.user || '',
      sshPort: (ssh || savedSsh)?.port || null,
      sshKeyPath: (ssh || savedSsh)?.keyPath || '',
      sshRemoteHermesPath: (ssh || savedSsh)?.remoteHermesPath || '',
      sshRemoteProfile: (ssh || savedSsh)?.remoteProfile || '',
      // The env override only forces the global/primary connection; a per-profile
      // scope is never overridden by HERMES_DESKTOP_REMOTE_URL.
      envOverride
    }
  }

  // Build + validate a `{ url, authMode, token }` remote block. OAuth gateways
  // authenticate via the login-window session cookie (verified at connect time in
  // resolveRemoteBackend), so only token-auth remotes require a saved token.
  // `org` (optional) is the Hermes Cloud org slug/id the instance was discovered
  // under — persisted so Settings can reopen into the same org; omitted from the
  // block when empty so plain remote connections stay unchanged.
  function buildRemoteBlock(remoteUrl, authMode, token, org?: string, headers?: object) {
    if (authMode !== 'oauth' && !decryptDesktopSecret(token)) {
      throw new Error('Remote gateway session token is required.')
    }

    const block: { url: string; authMode: string; token: object; headers?: object; org?: string } = {
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

  function coerceDesktopConnectionConfig(input: any = {}, existing = readDesktopConnectionConfig(), options: any = {}) {
    const persistToken = options.persistToken !== false
    const key = connectionScopeKey(input.profile)
    // 'cloud' and 'remote' both persist a remote-shaped block; 'cloud' is
    // remembered as its own provenance (Q6) and resolves to remote downstream.
    // Anything else collapses to local.
    const mode = input.mode === 'ssh' ? 'ssh' : modeIsRemoteLike(input.mode) ? input.mode : 'local'
    const remoteLike = modeIsRemoteLike(mode)

    // The block being edited: a per-profile entry or the global remote block.
    const rawExistingBlock = key ? existing.profiles?.[key] || {} : existing.remote || {}
    // Leaving a CLOUD connection unselects it: a cloud block's url/org/token
    // describe a discovered Hermes Cloud instance, NOT a user-owned remote gateway,
    // so switching to local or remote must NOT inherit them (otherwise the stale
    // cloud URL lingers and re-selecting Cloud looks "already connected"). When the
    // saved block was cloud and the new mode is not cloud, start from an empty
    // block. (remote↔local toggles still preserve a real remote URL as before.)
    const existingMode = key ? existing.profiles?.[key]?.mode : existing.mode
    const leavingCloud = existingMode === 'cloud' && mode !== 'cloud'
    const leavingSsh = rawExistingBlock.mode === 'ssh' && mode !== 'ssh' && mode !== 'local'
    const existingBlock = leavingCloud || leavingSsh ? {} : rawExistingBlock
    const remoteUrl = String(input.remoteUrl ?? existingBlock.url ?? '').trim()
    // authMode: explicit input wins; otherwise inherit the saved value, default 'token'.
    const authMode = resolveAuthMode(input.remoteAuthMode, existingBlock.authMode)
    // Cloud org: only meaningful for 'cloud' mode. Explicit input wins; otherwise
    // inherit the saved org. A plain 'remote' connection never carries an org
    // (switching cloud→remote drops it), so it stays unset unless mode is cloud.
    const cloudOrg = mode === 'cloud' ? String(input.cloudOrg ?? existingBlock.org ?? '').trim() : ''
    const incomingToken = typeof input.remoteToken === 'string' ? input.remoteToken.trim() : ''

    const remoteHeaders =
      input.remoteHeaders && typeof input.remoteHeaders === 'object'
        ? encryptIncomingRemoteHeaders(input.remoteHeaders, existingBlock.headers, {
            allowPlainText: input.allowPlainTextToken
          })
        : existingBlock.headers

    // Persist decision lives in hardening.resolvePersistedRemoteToken so the
    // IPC-propagation seam (allowPlainTextToken → encryptDesktopSecret opt-in) is
    // covered by a focused regression test. Pass allowPlainText through RAW — the
    // helper coerces with `=== true`, so a truthy-non-true value never enables
    // plain-text storage, and that strictness is asserted in exactly one place.
    const nextToken = resolvePersistedRemoteToken({
      incomingToken,
      persistToken,
      existingToken: existingBlock.token,
      allowPlainText: input.allowPlainTextToken,
      encryptSecret: encryptDesktopSecret
    })

    if (mode === 'ssh') {
      const sshBlock = buildSshBlock(input, savedProfileSsh(existing, key) || rawExistingBlock)

      if (key) {
        const profiles = { ...(existing.profiles || {}), [key]: sshBlock }

        return {
          mode: existing.mode === 'ssh' || modeIsRemoteLike(existing.mode) ? existing.mode : 'local',
          remote: existing.remote || {},
          profiles
        }
      }

      return { mode: 'ssh', remote: sshBlock, profiles: existing.profiles || {} }
    }

    if (key) {
      // Per-profile scope: a remote/cloud entry pins this profile to its own
      // backend; a local entry clears the override so the profile inherits the
      // default. The mode tag (remote vs cloud) is preserved on the entry.
      const profiles = { ...(existing.profiles || {}) }

      if (remoteLike) {
        profiles[key] = {
          mode,
          ...buildRemoteBlock(remoteUrl, authMode, nextToken, cloudOrg, remoteHeaders)
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
      ? buildRemoteBlock(remoteUrl, authMode, nextToken, cloudOrg, remoteHeaders)
      : existingMode === 'ssh'
        ? rawExistingBlock
        : { url: remoteUrl ? normalizeRemoteBaseUrl(remoteUrl) : remoteUrl, authMode, token: nextToken }

    // Preserve per-profile overrides when saving the global connection.
    return { mode, remote: nextRemote, profiles: existing.profiles || {} }
  }

  // Build an SSH connection block from a save payload, preserving an
  // already-adopted dashboard token from the existing block (the token is minted
  // + reconciled at bootstrap, never user-entered). `mode: 'ssh'` is stamped so
  // normalizeSshConfig/profileSshOverride recognize it.
  function buildSshBlock(input: any, existingBlock: any = {}) {
    // `??` (not `||`) so an explicit '' (user CLEARED the field) wins over the
    // saved value; only a truly absent (undefined) field inherits.
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

    // Carry forward an already-adopted dashboard token unless the host changed
    // (a different host invalidates the old dashboard's token).
    if (existingBlock.token && existingBlock.host === merged.host) {
      merged.token = existingBlock.token
    }

    return merged
  }

  // Build a remote backend connection descriptor from an already-resolved remote
  // config. Handles both auth models (OAuth ws-ticket vs static session token)
  // and is shared by the per-profile, env, and global resolution paths. `token`
  // is the DECRYPTED static token (or null in OAuth mode). `source` is a label
  // for diagnostics ('profile' | 'env' | 'settings').
  async function buildRemoteConnection(
    rawUrl,
    authMode,
    token,
    source,
    remoteHost?,
    remoteKind = 'url',
    remoteIdentity?,
    headers?
  ) {
    const baseUrl = normalizeRemoteBaseUrl(rawUrl)
    const remoteHeaders = decryptRemoteHeaders(headers)
    // For token/oauth remotes the meaningful host is the real backend URL; for
    // SSH remotes the caller passes the entered/resolved host explicitly (the
    // baseUrl is a 127.0.0.1 tunnel and would be useless in the pill).
    const host = remoteHost || hostLabelFromBaseUrl(baseUrl)

    if (authMode === 'oauth') {
      // OAuth gateway: auth comes from EITHER a native bearer token (cookieless
      // RFC 8252 flow) OR the session cookies in the OAuth partition. Liveness is
      // NOT "is the access-token cookie present?" — Portal issues a 24h rotating
      // refresh token (hermes #37247), and the gateway middleware transparently
      // rotates a fresh ~15-min access token from it on the next authenticated
      // request. So a session with an expired AT cookie but a live RT cookie is
      // still perfectly connectable. We early-out only when NEITHER a native
      // token NOR any cookie is present, then mint a ws-ticket (which itself
      // prefers the native bearer) as the authoritative liveness check.
      //
      // The native-token check is essential: the native login stores bearer
      // tokens (no cookie is ever set), so gating solely on hasLiveOauthSession
      // here would reject a freshly-completed native sign-in and loop the UI back
      // into "not signed in" even though mintGatewayWsTicket would succeed with
      // the stored bearer.
      if (
        !oauthSessionIsLive(hasNativeSession(baseUrl), await hasLiveOauthSession(baseUrl)) &&
        oauthGuardMayHardFail(await gatewayAuthProviders(baseUrl, remoteHeaders))
      ) {
        throw makeUnsignedOauthError()
      }

      let ticket

      try {
        ticket = await mintGatewayWsTicket(baseUrl, remoteHeaders)
      } catch (error) {
        // For a Nous-managed Cloud agent, a 502/503/504 from the WS-ticket mint
        // means the backend server itself is down — the actionable Cloud-down
        // error. This boundary runs BEFORE the readiness loop, so without this
        // the ticket wrapper below would swallow the server-fault classification
        // and the renderer would never see isCloudBackendDown. Preserve the
        // existing 401/403 reauth and generic transport behavior for everything
        // else (#85335).
        const cloudError = makeNousCloudBackendDownError(baseUrl, error)

        if (cloudError !== null) {
          throw cloudError
        }

        throw gatewayTicketFailure(
          error,
          oauthTicketFailureAuthMessage(hasNativeSession(baseUrl)),
          'Could not reach the remote Hermes gateway while refreshing its WebSocket ticket. Try reconnecting.'
        )
      }

      const wsUrl = buildGatewayWsUrlWithTicket(baseUrl, ticket)

      rememberRemoteWsHeaders(wsUrl, remoteHeaders)

      return {
        baseUrl,
        mode: 'remote',
        source,
        authMode: 'oauth',
        remoteHost: host || undefined,
        remoteIdentity,
        remoteKind,
        headers: remoteHeaders,
        // No static token in OAuth mode; REST is cookie-authed via the partition.
        token: null,
        wsUrl
      }
    }

    if (!token) {
      throw new Error(
        'Remote Hermes gateway is selected, but no session token is saved. ' +
          'Open Settings → Gateway and save a token, or switch back to Local.'
      )
    }

    const wsUrl = buildGatewayWsUrl(baseUrl, token)

    rememberRemoteWsHeaders(wsUrl, remoteHeaders)

    return {
      baseUrl,
      mode: 'remote',
      source,
      authMode: 'token',
      remoteHost: host || undefined,
      remoteIdentity,
      remoteKind,
      headers: remoteHeaders,
      token,
      wsUrl
    }
  }


  return {
    cloudAgentSilentSignIn,
    SECRET_STORAGE_POLICY_PATH,
    _secretStoragePolicyIo,
    _secretStoragePolicy,
    secretStoragePolicy,
    setSecretStoragePolicy,
    encryptDesktopSecret,
    decryptDesktopSecret,
    decryptRemoteHeaders,
    encryptIncomingRemoteHeaders,
    probeSecureTokenStorage,
    rewriteAllStoredSecrets,
    SECRET_STORAGE_TRANSITION_PATH,
    writeSecretStorageTransition,
    readSecretStorageTransition,
    clearSecretStorageTransition,
    decryptSecretForMigration,
    runSecretStorageTransition,
    recoverSecretStorageTransition,
    migrateLegacyEncryptedSecretsOnce,
    applySecretStorageEncryption,
    rememberRemoteWsHeaders,
    headersForRemoteRequest,
    installRemoteHeaderRules,
    sanitizeConnectionProfiles,
    readDesktopConnectionConfig,
    writeDesktopConnectionConfig,
    readDesktopConnectionsRegistry,
    preserveCorruptRegistrySidecar,
    writeDesktopConnectionsRegistry,
    sanitizeRegistryConnection,
    sanitizeConnectionsRegistry,
    saveRegistryConnection,
    readActiveDesktopProfile,
    writeActiveDesktopProfile,
    sanitizeDesktopConnectionConfig,
    buildRemoteBlock,
    coerceDesktopConnectionConfig,
    buildSshBlock,
    buildRemoteConnection
  }
}
