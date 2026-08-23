import { contextBridge, ipcRenderer, webFrame, webUtils } from 'electron'

// Which translucency the OS can back. Asked synchronously because the renderer
// needs it before its first paint, and answered by main because deciding it
// needs `os.release()` — a sandboxed preload may only require electron, events,
// timers and url, so importing node:os here throws before contextBridge runs
// and takes the ENTIRE bridge down with it (window.orionDesktop undefined =>
// "Desktop IPC bridge is unavailable"). No reply means no glass, which degrades
// to an ordinary opaque window rather than a page thinned over nothing.
const translucencySupport = ipcRenderer.sendSync('orion:translucency:support')

contextBridge.exposeInMainWorld('orionDesktop', {
  glassSupported: translucencySupport?.glass === true,
  translucencySupported: translucencySupport?.translucency === true,
  getConnection: profile => ipcRenderer.invoke('orion:connection', profile),
  // Registry-scoped backend resolution: { connectionId, profile } → descriptor.
  getConnectionFor: payload => ipcRenderer.invoke('orion:connection:for', payload),
  getProfileRoutes: profiles => ipcRenderer.invoke('orion:plugin-profile-routes', profiles),
  revalidateConnection: () => ipcRenderer.invoke('orion:connection:revalidate'),
  touchBackend: profile => ipcRenderer.invoke('orion:backend:touch', profile),
  getGatewayWsUrl: profile => ipcRenderer.invoke('orion:gateway:ws-url', profile),
  // Registry-scoped fresh WS URL: { connectionId, profile } → result shape of
  // getGatewayWsUrl, minted against that connection's backend.
  getGatewayWsUrlFor: payload => ipcRenderer.invoke('orion:gateway:ws-url-for', payload),
  // Union agent roster across every registered connection.
  getAgentRoster: () => ipcRenderer.invoke('orion:agents:roster'),
  openSessionWindow: (sessionId, opts) => ipcRenderer.invoke('orion:window:openSession', sessionId, opts),
  openSessionInTerminal: (sessionId, opts) => ipcRenderer.invoke('orion:window:openInTerminal', sessionId, opts),
  openWindow: () => ipcRenderer.invoke('orion:window:openInstance'),
  claimAmbientCue: key => ipcRenderer.invoke('orion:ambient:claim', key),
  wakeIndicator: {
    getState: () => ipcRenderer.invoke('orion:wake-indicator:get'),
    setState: state => ipcRenderer.send('orion:wake-indicator:set', state),
    onState: callback => {
      const listener = (_event, state) => callback(state)
      ipcRenderer.on('orion:wake-indicator:state', listener)

      return () => ipcRenderer.removeListener('orion:wake-indicator:state', listener)
    }
  },
  petOverlay: {
    // Main renderer → main process: window lifecycle + drag. `request` is
    // `{ bounds, screen }`; resolves with the screen bounds it actually used.
    open: request => ipcRenderer.invoke('orion:pet-overlay:open', request),
    close: () => ipcRenderer.invoke('orion:pet-overlay:close'),
    setBounds: bounds => ipcRenderer.send('orion:pet-overlay:set-bounds', bounds),
    setIgnoreMouse: ignore => ipcRenderer.send('orion:pet-overlay:ignore-mouse', ignore),
    // Flip the overlay focusable (and focus it) while the composer needs keys.
    setFocusable: focusable => ipcRenderer.send('orion:pet-overlay:set-focusable', focusable),
    // Main renderer → overlay (forwarded by main): push the latest pet state.
    pushState: payload => ipcRenderer.send('orion:pet-overlay:state', payload),
    // Overlay → main renderer (forwarded by main): pop back in / composer submit.
    control: payload => ipcRenderer.send('orion:pet-overlay:control', payload),
    // Overlay subscribes to state pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('orion:pet-overlay:state', listener)

      return () => ipcRenderer.removeListener('orion:pet-overlay:state', listener)
    },
    // Main renderer subscribes to overlay control messages.
    onControl: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('orion:pet-overlay:control', listener)

      return () => ipcRenderer.removeListener('orion:pet-overlay:control', listener)
    }
  },
  // HUD mode: the chrome-free floating chat. A full app renderer (own gateway)
  // sized as a floating bar, so it mounts the real composer. Main owns the
  // window; `onChanged` keeps every window's toggle truthful.
  hud: {
    open: request => ipcRenderer.invoke('orion:hud:open', request),
    close: () => ipcRenderer.invoke('orion:hud:close'),
    setIgnoreMouse: ignore => ipcRenderer.send('orion:hud:ignore-mouse', ignore),
    moveBy: delta => ipcRenderer.send('orion:hud:move-by', delta),
    setBounds: bounds => ipcRenderer.send('orion:hud:set-bounds', bounds),
    // Whether the band covers the window below the bar. Main pairs it with the
    // user's translucency setting to decide the native frost (macOS vibrancy /
    // Windows 11 DWM backdrop) — see hudFrostFor.
    setFrost: showing => ipcRenderer.invoke('orion:hud:frost', showing),
    // The HUD tells main which session it is on; main hands that back to the
    // app window when the HUD closes, so the app can re-home onto it.
    setSession: sessionId => ipcRenderer.send('orion:hud:session', sessionId),
    onGoto: callback => {
      const listener = (_event, sessionId) => callback(sessionId)
      ipcRenderer.on('orion:hud:goto', listener)

      return () => ipcRenderer.removeListener('orion:hud:goto', listener)
    },
    onChanged: callback => {
      const listener = (_event, state) => callback(state)
      ipcRenderer.on('orion:hud:changed', listener)

      return () => ipcRenderer.removeListener('orion:hud:changed', listener)
    },
    // Linux only, and silent elsewhere: where the cursor is, in page
    // coordinates, or null when it has left the window. Stands in for the
    // mousemove that `setIgnoreMouseEvents(true, { forward: true })` delivers on
    // macOS and Windows but not here.
    onCursor: callback => {
      const listener = (_event, point) => callback(point)
      ipcRenderer.on('orion:hud:cursor', listener)

      return () => ipcRenderer.removeListener('orion:hud:cursor', listener)
    }
  },
  // Quick Entry: the global-hotkey mini composer window. Main owns the OS
  // shortcut + the persisted preference; the quick window only captures text
  // and hands it back, and the primary renderer submits it through the normal
  // prompt path.
  quickEntry: {
    getSettings: () => ipcRenderer.invoke('orion:quick-entry:settings:get'),
    setSettings: patch => ipcRenderer.invoke('orion:quick-entry:settings:set', patch),
    submit: payload => ipcRenderer.send('orion:quick-entry:submit', payload),
    dismiss: () => ipcRenderer.send('orion:quick-entry:dismiss'),
    // Primary renderer → main → quick window: gateway connection state + the
    // recent-session options the target picker offers. Main caches the latest
    // payload so a freshly spawned quick window starts from truth.
    pushState: payload => ipcRenderer.send('orion:quick-entry:state', payload),
    // Quick window subscribes to those pushes.
    onState: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('orion:quick-entry:state', listener)

      return () => ipcRenderer.removeListener('orion:quick-entry:state', listener)
    },
    // Main → primary renderer: a submit captured by the quick window.
    onSubmit: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('orion:quick-entry:submit', listener)

      return () => ipcRenderer.removeListener('orion:quick-entry:submit', listener)
    },
    // Main → quick window: you were just summoned (reset draft + refocus).
    onShown: callback => {
      const listener = () => callback()
      ipcRenderer.on('orion:quick-entry:shown', listener)

      return () => ipcRenderer.removeListener('orion:quick-entry:shown', listener)
    }
  },
  getBootProgress: () => ipcRenderer.invoke('orion:boot-progress:get'),
  getConnectionConfig: profile => ipcRenderer.invoke('orion:connection-config:get', profile),
  saveConnectionConfig: payload => ipcRenderer.invoke('orion:connection-config:save', payload),
  applyConnectionConfig: payload => ipcRenderer.invoke('orion:connection-config:apply', payload),
  testConnectionConfig: payload => ipcRenderer.invoke('orion:connection-config:test', payload),
  // v2 multi-connection registry: named agent sources (local / remote / cloud / ssh).
  connections: {
    list: () => ipcRenderer.invoke('orion:connections:list'),
    save: payload => ipcRenderer.invoke('orion:connections:save', payload),
    remove: id => ipcRenderer.invoke('orion:connections:remove', id),
    setPrimary: id => ipcRenderer.invoke('orion:connections:set-primary', id),
    setLaunchMode: mode => ipcRenderer.invoke('orion:connections:set-launch-mode', mode),
    setLastUsed: id => ipcRenderer.invoke('orion:connections:set-last-used', id),
    test: id => ipcRenderer.invoke('orion:connections:test', id),
    // Fan out `orion update` to every eligible registered connection.
    // Optional excludeIds skips rows the caller updates through another path.
    updateAll: options => ipcRenderer.invoke('orion:connections:update-all', options),
    // Registry lifecycle push (main → renderer): a connection was removed or
    // materially edited, so secondaries scoped to it must be disposed (and,
    // for edits, re-dialed at the new target).
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('orion:connections:changed', listener)

      return () => ipcRenderer.removeListener('orion:connections:changed', listener)
    }
  },
  sshConfigHosts: () => ipcRenderer.invoke('orion:ssh-config:hosts'),
  sshResolveHost: host => ipcRenderer.invoke('orion:ssh-config:resolve', host),
  probeConnectionConfig: remoteUrl => ipcRenderer.invoke('orion:connection-config:probe', remoteUrl),
  oauthLoginConnectionConfig: remoteUrl => ipcRenderer.invoke('orion:connection-config:oauth-login', remoteUrl),
  oauthLogoutConnectionConfig: remoteUrl => ipcRenderer.invoke('orion:connection-config:oauth-logout', remoteUrl),
  // Orion Cloud: one portal login powers discovery + silent per-agent sign-in
  // (cloud-auto-discovery Phase 3).
  cloud: {
    status: () => ipcRenderer.invoke('orion:cloud:status'),
    login: () => ipcRenderer.invoke('orion:cloud:login'),
    logout: () => ipcRenderer.invoke('orion:cloud:logout'),
    discover: org => ipcRenderer.invoke('orion:cloud:discover', org),
    agentSignIn: dashboardUrl => ipcRenderer.invoke('orion:cloud:agent-sign-in', dashboardUrl)
  },
  profile: {
    get: () => ipcRenderer.invoke('orion:profile:get'),
    set: name => ipcRenderer.invoke('orion:profile:set', name)
  },
  api: request => ipcRenderer.invoke('orion:api', request),
  notify: payload => ipcRenderer.invoke('orion:notify', payload),
  requestMicrophoneAccess: () => ipcRenderer.invoke('orion:requestMicrophoneAccess'),
  readWindowBelow: () => ipcRenderer.invoke('orion:window:readBelow'),
  readFileDataUrl: filePath => ipcRenderer.invoke('orion:readFileDataUrl', filePath),
  readFileDataUrlForAttach: filePath => ipcRenderer.invoke('orion:readFileDataUrlForAttach', filePath),
  dataUrlReadMax: {
    get: () => ipcRenderer.invoke('orion:data-url-read-max:get'),
    set: maxMb => ipcRenderer.invoke('orion:data-url-read-max:set', maxMb)
  },
  readFileText: filePath => ipcRenderer.invoke('orion:readFileText', filePath),
  readPluginSource: (filePath: string) => ipcRenderer.invoke('orion:readPluginSource', filePath),
  selectPaths: options => ipcRenderer.invoke('orion:selectPaths', options),
  selectSavePath: options => ipcRenderer.invoke('orion:selectSavePath', options),
  writeClipboard: text => ipcRenderer.invoke('orion:writeClipboard', text),
  readClipboard: () => ipcRenderer.invoke('orion:readClipboard'),
  saveGatewayFile: payload => ipcRenderer.invoke('orion:saveGatewayFile', payload),
  saveImageFromUrl: url => ipcRenderer.invoke('orion:saveImageFromUrl', url),
  contextMenuEdit: command => ipcRenderer.invoke('orion:context-menu:edit', command),
  contextMenuCopyImage: () => ipcRenderer.invoke('orion:context-menu:copy-image'),
  contextMenuSpellcheck: action => ipcRenderer.invoke('orion:context-menu:spellcheck', action),
  contextMenuGuestAddWord: payload => ipcRenderer.invoke('orion:context-menu:guest-add-word', payload),
  onContextMenuSpellcheck: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('orion:context-menu-spellcheck', listener)

    return () => ipcRenderer.removeListener('orion:context-menu-spellcheck', listener)
  },
  saveImageBuffer: (data, ext) => ipcRenderer.invoke('orion:saveImageBuffer', { data, ext }),
  saveClipboardImage: () => ipcRenderer.invoke('orion:saveClipboardImage'),
  getPathForFile: file => {
    try {
      return webUtils.getPathForFile(file) || ''
    } catch {
      return ''
    }
  },
  normalizePreviewTarget: (target, baseDir) => ipcRenderer.invoke('orion:normalizePreviewTarget', target, baseDir),
  watchPreviewFile: url => ipcRenderer.invoke('orion:watchPreviewFile', url),
  watchDirectory: dir => ipcRenderer.invoke('orion:watchDirectory', dir),
  stopPreviewFileWatch: id => ipcRenderer.invoke('orion:stopPreviewFileWatch', id),
  setActiveWork: payload => ipcRenderer.send('orion:active-work', payload),
  setTitleBarTheme: payload => ipcRenderer.send('orion:titlebar-theme', payload),
  setNativeTheme: mode => ipcRenderer.send('orion:native-theme', mode),
  setTranslucency: payload => ipcRenderer.send('orion:translucency', payload),
  setKeepAwake: on => ipcRenderer.send('orion:keep-awake', on),
  setDisableF12: blocked => ipcRenderer.send('orion:devtools:disable-f12', blocked),
  setPreviewShortcutActive: active => ipcRenderer.send('orion:previewShortcutActive', Boolean(active)),
  openExternal: url => ipcRenderer.invoke('orion:openExternal', url),
  openPreviewInBrowser: url => ipcRenderer.invoke('orion:openPreviewInBrowser', url),
  reachPreviewUrl: url => ipcRenderer.invoke('orion:preview:reach', url),
  fetchLinkTitle: url => ipcRenderer.invoke('orion:fetchLinkTitle', url),
  resolveFavicon: url => ipcRenderer.invoke('orion:resolveFavicon', url),
  sanitizeWorkspaceCwd: cwd => ipcRenderer.invoke('orion:workspace:sanitize', cwd),
  settings: {
    getDefaultProjectDir: () => ipcRenderer.invoke('orion:setting:defaultProjectDir:get'),
    setDefaultProjectDir: dir => ipcRenderer.invoke('orion:setting:defaultProjectDir:set', dir),
    pickDefaultProjectDir: () => ipcRenderer.invoke('orion:setting:defaultProjectDir:pick')
  },
  zoom: {
    // Current zoom of this window, as { level, percent }.
    get: () => ipcRenderer.invoke('orion:zoom:get'),
    // Synchronous zoom factor (1 = 100%). Coordinate math needs it in the
    // same tick as the event it converts, so no IPC round-trip here.
    factor: () => webFrame.getZoomFactor(),
    setPercent: percent => ipcRenderer.send('orion:zoom:set-percent', percent),
    // Fires on every zoom change, including the Ctrl/Cmd +/-/0 shortcuts,
    // so the settings UI can stay in sync with the keyboard.
    onChanged: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('orion:zoom:changed', listener)

      return () => ipcRenderer.removeListener('orion:zoom:changed', listener)
    }
  },
  revealLogs: () => ipcRenderer.invoke('orion:logs:reveal'),
  getRecentLogs: () => ipcRenderer.invoke('orion:logs:recent'),
  // Fire-and-forget: persists a renderer error-boundary catch (with component
  // stack) to desktop.log so crashes survive the window (#79428).
  reportRendererError: report => ipcRenderer.send('orion:logs:renderer-error', report),
  readDir: dirPath => ipcRenderer.invoke('orion:fs:readDir', dirPath),
  gitRoot: startPath => ipcRenderer.invoke('orion:fs:gitRoot', startPath),
  revealPath: targetPath => ipcRenderer.invoke('orion:fs:reveal', targetPath),
  openDir: dirPath => ipcRenderer.invoke('orion:fs:openDir', dirPath),
  desktopPluginsRoot: () => ipcRenderer.invoke('orion:fs:desktopPluginsRoot'),
  logsRoot: () => ipcRenderer.invoke('orion:fs:logsRoot'),
  agentPluginsRoot: () => ipcRenderer.invoke('orion:fs:agentPluginsRoot'),
  renamePath: (targetPath, newName) => ipcRenderer.invoke('orion:fs:rename', targetPath, newName),
  writeTextFile: (filePath, content) => ipcRenderer.invoke('orion:fs:writeText', filePath, content),
  trashPath: targetPath => ipcRenderer.invoke('orion:fs:trash', targetPath),
  git: {
    worktreeList: repoPath => ipcRenderer.invoke('orion:git:worktreeList', repoPath),
    worktreeAdd: (repoPath, options) => ipcRenderer.invoke('orion:git:worktreeAdd', repoPath, options),
    worktreeRemove: (repoPath, worktreePath, options) =>
      ipcRenderer.invoke('orion:git:worktreeRemove', repoPath, worktreePath, options),
    branchSwitch: (repoPath, branch) => ipcRenderer.invoke('orion:git:branchSwitch', repoPath, branch),
    branchList: repoPath => ipcRenderer.invoke('orion:git:branchList', repoPath),
    baseBranchList: repoPath => ipcRenderer.invoke('orion:git:baseBranchList', repoPath),
    repoStatus: repoPath => ipcRenderer.invoke('orion:git:repoStatus', repoPath),
    fileDiff: (repoPath, filePath) => ipcRenderer.invoke('orion:git:fileDiff', repoPath, filePath),
    scanRepos: (roots, options) => ipcRenderer.invoke('orion:git:scanRepos', roots, options),
    review: {
      list: (repoPath, scope, baseRef) => ipcRenderer.invoke('orion:git:review:list', repoPath, scope, baseRef),
      diff: (repoPath, filePath, scope, baseRef, staged) =>
        ipcRenderer.invoke('orion:git:review:diff', repoPath, filePath, scope, baseRef, staged),
      stage: (repoPath, filePath) => ipcRenderer.invoke('orion:git:review:stage', repoPath, filePath),
      unstage: (repoPath, filePath) => ipcRenderer.invoke('orion:git:review:unstage', repoPath, filePath),
      revert: (repoPath, filePath) => ipcRenderer.invoke('orion:git:review:revert', repoPath, filePath),
      revParse: (repoPath, ref) => ipcRenderer.invoke('orion:git:review:revParse', repoPath, ref),
      commit: (repoPath, message, push) => ipcRenderer.invoke('orion:git:review:commit', repoPath, message, push),
      commitContext: repoPath => ipcRenderer.invoke('orion:git:review:commitContext', repoPath),
      push: repoPath => ipcRenderer.invoke('orion:git:review:push', repoPath),
      shipInfo: repoPath => ipcRenderer.invoke('orion:git:review:shipInfo', repoPath),
      prList: (repoPath, branches, numbers) =>
        ipcRenderer.invoke('orion:git:review:prList', repoPath, branches, numbers),
      fetchPrComment: (repoPath, url) => ipcRenderer.invoke('orion:git:review:fetchPrComment', repoPath, url),
      createPr: repoPath => ipcRenderer.invoke('orion:git:review:createPr', repoPath)
    }
  },
  terminal: {
    cwd: id => ipcRenderer.invoke('orion:terminal:cwd', id),
    dispose: id => ipcRenderer.invoke('orion:terminal:dispose', id),
    resize: (id, size) => ipcRenderer.invoke('orion:terminal:resize', id, size),
    start: options => ipcRenderer.invoke('orion:terminal:start', options),
    write: (id, data) => ipcRenderer.invoke('orion:terminal:write', id, data),
    onData: (id, callback) => {
      const channel = `orion:terminal:${id}:data`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    },
    onExit: (id, callback) => {
      const channel = `orion:terminal:${id}:exit`
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on(channel, listener)

      return () => ipcRenderer.removeListener(channel, listener)
    }
  },
  onClosePreviewRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('orion:close-preview-requested', listener)

    return () => ipcRenderer.removeListener('orion:close-preview-requested', listener)
  },
  onPreviewNav: callback => {
    const listener = (_event, command) => callback(command)
    ipcRenderer.on('orion:preview-nav', listener)

    return () => ipcRenderer.removeListener('orion:preview-nav', listener)
  },
  onOpenFolderRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('orion:open-folder-requested', listener)

    return () => ipcRenderer.removeListener('orion:open-folder-requested', listener)
  },
  onOpenUpdatesRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('orion:open-updates', listener)

    return () => ipcRenderer.removeListener('orion:open-updates', listener)
  },
  onDeepLink: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('orion:deep-link', listener)

    return () => ipcRenderer.removeListener('orion:deep-link', listener)
  },
  signalDeepLinkReady: () => ipcRenderer.invoke('orion:deep-link-ready'),
  probePluginRepo: payload => ipcRenderer.invoke('orion:plugin:probe', payload),
  installDesktopPlugin: payload => ipcRenderer.invoke('orion:plugin:installDesktop', payload),
  onWindowStateChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('orion:window-state-changed', listener)

    return () => ipcRenderer.removeListener('orion:window-state-changed', listener)
  },
  onFocusSession: callback => {
    const listener = (_event, sessionId) => callback(sessionId)
    ipcRenderer.on('orion:focus-session', listener)

    return () => ipcRenderer.removeListener('orion:focus-session', listener)
  },
  onNotificationAction: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('orion:notification-action', listener)

    return () => ipcRenderer.removeListener('orion:notification-action', listener)
  },
  onNotificationActivate: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('orion:notification-activate', listener)

    return () => ipcRenderer.removeListener('orion:notification-activate', listener)
  },
  onPreviewFileChanged: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('orion:preview-file-changed', listener)

    return () => ipcRenderer.removeListener('orion:preview-file-changed', listener)
  },
  onBackendExit: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('orion:backend-exit', listener)

    return () => ipcRenderer.removeListener('orion:backend-exit', listener)
  },
  // Soft gateway-mode apply finished tearing down the primary backend. Renderer
  // should wipe session lists + re-dial without a window reload.
  onConnectionApplied: callback => {
    const listener = () => callback()
    ipcRenderer.on('orion:connection:applied', listener)

    return () => ipcRenderer.removeListener('orion:connection:applied', listener)
  },
  onPowerResume: callback => {
    const listener = () => callback()
    ipcRenderer.on('orion:power-resume', listener)

    return () => ipcRenderer.removeListener('orion:power-resume', listener)
  },
  // AC ↔ battery transitions; renderers slow their backstop polls on battery.
  getOnBattery: () => ipcRenderer.invoke('orion:power-battery:get'),
  onBatteryChanged: callback => {
    const listener = (_event, onBattery) => callback(Boolean(onBattery))
    ipcRenderer.on('orion:power-battery', listener)

    return () => ipcRenderer.removeListener('orion:power-battery', listener)
  },
  onBootProgress: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('orion:boot-progress', listener)

    return () => ipcRenderer.removeListener('orion:boot-progress', listener)
  },
  // First-launch bootstrap progress -- emitted by the install.ps1 stage
  // runner in main.ts (apps/desktop/electron/bootstrap-runner.ts).
  // Renderer's install overlay subscribes to live events and queries the
  // current snapshot via getBootstrapState() to recover after a devtools
  // reload mid-bootstrap.
  getBootstrapState: () => ipcRenderer.invoke('orion:bootstrap:get'),
  continueBootstrapLocal: () => ipcRenderer.invoke('orion:bootstrap:continue-local'),
  resetBootstrap: () => ipcRenderer.invoke('orion:bootstrap:reset'),
  repairBootstrap: () => ipcRenderer.invoke('orion:bootstrap:repair'),
  cancelBootstrap: () => ipcRenderer.invoke('orion:bootstrap:cancel'),
  onBootstrapEvent: callback => {
    const listener = (_event, payload) => callback(payload)
    ipcRenderer.on('orion:bootstrap:event', listener)

    return () => ipcRenderer.removeListener('orion:bootstrap:event', listener)
  },
  getVersion: () => ipcRenderer.invoke('orion:version'),
  getRemoteDisplayReason: () => ipcRenderer.invoke('orion:get-remote-display-reason'),
  uninstall: {
    summary: () => ipcRenderer.invoke('orion:uninstall:summary'),
    run: mode => ipcRenderer.invoke('orion:uninstall:run', { mode })
  },
  updates: {
    check: () => ipcRenderer.invoke('orion:updates:check'),
    apply: opts => ipcRenderer.invoke('orion:updates:apply', opts),
    getBranch: () => ipcRenderer.invoke('orion:updates:branch:get'),
    setBranch: name => ipcRenderer.invoke('orion:updates:branch:set', name),
    onProgress: callback => {
      const listener = (_event, payload) => callback(payload)
      ipcRenderer.on('orion:updates:progress', listener)

      return () => ipcRenderer.removeListener('orion:updates:progress', listener)
    }
  },
  themes: {
    fetchMarketplace: id => ipcRenderer.invoke('orion:vscode-theme:fetch', id),
    searchMarketplace: query => ipcRenderer.invoke('orion:vscode-theme:search', query)
  },
  // Find-in-page (Ctrl/Cmd+F): delegates to Electron's
  // webContents.findInPage on the IPC sender's window so a Cmd+F pressed
  // in a secondary session window searches THAT window, not the primary.
  // `onFoundInPage` returns the unsubscribe fn; the renderer wires it via
  // `initFindInPageListener` in store/find-in-page.ts and tears it down
  // when the FindBar unmounts.
  findInPage: (query, options) => ipcRenderer.invoke('orion:find-in-page', query, options),
  stopFindInPage: () => ipcRenderer.invoke('orion:stop-find-in-page'),
  onFoundInPage: callback => {
    const listener = (_event, result) => callback(result)
    ipcRenderer.on('orion:found-in-page', listener)

    return () => ipcRenderer.removeListener('orion:found-in-page', listener)
  },
  // Main-process `before-input-event` forwards Ctrl/Cmd+F here so renderer
  // can open the FindBar even when the GTK compositor has already grabbed
  // the chord at the windowing layer (#81727).
  onOpenFindBarRequested: callback => {
    const listener = () => callback()
    ipcRenderer.on('orion:open-find-bar', listener)

    return () => ipcRenderer.removeListener('orion:open-find-bar', listener)
  }
})
