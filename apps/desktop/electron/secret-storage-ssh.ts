import type { PreviewReachRegistry } from './preview-reach'

export function createSecretStorageSsh(deps: Record<string, any>): Record<string, any> {
  let {
    DESKTOP_INSTALLATION_PATH,
    DESKTOP_MANAGED_SSH_RECOVERY_PATH,
    ManagedConnectionUpdateGate,
    PreviewReachRegistry,
    SshConnection,
    adoptServedDashboardToken,
    backendDialClaims,
    backendScopeKey,
    backendScopePrefix,
    connectWindowsRemote,
    connectionScopeKey,
    createBootstrapCoordinator,
    crypto,
    detectRemotePlatform,
    ensureBackend,
    ensureRegistryBackend,
    execText,
    fenceManagedSshBootstrapPublication,
    fetchJson,
    fs,
    loadOrCreateInstallationId,
    managedSshRecoveryScopes,
    managedSshTokenPersistencePlan,
    os,
    path,
    pickLocalPort,
    primaryProfileKey,
    redactSecrets,
    registrySshScopeForWindowRoute,
    rememberLog,
    remoteLifecycle,
    resolveDesktopRemoteRoute,
    resolveRemoteBackend,
    resolveRemoteSshDashboardProfile,
    safeStorage,
    sshConfigFingerprint,
    sshOwnershipId,
    startHermes,
    teardownSshState,
    terminalIpc,
    terminateOwnedWindowsDashboardForUpdate,
    tightenSecretFileMode,
    upsertConnection,
    validateCorrelationId,
    waitForHermes,
    windowConnectionRoutes,
    writeSecretFileAtomic,
    encryptDesktopSecret,
    readDesktopConnectionConfig,
    writeDesktopConnectionConfig,
    readDesktopConnectionsRegistry,
    writeDesktopConnectionsRegistry,
    buildRemoteConnection
  } = deps

  const sshConnections = new Map<string, any>()
  const desktopInstallationId = loadOrCreateInstallationId(DESKTOP_INSTALLATION_PATH)

  // Managed SSH update lifecycle (#93042): while an update owns a registered
  // SSH connection, the gate pauses new dials and dial-material mutations for
  // that connection id; the durable recovery journal below survives a crash
  // mid-transaction so the next launch can restore every drained scope.
  const managedConnectionUpdateGate = new ManagedConnectionUpdateGate(
    connectionId =>
      readManagedSshRecoveryRecords().find(record => record.connectionId === connectionId)?.correlationId || null
  )

  const managedConnectionUpdates = new Map<string, Promise<any>>()
  const managedConnectionRecoveries = new Map<string, Promise<void>>()
  const managedPrimaryRestoreOwners = new Map<string, { correlationId: string; profile: string; source: any }>()
  let managedUpdateQuitWait: Promise<void> | null = null
  let managedUpdateQuitWaitDone = false

  function assertCanMutateManagedPrimaryRouting() {
    const durableIds = readManagedSshRecoveryRecords().map(record => record.connectionId)

    const ids = new Set([
      ...managedConnectionUpdates.keys(),
      ...managedConnectionRecoveries.keys(),
      ...managedPrimaryRestoreOwners.keys(),
      ...durableIds
    ])

    if (ids.size > 0) {
      const error: any = new Error(
        `Primary connection routing cannot change while managed SSH update recovery is pending for ${[...ids].join(', ')}.`
      )

      error.code = 'managed-update-in-progress'
      throw error
    }
  }

  function readManagedSshRecoveryRecords(): any[] {
    try {
      const stat = fs.lstatSync(DESKTOP_MANAGED_SSH_RECOVERY_PATH)

      if (!stat.isFile() || stat.isSymbolicLink() || !tightenSecretFileMode(DESKTOP_MANAGED_SSH_RECOVERY_PATH)) {
        throw new Error('Managed SSH recovery journal is not a safe owner-only file.')
      }

      const payload = JSON.parse(fs.readFileSync(DESKTOP_MANAGED_SSH_RECOVERY_PATH, 'utf8'))

      if (payload?.version !== 1 || !Array.isArray(payload.records)) {
        throw new Error('Managed SSH recovery journal has an unsupported shape.')
      }

      const valid = payload.records.every(record => {
        if (
          !record ||
          typeof record !== 'object' ||
          typeof record.connectionId !== 'string' ||
          record.source?.kind !== 'ssh' ||
          record.source?.id !== record.connectionId ||
          !['prepared', 'launching'].includes(record.phase) ||
          !Array.isArray(record.scopes) ||
          record.scopes.length > 256
        ) {
          return false
        }

        try {
          validateCorrelationId(record.correlationId)
        } catch {
          return false
        }

        const scopesValid = record.scopes.every(
          scope =>
            scope &&
            typeof scope === 'object' &&
            typeof scope.key === 'string' &&
            scope.key.length <= 256 &&
            typeof scope.profile === 'string' &&
            scope.profile.length > 0 &&
            scope.profile.length <= 128 &&
            ['legacy', 'primary', 'registry'].includes(scope.kind) &&
            (scope.kind === 'primary' || scope.key.length > 0)
        )

        const identities = record.scopes.map(scope => `${scope.kind}\0${scope.key}\0${scope.profile}`)

        return (
          scopesValid &&
          new Set(identities).size === identities.length &&
          record.scopes.filter(scope => scope.kind === 'primary').length <= 1
        )
      })

      if (!valid) {
        throw new Error('Managed SSH recovery journal contains an invalid record.')
      }

      return payload.records
    } catch (cause: any) {
      if (cause?.code === 'ENOENT') {
        return []
      }

      const error: any = new Error(
        'Managed SSH recovery state is unreadable or malformed; refusing connection startup and edits.'
      )

      error.code = 'managed-update-recovery-unavailable'
      error.cause = cause
      throw error
    }
  }

  function writeManagedSshRecoveryRecords(records) {
    fs.mkdirSync(path.dirname(DESKTOP_MANAGED_SSH_RECOVERY_PATH), { recursive: true })
    writeSecretFileAtomic(
      DESKTOP_MANAGED_SSH_RECOVERY_PATH,
      JSON.stringify({ version: 1, records, updatedAt: new Date().toISOString() }, null, 2)
    )
  }

  function persistManagedSshRecovery(source, correlationId, scopes) {
    const prefix = backendScopePrefix(source.id)
    const recoveryScopes = managedSshRecoveryScopes(scopes, prefix)

    const records = readManagedSshRecoveryRecords().filter(record => record.connectionId !== source.id)
    records.push({
      connectionId: source.id,
      correlationId: validateCorrelationId(correlationId),
      createdAt: new Date().toISOString(),
      phase: 'prepared',
      scopes: recoveryScopes,
      // Registry secrets are already safeStorage envelopes. Persist the exact
      // connection snapshot so crash recovery does not silently switch hosts or
      // credentials after a Settings edit.
      source
    })
    writeManagedSshRecoveryRecords(records)
  }

  function markManagedSshRecoveryLaunching(connectionId, correlationId) {
    const records = readManagedSshRecoveryRecords()

    const index = records.findIndex(
      record => record.connectionId === connectionId && record.correlationId === correlationId
    )

    if (index < 0) {
      throw new Error('Managed SSH recovery record disappeared before remote update launch.')
    }

    records[index] = { ...records[index], phase: 'launching' }
    writeManagedSshRecoveryRecords(records)
  }

  function clearManagedSshRecovery(connectionId, correlationId) {
    const records = readManagedSshRecoveryRecords()

    const remaining = records.filter(
      record => record.connectionId !== connectionId || record.correlationId !== correlationId
    )

    if (remaining.length === records.length) {
      return
    }

    if (remaining.length > 0) {
      writeManagedSshRecoveryRecords(remaining)
    } else {
      try {
        fs.unlinkSync(DESKTOP_MANAGED_SSH_RECOVERY_PATH)
      } catch (error: any) {
        if (error?.code !== 'ENOENT') {
          throw error
        }
      }
    }
  }

  const sshBootstrapCoordinator = createBootstrapCoordinator()

  let sshQuitTeardownDone = false
  let backendQuitTeardownDone = false

  function sshScopeKey(profile) {
    return connectionScopeKey(profile) || ''
  }

  function sshOwnershipKey(profile) {
    return sshOwnershipId(desktopInstallationId, sshScopeKey(profile))
  }

  function sshRememberLog(chunk) {
    rememberLog(redactSecrets(String(chunk == null ? '' : chunk)))
  }

  async function sshProbeReuseProof(baseUrl, token, spawnNonce) {
    try {
      const proof: any = await fetchJson(`${baseUrl}/api/ssh/ownership`, token)

      return remoteLifecycle.classifySshReuseProof(proof, spawnNonce)
    } catch (error: any) {
      if (/^(401|403|404):/.test(String(error?.message || ''))) {
        return 'authenticated-stale'
      }

      throw error
    }
  }

  async function teardownSshConnection(profile) {
    const scope = sshScopeKey(profile)
    const state = sshConnections.get(scope)

    if (!state) {
      return
    }

    sshConnections.delete(scope)

    terminalIpc.disposeTerminalSessionsForSshScope(scope)

    // Kill the owned remote serve --isolated *before* closing the SSH
    // transport. Spawn detaches with setsid/nohup, so closing the tunnel
    // alone leaves the backend at pid 1 holding state.db (#91668).
    // Windows remotes use a different lifecycle (connectWindowsRemote) and
    // are left to a follow-up; POSIX is the leak that OOM'd gateways.
    await teardownSshState(
      {
        ...state,
        ownershipId: state.ownershipId || sshOwnershipKey(profile)
      },
      {
        cleanupRemote:
          state.remotePlatform === 'Windows'
            ? async () => {
                // connectWindowsRemote does not share POSIX lock/kill. Stay
                // silent on the kill path, but leave a log so quit is not a
                // mysterious no-op on Windows remotes.
                sshRememberLog('[ssh] skip remote serve teardown on Windows remotes; POSIX disconnect does not apply')
              }
            : remoteLifecycle.disconnect
      }
    )
  }

  // CRITICAL: this must mirror resolveRemoteBackend's precedence, not just return
  // any cached SSH state. A per-profile token/OAuth override wins over a global
  // SSH connection — so if the active profile resolves to a NON-SSH backend, the
  // terminal must NOT fall through to a global SSH host.
  function activeSshTerminalTarget(webContentsId?: number) {
    const windowRoute = typeof webContentsId === 'number' ? windowConnectionRoutes.get(webContentsId) : null

    if (windowRoute?.registryScoped && windowRoute.connectionId) {
      const scope = registrySshScopeForWindowRoute(windowRoute, readDesktopConnectionsRegistry())

      if (!scope) {
        return null
      }

      const state = sshConnections.get(scope)

      return state && state.ssh ? { ssh: state.ssh, scope } : 'pending'
    }

    const profile = windowRoute?.profile ?? primaryProfileKey()
    const config = readDesktopConnectionConfig()

    const route = resolveDesktopRemoteRoute({
      config,
      env: {
        token: process.env.HERMES_DESKTOP_REMOTE_TOKEN,
        url: process.env.HERMES_DESKTOP_REMOTE_URL
      },
      profile,
      registry: readDesktopConnectionsRegistry()
    })

    if (!route || route.kind !== 'ssh') {
      return null
    }

    const scope = route.connectionId
      ? backendScopeKey(route.connectionId, profile)
      : sshScopeKey(route.source === 'profile' ? profile : null)

    const state = sshConnections.get(scope)

    return state && state.ssh ? { ssh: state.ssh, scope } : 'pending'
  }

  async function ensureTerminalBackend(webContentsId: number) {
    const windowRoute = windowConnectionRoutes.get(webContentsId)

    // Claim-guarded (#90812): opening a terminal pane can race a renderer's own
    // reconnect dial for the same (connectionId, profile) scope; coalescing
    // here avoids bootstrapping a second SSH tunnel / remote dashboard.
    if (windowRoute?.registryScoped && windowRoute.connectionId) {
      return backendDialClaims.run(backendScopeKey(windowRoute.connectionId, windowRoute.profile), () =>
        ensureRegistryBackend(windowRoute.connectionId, windowRoute.profile)
      )
    }

    const profile = windowRoute?.profile ?? primaryProfileKey()

    return backendDialClaims.run(backendScopeKey(null, profile), () => ensureBackend(profile))
  }

  // Loopback reach for the browser pane. Scoped to the SSH connection that
  // authorized it: a different host (or none) must never inherit live forwards
  // into somebody else's machine.
  const previewReachByWebContents = new Map<number, { registry: PreviewReachRegistry; scope: string }>()

  async function resetPreviewReach(webContentsId?: number) {
    if (typeof webContentsId === 'number') {
      const current = previewReachByWebContents.get(webContentsId)

      previewReachByWebContents.delete(webContentsId)

      if (current) {
        await current.registry.closeAll()
      }

      return
    }

    const open = [...previewReachByWebContents.values()]

    previewReachByWebContents.clear()
    await Promise.allSettled(open.map(entry => entry.registry.closeAll()))
  }

  /**
   * Rewrite a gateway-loopback URL into one this machine can actually load.
   *
   * Returns the URL unchanged when no rewrite is needed or possible — a local
   * backend (the address is already true), a non-loopback host, or a url/cloud
   * remote with no tunnel to borrow. Callers must not treat an unchanged URL as
   * failure; the pane explains an unreachable one on its own.
   */
  async function reachablePreviewUrl(webContentsId: number, rawUrl: string): Promise<string> {
    let target = activeSshTerminalTarget(webContentsId)

    if (target === 'pending') {
      await ensureTerminalBackend(webContentsId).catch(() => undefined)
      target = activeSshTerminalTarget(webContentsId)
    }

    if (!target || target === 'pending') {
      // No SSH transport behind this renderer's gateway. Another window's
      // forward must never be reused for this preview.
      await resetPreviewReach(webContentsId)

      return rawUrl
    }

    const { scope, ssh } = target as { scope: string; ssh: any }
    let reach = previewReachByWebContents.get(webContentsId)

    if (!reach || reach.scope !== scope) {
      await resetPreviewReach(webContentsId)
      reach = { registry: new PreviewReachRegistry(), scope }
      previewReachByWebContents.set(webContentsId, reach)
    }

    try {
      const rewritten = await reach.registry.resolve(rawUrl, {
        cancel: (localPort, remotePort) => ssh.cancelForward(localPort, remotePort),
        forward: (localPort, remotePort, remoteHost) => ssh.forward(localPort, remotePort, remoteHost),
        isCurrent: () => sshConnections.get(scope)?.ssh === ssh,
        // pickLocalPort predates the typed surface here and infers `unknown`.
        pickLocalPort: () => pickLocalPort() as Promise<number>
      })

      return rewritten || rawUrl
    } catch (error: any) {
      sshRememberLog(`preview reach failed for ${rawUrl}: ${error?.message || error}`)

      return rawUrl
    }
  }

  async function effectiveSshConfigFingerprint(sshConfig) {
    const ssh =
      process.platform === 'win32'
        ? path.join(process.env.SystemRoot || 'C:\\Windows', 'System32', 'OpenSSH', 'ssh.exe')
        : 'ssh'

    const args = ['-G']

    if (sshConfig.port) {
      args.push('-p', String(sshConfig.port))
    }

    if (sshConfig.keyPath) {
      args.push('-i', sshConfig.keyPath)
    }

    args.push('--', sshConfig.user ? `${sshConfig.user}@${sshConfig.host}` : sshConfig.host)
    const output = await execText(ssh, args, { timeout: 10_000 })

    return crypto.createHash('sha256').update(output).digest('hex')
  }

  async function bootstrapSshConnection(
    profile,
    sshConfig,
    reuseToken,
    source,
    resolvedEffectiveFingerprint?,
    metadata: any = {}
  ) {
    const scope = sshScopeKey(profile)
    const effectiveConfigFingerprint = resolvedEffectiveFingerprint || (await effectiveSshConfigFingerprint(sshConfig))
    const resolvedConfig = { ...sshConfig, effectiveConfigFingerprint }
    const fingerprint = sshConfigFingerprint(scope, resolvedConfig)

    return sshBootstrapCoordinator.start(
      scope,
      fingerprint,
      lease => bootstrapSshConnectionInner(profile, resolvedConfig, reuseToken, source, metadata, fingerprint, lease),
      metadata
    )
  }

  // Tear down a bootstrap result whose publication lost the managed-update
  // fence: exact-terminate the serve this bootstrap owns (never a foreign one),
  // drop its forward and transport, and surface a fence error so the managed
  // updater refuses to mutate a remote install with an unfenced serve.
  async function rollbackSshBootstrapResult(ssh, result, profile, sshConfig, boundaryError) {
    const cleanupErrors: string[] = []
    const scope = sshScopeKey(profile)

    try {
      const expected = {
        ownershipId: result.ownershipId,
        pid: result.pid,
        spawnNonce: result.spawnNonce,
        profile: resolveRemoteSshDashboardProfile(sshConfig.remoteProfile, profile),
        hermesPath: result.hermesPath,
        hermesHome: result.hermesHome,
        startedAt: result.startedAt,
        creationTimeNs: result.creationTimeNs,
        creationTime: result.creationTime
      }

      if (result.platform?.os === 'Windows') {
        await terminateOwnedWindowsDashboardForUpdate(
          ssh,
          { hermesPath: result.hermesPath, hermesHome: result.hermesHome, python: result.pythonPath },
          expected
        )
      } else if (result.platform?.os === 'Linux' || result.platform?.os === 'Darwin') {
        await remoteLifecycle.terminateOwnedDashboardForUpdate(ssh, expected)
      } else {
        cleanupErrors.push(`unsupported remote platform ${result.platform?.os || 'unknown'}`)
      }
    } catch (error: any) {
      cleanupErrors.push(String(error?.message || error))
    }

    try {
      await ssh.cancelForward(result.localPort, result.remotePort)
    } catch (error: any) {
      cleanupErrors.push(String(error?.message || error))
    }

    try {
      await ssh.close()
    } catch (error: any) {
      cleanupErrors.push(String(error?.message || error))
    }

    if (sshConnections.get(scope)?.ssh === ssh) {
      sshConnections.delete(scope)
    }

    if (cleanupErrors.length > 0) {
      const unsafe: any = new Error(
        `An SSH bootstrap crossed the managed-update gate and its exact owned serve could not be fenced: ${cleanupErrors.join('; ')}`
      )

      unsafe.code = 'managed-update-bootstrap-fence-failed'
      unsafe.unsafeManagedBootstrap = true
      unsafe.cause = boundaryError
      throw unsafe
    }
  }

  async function bootstrapSshConnectionInner(profile, sshConfig, reuseToken, source, metadata, fingerprint, lease) {
    const scope = sshScopeKey(profile)
    const hostLabel = sshConfig.user ? `${sshConfig.user}@${sshConfig.host}` : sshConfig.host
    const existing = sshConnections.get(scope)

    if (existing && existing.fingerprint !== fingerprint) {
      await teardownSshConnection(profile)
    }

    let ssh = sshConnections.get(scope)?.ssh

    if (ssh && !(await ssh.isAlive())) {
      try {
        await ssh.close()
      } catch {
        void 0
      }

      ssh = null
      sshConnections.delete(scope)
    }

    const created = !ssh

    let removeForceCleanup = () => {}

    if (created) {
      ssh = new SshConnection(
        { host: sshConfig.host, user: sshConfig.user, port: sshConfig.port, keyPath: sshConfig.keyPath },
        {
          rememberLog: sshRememberLog,
          ownershipId: sshOwnershipKey(profile),
          scope,
          effectiveConfigFingerprint: sshConfig.effectiveConfigFingerprint
        }
      )
      removeForceCleanup = lease.onForceCleanup(() => ssh.close())
      await ssh.open({ signal: lease.signal })
    }

    let result: any

    try {
      if (metadata.registryConnectionId) {
        managedConnectionUpdateGate.assertCanDial(metadata.registryConnectionId, metadata.managedUpdateCorrelation || '')
      }

      const platform = await detectRemotePlatform(ssh, sshConfig.remoteHermesPath || '')
      const lifecycle = platform.os === 'Windows' ? connectWindowsRemote : remoteLifecycle.connect
      result = await lifecycle({
        ssh,
        profile: resolveRemoteSshDashboardProfile(sshConfig.remoteProfile, profile),
        remoteHermesPath: sshConfig.remoteHermesPath || '',
        ownershipId: sshOwnershipKey(profile),
        reuseToken: reuseToken || '',
        forward: (localPort, remotePort) => ssh.forward(localPort, remotePort),
        cancelForward: (localPort, remotePort) => ssh.cancelForward(localPort, remotePort),
        pickLocalPort,
        waitForHermes: (baseUrl, token) => waitForHermes(baseUrl, token, lease.signal, 'token'),
        probeReuseProof: sshProbeReuseProof,
        adoptServedToken: adoptServedDashboardToken,
        rememberLog: sshRememberLog,
        signal: lease.signal
      })
    } catch (error: any) {
      if (created) {
        try {
          await ssh.close()
        } catch {
          void 0
        }
      } else {
        // The cached master was reused but the lifecycle probe against it
        // failed ("Could not verify the existing SSH backend"). Keeping the
        // stale entry means every subsequent boot re-attempts through the same
        // wedged master/tunnel and fails identically until the user re-enters
        // the connection details (whose changed fingerprint forces a teardown).
        // Tear it down now so the next attempt — automatic retry included —
        // bootstraps a fresh master, which is exactly what manual re-entry
        // did (#82679).
        try {
          await teardownSshConnection(profile)
        } catch {
          void 0
        }
      }

      const err = new Error(error.message) as any
      err.sshError = error.kind || 'unknown'
      err.isSshBootstrap = true
      throw err
    }

    try {
      lease.assertCurrent()
    } catch (error) {
      await rollbackSshBootstrapResult(ssh, result, profile, sshConfig, error)
      throw error
    }

    await fenceManagedSshBootstrapPublication({
      assertCanPublish: () => {
        if (metadata.registryConnectionId) {
          managedConnectionUpdateGate.assertCanDial(
            metadata.registryConnectionId,
            metadata.managedUpdateCorrelation || ''
          )
        }
      },
      publish: () => {
        persistSshConnectionToken(profile, source, result.token, metadata.registryConnectionId)
        removeForceCleanup()
        sshConnections.set(scope, {
          ssh,
          fingerprint,
          ownershipId: result.ownershipId || sshOwnershipKey(profile),
          localPort: result.localPort,
          remotePort: result.remotePort,
          pid: result.pid,
          host: sshConfig.host,
          hostLabel,
          hermesVersion: result.hermesVersion || '',
          remotePlatform: result.platform?.os || '',
          reused: result.reused,
          spawnNonce: result.spawnNonce,
          creationTimeNs: result.creationTimeNs,
          creationTime: result.creationTime,
          startedAt: result.startedAt,
          hermesPath: result.hermesPath,
          hermesHome: result.hermesHome,
          pythonPath: result.pythonPath,
          remoteProfile: resolveRemoteSshDashboardProfile(sshConfig.remoteProfile, profile),
          registryConnectionId:
            metadata.registryConnectionId ||
            (typeof source === 'string' && source.startsWith('registry:') ? source.slice('registry:'.length) : ''),
          // Never infer primary ownership from a non-composite scope key: legacy
          // per-profile pools also use bare keys. Only startHermes' explicit call
          // site may label a registry-qualified SSH scope as the primary backend.
          primaryRegistryScope: metadata.primaryRegistryScope === true
        })
      },
      rollback: error => rollbackSshBootstrapResult(ssh, result, profile, sshConfig, error)
    })

    sshRememberLog(
      `[ssh] connection ${result.reused ? 'REUSED' : 'spawned'} dashboard: ` +
        `${result.hermesVersion || 'hermes (version unknown)'} at ${result.hermesPath || '?'}`
    )

    const connection = await buildRemoteConnection(
      result.baseUrl,
      'token',
      result.token,
      source,
      hostLabel,
      'ssh',
      result.ownershipId
    )

    return {
      ...connection,
      remoteHermesVersion: result.hermesVersion || '',
      ssh: {
        effectiveConfigFingerprint: sshConfig.effectiveConfigFingerprint,
        host: sshConfig.host,
        keyPath: sshConfig.keyPath,
        port: sshConfig.port,
        remoteHermesPath: sshConfig.remoteHermesPath,
        remoteProfile: sshConfig.remoteProfile,
        user: sshConfig.user
      }
    }
  }

  function persistSshConnectionToken(profile, source, token, registryConnectionId = '') {
    try {
      const persistence = managedSshTokenPersistencePlan(source, registryConnectionId)
      const id = persistence.registryConnectionId
      const encrypted = encryptDesktopSecret(token)

      // A primary legacy route can also be qualified with a stable registry id.
      // Mirror the adopted per-serve token to both stores so the next primary
      // launch and a later registry-scoped launch reuse the same owned process.
      if (id) {
        const registry = readDesktopConnectionsRegistry()
        const entry = registry.connections.find(c => c.id === id)

        if (entry && entry.kind === 'ssh') {
          writeDesktopConnectionsRegistry(upsertConnection(registry, { ...entry, token: encrypted }))
        }
      }

      if (!persistence.legacySource) {
        return
      }

      const config = readDesktopConnectionConfig()

      if (persistence.legacySource === 'profile') {
        const key = connectionScopeKey(profile)

        if (key && config.profiles?.[key]?.mode === 'ssh') {
          config.profiles[key].token = encrypted
          writeDesktopConnectionConfig(config)
        }
      } else if (config.mode === 'ssh' && config.remote) {
        config.remote.token = encrypted
        writeDesktopConnectionConfig(config)
      }
    } catch (error: any) {
      sshRememberLog(`[ssh] could not persist served token: ${error.message}`)
    }
  }


  return {
    sshConnections,
    desktopInstallationId,
    managedConnectionUpdateGate,
    managedConnectionUpdates,
    managedConnectionRecoveries,
    managedPrimaryRestoreOwners,
    managedUpdateQuitWait,
    managedUpdateQuitWaitDone,
    assertCanMutateManagedPrimaryRouting,
    readManagedSshRecoveryRecords,
    writeManagedSshRecoveryRecords,
    persistManagedSshRecovery,
    markManagedSshRecoveryLaunching,
    clearManagedSshRecovery,
    sshBootstrapCoordinator,
    sshQuitTeardownDone,
    backendQuitTeardownDone,
    sshScopeKey,
    sshOwnershipKey,
    sshRememberLog,
    sshProbeReuseProof,
    teardownSshConnection,
    activeSshTerminalTarget,
    ensureTerminalBackend,
    previewReachByWebContents,
    resetPreviewReach,
    reachablePreviewUrl,
    effectiveSshConfigFingerprint,
    bootstrapSshConnection,
    rollbackSshBootstrapResult,
    bootstrapSshConnectionInner,
    persistSshConnectionToken
  }
}
