import { contextBridge, ipcRenderer, webUtils } from 'electron'

// Compile-time brand gates (see the DCE contract in brand-gates.ts): in the
// packaged preload these inline to literals, so the inactive brand's IPC
// namespace — including its channel strings — is dead-code-eliminated from
// dist/electron-preload.js, not just left unregistered.
// check-brand-separation.mjs asserts that on the bundle.
import { IS_FOUNDRLY_BRAND, IS_IX_AGENCY_BRAND, IS_QUIZVERSE_BRAND } from './brand-gates'

let voiceCaptureToken = ''
ipcRenderer.on('hermes:voice:capture-authorized', (_event, token) => {
  voiceCaptureToken = typeof token === 'string' ? token : ''
})

async function consumeVoiceCaptureAttestation(): Promise<string> {
  if (!voiceCaptureToken) {
    throw new Error('No current approved microphone capture.')
  }

  voiceCaptureToken = ''

  return ipcRenderer.invoke('hermes:voice:consume-capture-attestation')
}

contextBridge.exposeInMainWorld('hermesDesktop', {
  getConnection: profile => ipcRenderer.invoke('hermes:connection', profile),
  revalidateConnection: () => ipcRenderer.invoke('hermes:connection:revalidate'),
  touchBackend: profile => ipcRenderer.invoke('hermes:backend:touch', profile),
  getGatewayWsUrl: profile => ipcRenderer.invoke('hermes:gateway:ws-url', profile),
  openSessionWindow: (sessionId, opts) => ipcRenderer.invoke('hermes:window:openSession', sessionId, opts),
  openNewSessionWindow: () => ipcRenderer.invoke('hermes:window:openNewSession'),
  openWindow: () => ipcRenderer.invoke('hermes:window:openInstance'),
  claimAmbientCue: key => ipcRenderer.invoke('hermes:ambient:claim', key),
  voice: {
    consumeCaptureAttestation: consumeVoiceCaptureAttestation
  },
  petOverlay: {
    // Main renderer → main process: window lifecycle + drag. `request` is
    // `{ bounds, screen }`; resolves with the screen bounds it actually used.
    open: request => ipcRenderer.invoke('hermes:pet-overlay:open', request),
    close: () => ipcRenderer.invoke('hermes:pet-overlay:close'),
    setBounds: bounds => ipcRenderer.send('hermes:pet-overlay:set-bounds', bounds),
    setIgnoreMouse: ignore => ipcRenderer.send('hermes:pet-overlay:ignore-mouse', ignore),
    // Flip the overlay focusable (and focus it) while the composer needs keys.
    setFocusable: focusable => ipcRenderer.send('hermes:pet-overlay:set-focusable', focusable),
    // Main renderer → overlay (forwarded by main): push the latest pet state.
    pushState: payload => ipcRenderer.send('hermes:pet-overlay:state', payload),
    // Overlay → main renderer (forwarded by main): pop back in / composer submit.
    control: payload => ipcRenderer.send('hermes:pet-overlay:control', payload),
    // Overlay subscribes to state pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('hermes:pet-overlay:state', listener)

      return () => ipcRenderer.removeListener('hermes:pet-overlay:state', listener)
    },
    // Main renderer subscribes to overlay control messages.
    onControl: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('hermes:pet-overlay:control', listener)

      return () => ipcRenderer.removeListener('hermes:pet-overlay:control', listener)
    }
  },
  getBootProgress: () => ipcRenderer.invoke('hermes:boot-progress:get'),
  getConnectionConfig: profile => ipcRenderer.invoke('hermes:connection-config:get', profile),
  saveConnectionConfig: payload => ipcRenderer.invoke('hermes:connection-config:save', payload),
  applyConnectionConfig: payload => ipcRenderer.invoke('hermes:connection-config:apply', payload),
  testConnectionConfig: payload => ipcRenderer.invoke('hermes:connection-config:test', payload),
  sshConfigHosts: () => ipcRenderer.invoke('hermes:ssh-config:hosts'),
  sshResolveHost: host => ipcRenderer.invoke('hermes:ssh-config:resolve', host),
  probeConnectionConfig: remoteUrl => ipcRenderer.invoke('hermes:connection-config:probe', remoteUrl),
  oauthLoginConnectionConfig: remoteUrl => ipcRenderer.invoke('hermes:connection-config:oauth-login', remoteUrl),
  oauthLogoutConnectionConfig: remoteUrl => ipcRenderer.invoke('hermes:connection-config:oauth-logout', remoteUrl),
  // Hermes Cloud: one portal login powers discovery + silent per-agent sign-in
  // (cloud-auto-discovery Phase 3).
  cloud: {
    status: () => ipcRenderer.invoke('hermes:cloud:status'),
    login: () => ipcRenderer.invoke('hermes:cloud:login'),
    logout: () => ipcRenderer.invoke('hermes:cloud:logout'),
    discover: org => ipcRenderer.invoke('hermes:cloud:discover', org),
    agentSignIn: dashboardUrl => ipcRenderer.invoke('hermes:cloud:agent-sign-in', dashboardUrl)
  },
  profile: {
    get: () => ipcRenderer.invoke('hermes:profile:get'),
    set: name => ipcRenderer.invoke('hermes:profile:set', name)
  },
  api: request => ipcRenderer.invoke('hermes:api', request),
  notify: payload => ipcRenderer.invoke('hermes:notify', payload),
  requestMicrophoneAccess: () => ipcRenderer.invoke('hermes:requestMicrophoneAccess'),
  readFileDataUrl: filePath => ipcRenderer.invoke('hermes:readFileDataUrl', filePath),
  readFileText: filePath => ipcRenderer.invoke('hermes:readFileText', filePath),
  selectPaths: options => ipcRenderer.invoke('hermes:selectPaths', options),
  writeClipboard: text => ipcRenderer.invoke('hermes:writeClipboard', text),
  saveImageFromUrl: url => ipcRenderer.invoke('hermes:saveImageFromUrl', url),
  saveImageBuffer: (data, ext) => ipcRenderer.invoke('hermes:saveImageBuffer', { data, ext }),
  saveClipboardImage: () => ipcRenderer.invoke('hermes:saveClipboardImage'),
  getPathForFile: file => {
    try {
      return webUtils.getPathForFile(file) || ''
    } catch {
      return ''
    }
  },
  normalizePreviewTarget: (target, baseDir) => ipcRenderer.invoke('hermes:normalizePreviewTarget', target, baseDir),
  watchPreviewFile: url => ipcRenderer.invoke('hermes:watchPreviewFile', url),
  stopPreviewFileWatch: id => ipcRenderer.invoke('hermes:stopPreviewFileWatch', id),
  setTitleBarTheme: payload => ipcRenderer.send('hermes:titlebar-theme', payload),
  setNativeTheme: mode => ipcRenderer.send('hermes:native-theme', mode),
  setTranslucency: payload => ipcRenderer.send('hermes:translucency', payload),
  setKeepAwake: on => ipcRenderer.send('hermes:keep-awake', on),
  setPreviewShortcutActive: active => ipcRenderer.send('hermes:previewShortcutActive', Boolean(active)),
  openExternal: url => ipcRenderer.invoke('hermes:openExternal', url),
  openPreviewInBrowser: url => ipcRenderer.invoke('hermes:openPreviewInBrowser', url),
  fetchLinkTitle: url => ipcRenderer.invoke('hermes:fetchLinkTitle', url),
  sanitizeWorkspaceCwd: cwd => ipcRenderer.invoke('hermes:workspace:sanitize', cwd),
  settings: {
    getDefaultProjectDir: () => ipcRenderer.invoke('hermes:setting:defaultProjectDir:get'),
    setDefaultProjectDir: dir => ipcRenderer.invoke('hermes:setting:defaultProjectDir:set', dir),
    pickDefaultProjectDir: () => ipcRenderer.invoke('hermes:setting:defaultProjectDir:pick')
  },
  studio: {
    status: () => ipcRenderer.invoke('hermes:studio:status'),
    chooseExternal: () => ipcRenderer.invoke('hermes:studio:choose-external'),
    consentInstall: version => ipcRenderer.invoke('hermes:studio:install-consent', version),
    launch: input => ipcRenderer.invoke('hermes:studio:launch', input),
    focus: () => ipcRenderer.invoke('hermes:studio:focus'),
    stop: () => ipcRenderer.invoke('hermes:studio:stop')
  },
  zoom: {
    // Current zoom of this window, as { level, percent }.
    get: () => ipcRenderer.invoke('hermes:zoom:get'),
    setPercent: percent => ipcRenderer.send('hermes:zoom:set-percent', percent),
    // Fires on every zoom change, including the Ctrl/Cmd +/-/0 shortcuts,
    // so the settings UI can stay in sync with the keyboard.
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('hermes:zoom:changed', listener)

      return () => ipcRenderer.removeListener('hermes:zoom:changed', listener)
    }
  },
  revealLogs: () => ipcRenderer.invoke('hermes:logs:reveal'),
  getRecentLogs: () => ipcRenderer.invoke('hermes:logs:recent'),
  readDir: dirPath => ipcRenderer.invoke('hermes:fs:readDir', dirPath),
  gitRoot: startPath => ipcRenderer.invoke('hermes:fs:gitRoot', startPath),
  revealPath: targetPath => ipcRenderer.invoke('hermes:fs:reveal', targetPath),
  openDir: dirPath => ipcRenderer.invoke('hermes:fs:openDir', dirPath),
  renamePath: (targetPath, newName) => ipcRenderer.invoke('hermes:fs:rename', targetPath, newName),
  writeTextFile: (filePath, content) => ipcRenderer.invoke('hermes:fs:writeText', filePath, content),
  trashPath: targetPath => ipcRenderer.invoke('hermes:fs:trash', targetPath),
  git: {
    worktreeList: repoPath => ipcRenderer.invoke('hermes:git:worktreeList', repoPath),
    worktreeAdd: (repoPath, options) => ipcRenderer.invoke('hermes:git:worktreeAdd', repoPath, options),
    worktreeRemove: (repoPath, worktreePath, options) =>
      ipcRenderer.invoke('hermes:git:worktreeRemove', repoPath, worktreePath, options),
    branchSwitch: (repoPath, branch) => ipcRenderer.invoke('hermes:git:branchSwitch', repoPath, branch),
    branchList: repoPath => ipcRenderer.invoke('hermes:git:branchList', repoPath),
    baseBranchList: repoPath => ipcRenderer.invoke('hermes:git:baseBranchList', repoPath),
    repoStatus: repoPath => ipcRenderer.invoke('hermes:git:repoStatus', repoPath),
    fileDiff: (repoPath, filePath) => ipcRenderer.invoke('hermes:git:fileDiff', repoPath, filePath),
    scanRepos: (roots, options) => ipcRenderer.invoke('hermes:git:scanRepos', roots, options),
    review: {
      list: (repoPath, scope, baseRef) => ipcRenderer.invoke('hermes:git:review:list', repoPath, scope, baseRef),
      diff: (repoPath, filePath, scope, baseRef, staged) =>
        ipcRenderer.invoke('hermes:git:review:diff', repoPath, filePath, scope, baseRef, staged),
      stage: (repoPath, filePath) => ipcRenderer.invoke('hermes:git:review:stage', repoPath, filePath),
      unstage: (repoPath, filePath) => ipcRenderer.invoke('hermes:git:review:unstage', repoPath, filePath),
      revert: (repoPath, filePath) => ipcRenderer.invoke('hermes:git:review:revert', repoPath, filePath),
      revParse: (repoPath, ref) => ipcRenderer.invoke('hermes:git:review:revParse', repoPath, ref),
      commit: (repoPath, message, push) => ipcRenderer.invoke('hermes:git:review:commit', repoPath, message, push),
      commitContext: repoPath => ipcRenderer.invoke('hermes:git:review:commitContext', repoPath),
      push: repoPath => ipcRenderer.invoke('hermes:git:review:push', repoPath),
      shipInfo: repoPath => ipcRenderer.invoke('hermes:git:review:shipInfo', repoPath),
      createPr: repoPath => ipcRenderer.invoke('hermes:git:review:createPr', repoPath)
    }
  },
  terminal: {
    cwd: id => ipcRenderer.invoke('hermes:terminal:cwd', id),
    dispose: id => ipcRenderer.invoke('hermes:terminal:dispose', id),
    resize: (id, size) => ipcRenderer.invoke('hermes:terminal:resize', id, size),
    start: options => ipcRenderer.invoke('hermes:terminal:start', options),
    write: (id, data) => ipcRenderer.invoke('hermes:terminal:write', id, data),
    onData: (id, callback) => {
      const channel = `hermes:terminal:${id}:data`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    },
    onExit: (id, callback) => {
      const channel = `hermes:terminal:${id}:exit`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    }
  },
  onClosePreviewRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('hermes:close-preview-requested', listener)

    return () => ipcRenderer.removeListener('hermes:close-preview-requested', listener)
  },
  onOpenUpdatesRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('hermes:open-updates', listener)

    return () => ipcRenderer.removeListener('hermes:open-updates', listener)
  },
  onDeepLink: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('hermes:deep-link', listener)

    return () => ipcRenderer.removeListener('hermes:deep-link', listener)
  },
  signalDeepLinkReady: () => ipcRenderer.invoke('hermes:deep-link-ready'),
  onWindowStateChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('hermes:window-state-changed', listener)

    return () => ipcRenderer.removeListener('hermes:window-state-changed', listener)
  },
  onFocusSession: callback => {
    const listener = (_event, sessionId) => callback(sessionId)
    ipcRenderer.on('hermes:focus-session', listener)

    return () => ipcRenderer.removeListener('hermes:focus-session', listener)
  },
  onNotificationAction: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('hermes:notification-action', listener)

    return () => ipcRenderer.removeListener('hermes:notification-action', listener)
  },
  onPreviewFileChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('hermes:preview-file-changed', listener)

    return () => ipcRenderer.removeListener('hermes:preview-file-changed', listener)
  },
  onBackendExit: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('hermes:backend-exit', listener)

    return () => ipcRenderer.removeListener('hermes:backend-exit', listener)
  },
  // Soft gateway-mode apply finished tearing down the primary backend. Renderer
  // should wipe session lists + re-dial without a window reload.
  onConnectionApplied: callback => {
    const listener = () => callback()
    ipcRenderer.on('hermes:connection:applied', listener)

    return () => ipcRenderer.removeListener('hermes:connection:applied', listener)
  },
  onPowerResume: callback => {
    const listener = () => callback()
    ipcRenderer.on('hermes:power-resume', listener)

    return () => ipcRenderer.removeListener('hermes:power-resume', listener)
  },
  onBootProgress: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('hermes:boot-progress', listener)

    return () => ipcRenderer.removeListener('hermes:boot-progress', listener)
  },
  // First-launch bootstrap progress -- emitted by the install.ps1 stage
  // runner in main.ts (apps/desktop/electron/bootstrap-runner.ts).
  // Renderer's install overlay subscribes to live events and queries the
  // current snapshot via getBootstrapState() to recover after a devtools
  // reload mid-bootstrap.
  getBootstrapState: () => ipcRenderer.invoke('hermes:bootstrap:get'),
  resetBootstrap: () => ipcRenderer.invoke('hermes:bootstrap:reset'),
  repairBootstrap: () => ipcRenderer.invoke('hermes:bootstrap:repair'),
  cancelBootstrap: () => ipcRenderer.invoke('hermes:bootstrap:cancel'),
  onBootstrapEvent: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('hermes:bootstrap:event', listener)

    return () => ipcRenderer.removeListener('hermes:bootstrap:event', listener)
  },
  getVersion: () => ipcRenderer.invoke('hermes:version'),
  getRemoteDisplayReason: () => ipcRenderer.invoke('hermes:get-remote-display-reason'),
  localAi: {
    getStatus: () => ipcRenderer.invoke('hermes:local-ai:status'),
    getRecommendation: () => ipcRenderer.invoke('hermes:local-ai:recommendation'),
    setMode: mode => ipcRenderer.invoke('hermes:local-ai:mode', mode),
    setTelemetryEnabled: enabled => ipcRenderer.invoke('hermes:local-ai:telemetry', enabled),
    install: input => ipcRenderer.invoke('hermes:local-ai:install', input),
    cancel: () => ipcRenderer.invoke('hermes:local-ai:cancel'),
    retry: () => ipcRenderer.invoke('hermes:local-ai:retry'),
    verify: () => ipcRenderer.invoke('hermes:local-ai:verify'),
    repair: () => ipcRenderer.invoke('hermes:local-ai:repair'),
    changeModel: modelId => ipcRenderer.invoke('hermes:local-ai:change-model', modelId),
    reinstall: () => ipcRenderer.invoke('hermes:local-ai:reinstall'),
    uninstall: () => ipcRenderer.invoke('hermes:local-ai:uninstall'),
    onProgress: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('hermes:local-ai:progress', listener)

      return () => ipcRenderer.removeListener('hermes:local-ai:progress', listener)
    }
  },
  uninstall: {
    summary: () => ipcRenderer.invoke('hermes:uninstall:summary'),
    run: mode => ipcRenderer.invoke('hermes:uninstall:run', { mode })
  },
  updates: {
    check: () => ipcRenderer.invoke('hermes:updates:check'),
    apply: opts => ipcRenderer.invoke('hermes:updates:apply', opts),
    getBranch: () => ipcRenderer.invoke('hermes:updates:branch:get'),
    setBranch: name => ipcRenderer.invoke('hermes:updates:branch:set', name),
    onProgress: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('hermes:updates:progress', listener)

      return () => ipcRenderer.removeListener('hermes:updates:progress', listener)
    }
  },
  themes: {
    fetchMarketplace: id => ipcRenderer.invoke('hermes:vscode-theme:fetch', id),
    searchMarketplace: query => ipcRenderer.invoke('hermes:vscode-theme:search', query)
  },
  // IX Agency: locally-persisted settings (portal/gateway/VPN profile), the
  // WireGuard VPN control, and the admin-mcp gateway MCP directory. Only the
  // IX Agency brand exposes this namespace (constant-folded out elsewhere).
  ...(IS_IX_AGENCY_BRAND
    ? {
        ixAgency: {
          getSettings: () => ipcRenderer.invoke('hermes:ix-agency:settings:get'),
          saveSettings: payload => ipcRenderer.invoke('hermes:ix-agency:settings:save', payload),
          pickVpnConf: () => ipcRenderer.invoke('hermes:ix-agency:vpn:pick-conf'),
          importVpnConf: () => ipcRenderer.invoke('hermes:ix-agency:vpn:import-conf'),
          vpnStatus: () => ipcRenderer.invoke('hermes:ix-agency:vpn:status'),
          vpnConnect: () => ipcRenderer.invoke('hermes:ix-agency:vpn:connect'),
          vpnDisconnect: () => ipcRenderer.invoke('hermes:ix-agency:vpn:disconnect'),
          // Status lamps (VPN deep check + admin-mcp) and the update poller.
          statusSummary: refresh => ipcRenderer.invoke('hermes:ix-agency:status:summary', { refresh }),
          updateCheck: () => ipcRenderer.invoke('hermes:ix-agency:update:check'),
          updateApply: () => ipcRenderer.invoke('hermes:ix-agency:update:apply'),
          // Cognito S2S + local Hermes first-run init.
          hermesStatus: () => ipcRenderer.invoke('hermes:ix-agency:hermes:status'),
          cognitoValidate: payload => ipcRenderer.invoke('hermes:ix-agency:cognito:validate', payload),
          hermesInit: () => ipcRenderer.invoke('hermes:ix-agency:hermes:init'),
          listMcpTiles: () => ipcRenderer.invoke('hermes:ix-agency:mcp:list'),
          // User-level SKILL.md drafts (~/.hermes/skills/ix-user/) + publish to the
          // portal's global Skills.md API.
          skillsList: () => ipcRenderer.invoke('hermes:ix-agency:skills:list'),
          skillsSave: payload => ipcRenderer.invoke('hermes:ix-agency:skills:save', payload),
          skillsDelete: id => ipcRenderer.invoke('hermes:ix-agency:skills:delete', { id }),
          skillsPublish: id => ipcRenderer.invoke('hermes:ix-agency:skills:publish', { id }),
          // Login enforcement: probes the portal OTP session (main process, using
          // the persist:ix-agency-portal session cookies).
          authStatus: force => ipcRenderer.invoke('hermes:ix-agency:auth:status', { force }),
          // Native OTP login — email a code, verify it; cookies land in the same
          // portal session partition the probe checks. No webview involved.
          authSendOtp: email => ipcRenderer.invoke('hermes:ix-agency:auth:send-otp', { email }),
          authVerifyOtp: payload => ipcRenderer.invoke('hermes:ix-agency:auth:verify-otp', payload),
          // Native chat: LiteLLM + admin-mcp tool loop in the main process. The
          // write gate's Confirm/Cancel travels ONLY over chatConfirm — no other
          // surface (and never the model) can approve a write.
          chatModels: () => ipcRenderer.invoke('hermes:ix-agency:chat:models'),
          chatList: () => ipcRenderer.invoke('hermes:ix-agency:chat:list'),
          chatGet: conversationId => ipcRenderer.invoke('hermes:ix-agency:chat:get', conversationId),
          chatSend: async payload => {
            const governedPayload =
              payload?.inputModality === 'voice'
                ? { ...payload, voiceCaptureToken: await consumeVoiceCaptureAttestation() }
                : payload

            return ipcRenderer.invoke('hermes:ix-agency:chat:send', governedPayload)
          },
          chatConfirm: payload => ipcRenderer.invoke('hermes:ix-agency:chat:confirm', payload),
          onChatEvent: callback => {
            const listener = (_event, payload) => callback(payload)
            ipcRenderer.on('hermes:ix-agency:chat:event', listener)

            return () => ipcRenderer.removeListener('hermes:ix-agency:chat:event', listener)
          },
          // Auto-attach sync: gateway MCP directory + dynamic connectors + org
          // skills, run automatically after login/boot and pushed via syncEvent.
          syncGet: () => ipcRenderer.invoke('hermes:ix-agency:sync:get'),
          syncRun: () => ipcRenderer.invoke('hermes:ix-agency:sync:run'),
          onSyncEvent: callback => {
            const listener = (_event, payload) => callback(payload)
            ipcRenderer.on('hermes:ix-agency:sync:event', listener)

            return () => ipcRenderer.removeListener('hermes:ix-agency:sync:event', listener)
          },
          // Per-MCP-tile health lamps (green/grey/red), cached in the main process.
          mcpHealth: refresh => ipcRenderer.invoke('hermes:ix-agency:mcp:health', { refresh }),
          // Dynamic connectors (super admin): CRUD + probe through the portal's
          // /api/portal/connectors/dynamic routes. Tokens pass through the main
          // process only — never stored or echoed back.
          connectorsList: () => ipcRenderer.invoke('hermes:ix-agency:connectors:list'),
          connectorsSave: payload => ipcRenderer.invoke('hermes:ix-agency:connectors:save', payload),
          connectorsPatch: payload => ipcRenderer.invoke('hermes:ix-agency:connectors:patch', payload),
          connectorsDelete: id => ipcRenderer.invoke('hermes:ix-agency:connectors:delete', { id }),
          connectorsTest: payload => ipcRenderer.invoke('hermes:ix-agency:connectors:test', payload),
          connectorsParseImport: json => ipcRenderer.invoke('hermes:ix-agency:connectors:parse-import', { json }),
          connectorsExport: () => ipcRenderer.invoke('hermes:ix-agency:connectors:export')
        }
      }
    : {}),
  // QuizVerse: DeepTutor platform supervisor (local spawn / hosted remote),
  // locally-persisted settings, and the Play/Arcade surface catalog. Only the
  // QuizVerse brand exposes this namespace (constant-folded out elsewhere).
  ...(IS_QUIZVERSE_BRAND
    ? {
        quizverse: {
          getSettings: () => ipcRenderer.invoke('hermes:quizverse:settings:get'),
          saveSettings: payload => ipcRenderer.invoke('hermes:quizverse:settings:save', payload),
          tutorStatus: () => ipcRenderer.invoke('hermes:quizverse:tutor:status'),
          tutorStart: () => ipcRenderer.invoke('hermes:quizverse:tutor:start'),
          tutorStop: () => ipcRenderer.invoke('hermes:quizverse:tutor:stop'),
          tutorRestart: () => ipcRenderer.invoke('hermes:quizverse:tutor:restart'),
          validateLitellm: payload => ipcRenderer.invoke('hermes:quizverse:litellm:validate', payload),
          tutorRequest: payload => ipcRenderer.invoke('hermes:quizverse:tutor:request', payload),
          tutorStreamStart: path => ipcRenderer.invoke('hermes:quizverse:tutor:stream:start', path),
          tutorStreamStop: id => ipcRenderer.invoke('hermes:quizverse:tutor:stream:stop', id),
          onTutorStreamEvent: callback => {
            const listener = (_event, payload) => callback(payload)
            ipcRenderer.on('hermes:quizverse:tutor:stream:event', listener)

            return () => ipcRenderer.removeListener('hermes:quizverse:tutor:stream:event', listener)
          },
          tutorWsConnect: (path, userId) => ipcRenderer.invoke('hermes:quizverse:tutor:ws:connect', path, userId),
          tutorWsSend: (id, data) => ipcRenderer.invoke('hermes:quizverse:tutor:ws:send', id, data),
          tutorWsClose: id => ipcRenderer.invoke('hermes:quizverse:tutor:ws:close', id),
          onTutorWsEvent: callback => {
            const listener = (_event, payload) => callback(payload)
            ipcRenderer.on('hermes:quizverse:tutor:ws:event', listener)

            return () => ipcRenderer.removeListener('hermes:quizverse:tutor:ws:event', listener)
          },
          // Managed install: venv + pip install deeptutor under userData.
          provisionTutor: () => ipcRenderer.invoke('hermes:quizverse:tutor:provision'),
          onProvisionEvent: callback => {
            const listener = (_event, payload) => callback(payload)
            ipcRenderer.on('hermes:quizverse:provision:event', listener)

            return () => ipcRenderer.removeListener('hermes:quizverse:provision:event', listener)
          },
          pickTutorDirectory: () => ipcRenderer.invoke('hermes:quizverse:tutor:pick-directory'),
          playSession: () => ipcRenderer.invoke('hermes:quizverse:play:session'),
          playRpc: (name, payload) => ipcRenderer.invoke('hermes:quizverse:play:rpc', name, payload),
          playRealtimeConnect: () => ipcRenderer.invoke('hermes:quizverse:play:realtime:connect'),
          playRealtimeListMatches: (id, query) =>
            ipcRenderer.invoke('hermes:quizverse:play:realtime:list-matches', id, query),
          playRealtimeJoinMatch: (id, matchId) =>
            ipcRenderer.invoke('hermes:quizverse:play:realtime:join-match', id, matchId),
          playRealtimeCreateMatch: (id, payload) =>
            ipcRenderer.invoke('hermes:quizverse:play:realtime:create-match', id, payload),
          playRealtimeSend: (id, opCode, payload) =>
            ipcRenderer.invoke('hermes:quizverse:play:realtime:send', id, opCode, payload),
          playRealtimeClose: id => ipcRenderer.invoke('hermes:quizverse:play:realtime:close', id),
          authStart: () => ipcRenderer.invoke('hermes:quizverse:auth:start'),
          authStatus: () => ipcRenderer.invoke('hermes:quizverse:auth:status'),
          productRequest: input => ipcRenderer.invoke('hermes:quizverse:product:request', input),
          productStream: (input, onChunk) => {
            const streamId = input.streamId ?? crypto.randomUUID()

            const listener = (_event, payload) => {
              if (payload?.streamId === streamId && typeof payload.chunk === 'string') {
                onChunk(payload.chunk)
              }
            }

            ipcRenderer.on('hermes:quizverse:product:chunk', listener)

            return ipcRenderer
              .invoke('hermes:quizverse:product:request', { ...input, streamId })
              .finally(() => ipcRenderer.removeListener('hermes:quizverse:product:chunk', listener))
          },
          productCancel: streamId => ipcRenderer.invoke('hermes:quizverse:product:cancel', streamId),
          mcpStatus: () => ipcRenderer.invoke('hermes:quizverse:mcp:status'),
          onPlayRealtimeEvent: callback => {
            const listener = (_event, payload) => callback(payload)
            ipcRenderer.on('hermes:quizverse:play:realtime:event', listener)

            return () => ipcRenderer.removeListener('hermes:quizverse:play:realtime:event', listener)
          },
          onAuthEvent: callback => {
            const listener = (_event, payload) => callback(payload)
            ipcRenderer.on('hermes:quizverse:auth:event', listener)

            return () => ipcRenderer.removeListener('hermes:quizverse:auth:event', listener)
          },
          // Update lamp for the status strip (same poller as the tray).
          updateCheck: () => ipcRenderer.invoke('hermes:quizverse:update:check'),
          updateApply: () => ipcRenderer.invoke('hermes:quizverse:update:apply'),
          onTutorEvent: callback => {
            const listener = (_event, payload) => callback(payload)
            ipcRenderer.on('hermes:quizverse:tutor:event', listener)

            return () => ipcRenderer.removeListener('hermes:quizverse:tutor:event', listener)
          }
        }
      }
    : {}),
  // Foundrly: product workspace (MVP). Only the Foundrly brand exposes this.
  ...(IS_FOUNDRLY_BRAND
    ? {
        foundrly: {
          status: () => ipcRenderer.invoke('hermes:foundrly:status'),
          openUrl: url => ipcRenderer.invoke('hermes:foundrly:open-url', url)
        }
      }
    : {})
})
