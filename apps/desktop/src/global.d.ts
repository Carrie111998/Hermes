import type { GatewayWsUrlResult } from '@hermes/shared'

import type { LocalAiRendererBridge } from './store/local-ai'

import type {
  PetOverlayBounds,
  PetOverlayControl,
  PetOverlayOpenRequest,
  PetOverlayStatePayload
} from './store/pet-overlay'

export {}

declare global {
  interface Window {
    hermesDesktop: {
      // Resolve a backend connection. Omit `profile` (or pass the primary) for
      // the window's backend; pass a named profile to lazily spawn/reuse that
      // profile's backend from the pool.
      getConnection: (profile?: string | null) => Promise<HermesConnection>
      voice?: {
        consumeCaptureAttestation: () => Promise<string>
      }
      // Reconnect-after-wake recovery: liveness-probe the cached PRIMARY backend
      // and drop it if a remote one has gone unreachable, so the next
      // getConnection() rebuilds a reachable descriptor instead of the renderer
      // re-dialing a dead remote forever. No-op for local backends (they
      // self-heal via the child 'exit' handler). `rebuilt` is true when a stale
      // remote cache was dropped.
      revalidateConnection: () => Promise<{ ok: boolean; rebuilt: boolean }>
      // Keepalive: mark a pool profile backend as recently used so the idle
      // reaper spares it while its chat is active.
      touchBackend: (profile?: string | null) => Promise<{ ok: boolean }>
      getGatewayWsUrl: (profile?: null | string) => Promise<GatewayWsUrlResult>
      // Open (or focus) a standalone OS window for a single chat session so
      // the user can work with multiple chats side by side. Returns ok:false
      // with an error code when the sessionId is empty/invalid. `watch` opens
      // a spectator window (lazy resume — no agent build) for live-streaming
      // a running subagent's session.
      openSessionWindow: (sessionId: string, opts?: { watch?: boolean }) => Promise<{ ok: boolean; error?: string }>
      // Open a new full-chrome app window — a peer instance of the primary that
      // renders the complete app against the shared backend, so the user can run
      // multiple GUI windows at once.
      openWindow: () => Promise<{ ok: boolean; error?: string }>
      // Claim a one-shot cross-window ambient cue (turn-end sound / spoken
      // reply). Resolves true for the first window to claim a key, false for
      // peers — so N open windows don't all fire the same cue.
      claimAmbientCue: (key: string) => Promise<boolean>
      // The pop-out pet overlay: a transparent always-on-top window hosting only
      // the mascot. The main renderer drives it (open/close/drag + state push);
      // the overlay sends control messages back (pop-in, composer submit).
      petOverlay: {
        open: (request: PetOverlayOpenRequest) => Promise<{ ok: boolean; bounds?: PetOverlayBounds }>
        close: () => Promise<{ ok: boolean }>
        setBounds: (bounds: PetOverlayBounds) => void
        setIgnoreMouse: (ignore: boolean) => void
        setFocusable: (focusable: boolean) => void
        pushState: (payload: PetOverlayStatePayload) => void
        control: (payload: PetOverlayControl) => void
        onState: (callback: (payload: PetOverlayStatePayload) => void) => () => void
        onControl: (callback: (payload: PetOverlayControl) => void) => () => void
      }
      getBootProgress: () => Promise<DesktopBootProgress>
      getConnectionConfig: (profile?: null | string) => Promise<DesktopConnectionConfig>
      saveConnectionConfig: (payload: DesktopConnectionConfigInput) => Promise<DesktopConnectionConfig>
      applyConnectionConfig: (payload: DesktopConnectionConfigInput) => Promise<DesktopConnectionConfig>
      testConnectionConfig: (payload: DesktopConnectionConfigInput) => Promise<DesktopConnectionTestResult>
      sshConfigHosts: () => Promise<DesktopSshHostsResult>
      sshResolveHost: (host: string) => Promise<DesktopSshResolveResult>
      probeConnectionConfig: (remoteUrl: string) => Promise<DesktopConnectionProbeResult>
      oauthLoginConnectionConfig: (remoteUrl: string) => Promise<DesktopOauthLoginResult>
      oauthLogoutConnectionConfig: (remoteUrl?: string) => Promise<DesktopOauthLogoutResult>
      // Hermes Cloud: one portal login powers discovery + silent per-agent
      // sign-in (cloud-auto-discovery Phase 3).
      cloud: {
        status: () => Promise<DesktopCloudStatus>
        login: () => Promise<DesktopCloudStatus & { ok: boolean }>
        logout: () => Promise<DesktopCloudStatus & { ok: boolean }>
        discover: (org?: string) => Promise<DesktopCloudDiscoverResult>
        agentSignIn: (dashboardUrl: string) => Promise<DesktopCloudAgentSignInResult>
      }
      profile: {
        get: () => Promise<DesktopActiveProfile>
        // Persists the desktop's profile choice and relaunches the local
        // backend under the new HERMES_HOME (reloads the window). Pass null to
        // clear the preference.
        set: (name: string | null) => Promise<DesktopActiveProfile>
      }
      api: <T>(request: HermesApiRequest) => Promise<T>
      notify: (payload: HermesNotification) => Promise<boolean>
      requestMicrophoneAccess: () => Promise<boolean>
      readFileDataUrl: (filePath: string) => Promise<string>
      readFileText: (filePath: string) => Promise<HermesReadFileTextResult>
      selectPaths: (options?: HermesSelectPathsOptions) => Promise<string[]>
      writeClipboard: (text: string) => Promise<boolean>
      saveImageFromUrl: (url: string) => Promise<boolean>
      saveImageBuffer: (data: ArrayBuffer | Uint8Array, ext: string) => Promise<string>
      saveClipboardImage: () => Promise<string>
      getPathForFile: (file: File) => string
      normalizePreviewTarget: (target: string, baseDir?: string) => Promise<HermesPreviewTarget | null>
      watchPreviewFile: (url: string) => Promise<HermesPreviewWatch>
      stopPreviewFileWatch: (id: string) => Promise<boolean>
      setTitleBarTheme?: (payload: HermesTitleBarTheme) => void
      setNativeTheme?: (mode: 'dark' | 'light' | 'system') => void
      setTranslucency?: (payload: { intensity: number }) => void
      setKeepAwake?: (on: boolean) => void
      setPreviewShortcutActive?: (active: boolean) => void
      openExternal: (url: string) => Promise<void>
      openPreviewInBrowser?: (url: string) => Promise<void>
      fetchLinkTitle: (url: string) => Promise<string>
      sanitizeWorkspaceCwd: (cwd?: null | string) => Promise<{ cwd: string; sanitized: boolean }>
      settings: {
        getDefaultProjectDir: () => Promise<{ defaultLabel: string; dir: null | string; resolvedCwd: string }>
        pickDefaultProjectDir: () => Promise<{ canceled: boolean; dir: null | string }>
        setDefaultProjectDir: (dir: null | string) => Promise<{ dir: null | string }>
      }
      studio: {
        status: () => Promise<DesktopStudioStatus>
        chooseExternal: () => Promise<{ canceled: boolean; status: DesktopStudioStatus }>
        consentInstall: (version: string) => Promise<{ consented: true; version: string }>
        launch: (input: { workspacePath: string; sessionId: string; windowId: string }) => Promise<DesktopStudioStatus>
        focus: () => Promise<DesktopStudioStatus>
        stop: () => Promise<DesktopStudioStatus>
      }
      zoom?: {
        get: () => Promise<{ level: number; percent: number }>
        setPercent: (percent: number) => void
        onChanged: (callback: (payload: { level: number; percent: number }) => void) => () => void
      }
      revealLogs: () => Promise<{ ok: boolean; path: string; error?: string }>
      getRecentLogs: () => Promise<{ path: string; lines: string[] }>
      readDir: (path: string) => Promise<HermesReadDirResult>
      gitRoot?: (path: string) => Promise<string | null>
      // Reveal a path in the OS file manager (Finder / Explorer).
      revealPath?: (path: string) => Promise<boolean>
      // Open a DIRECTORY (created if missing) in the OS file manager.
      openDir?: (path: string) => Promise<{ ok: boolean; error?: string }>
      // Rename a file/folder in place (new base name, same parent dir).
      renamePath?: (path: string, newName: string) => Promise<{ path: string }>
      // Write a small UTF-8 text file (hardened path, parent must exist).
      writeTextFile?: (path: string, content: string) => Promise<{ path: string }>
      // Move a file/folder to the OS trash (recoverable).
      trashPath?: (path: string) => Promise<boolean>
      // Git-driven worktree management for the "Start work" flow.
      git?: {
        worktreeList: (repoPath: string) => Promise<HermesGitWorktree[]>
        worktreeAdd: (
          repoPath: string,
          options?: { name?: string; branch?: string; base?: string; existingBranch?: string }
        ) => Promise<{ path: string; branch: string; repoRoot: string }>
        worktreeRemove: (
          repoPath: string,
          worktreePath: string,
          options?: { force?: boolean }
        ) => Promise<{ removed: string }>
        branchSwitch: (repoPath: string, branch: string) => Promise<{ branch: string }>
        // Local branches for the "convert a branch into a worktree" picker.
        branchList: (repoPath: string) => Promise<HermesGitBranch[]>
        // Local + remote-tracking branches for the "base branch" picker in the
        // new-worktree dialog. The remote default (origin/HEAD) is flagged so
        // the UI can preselect it.
        baseBranchList: (repoPath: string) => Promise<HermesGitBaseBranch[]>
        // Compact working-tree status for the composer coding rail. Null on a
        // non-repo / remote backend (where the Electron probe can't run).
        repoStatus: (repoPath: string) => Promise<HermesRepoStatus | null>
        // Working-tree-vs-HEAD unified diff for one file (the preview's diff
        // view). Empty string when the file is unchanged or not in a repo.
        fileDiff: (repoPath: string, filePath: string) => Promise<string>
        // Codex-style review pane: changed files per scope, per-file diff, and
        // stage / unstage / revert.
        review: {
          list: (repoPath: string, scope: HermesReviewScope, baseRef?: null | string) => Promise<HermesReviewList>
          diff: (
            repoPath: string,
            filePath: string,
            scope: HermesReviewScope,
            baseRef?: null | string,
            staged?: boolean
          ) => Promise<string>
          stage: (repoPath: string, filePath?: null | string) => Promise<{ ok: boolean }>
          unstage: (repoPath: string, filePath?: null | string) => Promise<{ ok: boolean }>
          revert: (repoPath: string, filePath?: null | string) => Promise<{ ok: boolean }>
          revParse: (repoPath: string, ref?: null | string) => Promise<null | string>
          commit: (repoPath: string, message: string, push: boolean) => Promise<{ ok: boolean }>
          // Diff (staged-or-all) + recent commit subjects for drafting a
          // commit message. Reads only; empty strings off-repo.
          commitContext: (repoPath: string) => Promise<{ diff: string; recent: string }>
          push: (repoPath: string) => Promise<{ ok: boolean }>
          shipInfo: (repoPath: string) => Promise<HermesReviewShipInfo>
          createPr: (repoPath: string) => Promise<{ url: string }>
        }
        // Repo-first discovery: scan bounded roots for git repos (depth-capped).
        scanRepos: (
          roots: string[],
          options?: { maxDepth?: number; enabled?: boolean; excludePaths?: string[] }
        ) => Promise<{ root: string; label: string }[]>
      }
      terminal: {
        /** Best-effort current working directory of the live PTY child (POSIX
         *  only; null on Windows or when unavailable). Used to reopen a tab
         *  where the user last `cd`'d. */
        cwd: (id: string) => Promise<string | null>
        dispose: (id: string) => Promise<boolean>
        onData: (id: string, callback: (payload: string) => void) => () => void
        onExit: (id: string, callback: (payload: HermesTerminalExit) => void) => () => void
        resize: (id: string, size: { cols: number; rows: number }) => Promise<boolean>
        start: (options?: { cols?: number; cwd?: string; rows?: number }) => Promise<HermesTerminalSession>
        write: (id: string, data: string) => Promise<boolean>
      }
      onClosePreviewRequested?: (callback: () => void) => () => void
      onOpenUpdatesRequested?: (callback: () => void) => () => void
      onDeepLink?: (
        callback: (payload: { kind: string; name: string; params: Record<string, string> }) => void
      ) => () => void
      signalDeepLinkReady?: () => Promise<{ ok: boolean }>
      onWindowStateChanged?: (callback: (payload: HermesWindowState) => void) => () => void
      onFocusSession?: (callback: (sessionId: string) => void) => () => void
      onNotificationAction?: (callback: (payload: { actionId: string; sessionId?: string }) => void) => () => void
      onPreviewFileChanged: (callback: (payload: HermesPreviewFileChanged) => void) => () => void
      onBackendExit: (callback: (payload: BackendExit) => void) => () => void
      // Soft gateway-mode apply: primary backend was torn down without a window
      // reload. Wipe session lists (skeletons) and re-dial.
      onConnectionApplied?: (callback: () => void) => () => void
      onPowerResume?: (callback: () => void) => () => void
      onBootProgress: (callback: (payload: DesktopBootProgress) => void) => () => void
      getBootstrapState: () => Promise<DesktopBootstrapState>
      resetBootstrap: () => Promise<{ ok: boolean }>
      repairBootstrap: () => Promise<{ ok: boolean }>
      cancelBootstrap: () => Promise<{ ok: boolean; cancelled: boolean }>
      onBootstrapEvent: (callback: (payload: DesktopBootstrapEvent) => void) => () => void
      getVersion: () => Promise<DesktopVersionInfo>
      getRemoteDisplayReason?: () => Promise<string | null>
      localAi: LocalAiRendererBridge
      updates: {
        check: () => Promise<DesktopUpdateStatus>
        apply: (opts?: DesktopUpdateApplyOptions) => Promise<DesktopUpdateApplyResult>
        getBranch: () => Promise<{ branch: string }>
        setBranch: (name: string) => Promise<{ branch: string }>
        onProgress: (callback: (payload: DesktopUpdateProgress) => void) => () => void
      }
      uninstall: {
        summary: () => Promise<DesktopUninstallSummary>
        run: (mode: DesktopUninstallMode) => Promise<DesktopUninstallResult>
      }
      themes: {
        // Download a VS Code Marketplace extension and return the raw color
        // theme files it contributes. The renderer converts + persists them.
        fetchMarketplace: (id: string) => Promise<DesktopMarketplaceThemeResult>
        // Search the Marketplace for color-theme extensions. An empty query
        // returns the most-installed themes.
        searchMarketplace: (query: string) => Promise<DesktopMarketplaceSearchItem[]>
      }
      // IX Agency: locally-persisted settings (portal/gateway/VPN profile),
      // the WireGuard VPN control, and the admin-mcp MCP directory.
      ixAgency?: {
        getSettings: () => Promise<IxAgencyRendererSettings>
        saveSettings: (payload: IxAgencySettingsInput) => Promise<IxAgencyRendererSettings>
        pickVpnConf: () => Promise<{ canceled: boolean; path: null | string }>
        importVpnConf: () => Promise<{ imported: boolean; detail: string }>
        vpnStatus: () => Promise<IxAgencyVpnStatus>
        vpnConnect: () => Promise<IxAgencyVpnStatus>
        vpnDisconnect: () => Promise<IxAgencyVpnStatus>
        // Status lamps + update poller (fed by main-process pollers).
        statusSummary: (refresh?: boolean) => Promise<IxStatusSummary>
        updateCheck: () => Promise<IxUpdateStatus>
        updateApply: () => Promise<{ opened: boolean; detail: string }>
        // Cognito S2S + local Hermes first-run init.
        hermesStatus: () => Promise<IxHermesStatus>
        cognitoValidate: (payload: {
          clientId?: string
          clientSecret?: string
        }) => Promise<{ ok: boolean; detail: string }>
        hermesInit: () => Promise<{ ok: boolean; log: string }>
        listMcpTiles: () => Promise<{ detail: string; tiles: IxAgencyMcpTile[] }>
        // User-level SKILL.md drafts + publish to the portal's global catalog.
        skillsList: () => Promise<{ skills: IxAgencyUserSkill[]; templates: IxAgencySkillTemplate[] }>
        skillsSave: (payload: {
          content: string
          description?: string
          id?: null | string
          title: string
        }) => Promise<IxAgencyUserSkill>
        skillsDelete: (id: string) => Promise<{ deleted: boolean }>
        skillsPublish: (id: string) => Promise<IxAgencyUserSkill>
        // Login enforcement: main-process probe of the portal OTP session.
        authStatus: (force?: boolean) => Promise<{ authenticated: boolean; detail: string; portalUrl: string }>
        // Native OTP login (no webview): request an emailed code, verify it.
        authSendOtp: (email: string) => Promise<{ challenge: string; expiresAt: number; devCode?: string }>
        authVerifyOtp: (payload: {
          challenge: string
          code: string
          email: string
        }) => Promise<{ authenticated: boolean; detail: string }>
        // Native chat: LiteLLM streaming + admin-mcp tool loop in the main
        // process. chatConfirm is the ONLY write-approval channel.
        chatModels: () => Promise<{ models: { id: string; label: string }[]; defaultModel: string }>
        chatList: () => Promise<IxChatConversationMeta[]>
        chatGet: (conversationId: string) => Promise<IxChatConversationDetail | null>
        chatSend: (payload: IxChatSendInput) => Promise<{ conversationId: string }>
        chatConfirm: (payload: {
          conversationId: string
          nonce: string
          approve: boolean
        }) => Promise<{ ok: boolean; state: 'approved' | 'denied' }>
        onChatEvent: (callback: (event: IxChatRendererEvent) => void) => () => void
        // Auto-attach sync (MCP directory + dynamic connectors + org skills):
        // runs automatically after OTP login and on boot with a live session.
        syncGet: () => Promise<IxAgencySyncState>
        syncRun: () => Promise<IxAgencySyncState>
        onSyncEvent: (callback: (state: IxAgencySyncState) => void) => () => void
        // Per-MCP-tile health lamps, cached ~30s in the main process.
        mcpHealth: (refresh?: boolean) => Promise<{ probedAt: number; results: IxMcpTileHealth[] }>
        // Dynamic connectors (super admin). Tokens are write-only pass-through.
        connectorsList: () => Promise<IxDynamicConnectorRow[]>
        connectorsSave: (payload: IxConnectorDraftInput) => Promise<IxDynamicConnectorRow>
        connectorsPatch: (payload: { id: string; enabled: boolean }) => Promise<IxDynamicConnectorRow>
        connectorsDelete: (id: string) => Promise<{ deleted: boolean }>
        connectorsTest: (payload: {
          authHeader?: string
          id?: string
          token?: string
          url?: string
        }) => Promise<{ ok: boolean; message: string; toolCount?: number; tools?: string[] }>
        connectorsParseImport: (json: string) => Promise<{ connectors: IxConnectorDraftInput[]; errors: string[] }>
        connectorsExport: () => Promise<{ json: string; count: number }>
      }
      // QuizVerse: DeepTutor platform supervisor + settings + surface catalog.
      // Present in every build's preload, but the main-process handlers only
      // register for the QuizVerse brand (other brands reject every invoke).
      foundrly?: {
        status: () => Promise<{
          ok: boolean
          brand: string
          workspace: string
          webUrl: string
          adminPortalUrl: string
          settingsPath: string
        }>
        openUrl: (url: string) => Promise<{ ok: boolean; url: string }>
      }
      quizverse?: {
        getSettings: () => Promise<QuizverseRendererSettings>
        saveSettings: (payload: QuizverseSettingsInput) => Promise<QuizverseRendererSettings>
        tutorStatus: () => Promise<DeepTutorRendererStatus>
        tutorStart: () => Promise<DeepTutorRendererStatus>
        tutorStop: () => Promise<DeepTutorRendererStatus>
        tutorRestart: () => Promise<DeepTutorRendererStatus>
        validateLitellm: (payload?: {
          key?: string
          url?: string
        }) => Promise<{ model: string; modelCount: number; provider: string }>
        tutorRequest: (payload: QuizverseTutorRequest) => Promise<{ body: string; contentType: string; status: number }>
        tutorStreamStart: (path: string) => Promise<string>
        tutorStreamStop: (id: string) => Promise<void>
        onTutorStreamEvent: (
          callback: (event: { data?: unknown; id: string; type: 'data' | 'done' | 'error' }) => void
        ) => () => void
        tutorWsConnect: (path: string, userId: string) => Promise<string>
        tutorWsSend: (id: string, data: string) => Promise<void>
        tutorWsClose: (id: string) => Promise<void>
        onTutorWsEvent: (callback: (event: QuizverseTutorWsEvent) => void) => () => void
        // Managed install: venv + `pip install -U deeptutor` under userData.
        provisionTutor: () => Promise<{ error: string; ok: false } | { ok: true }>
        onProvisionEvent: (callback: (payload: QuizverseProvisionEvent) => void) => () => void
        pickTutorDirectory: () => Promise<{ canceled: boolean; dir: null | string }>
        playSession: () => Promise<{ deviceId: string; userId: string; username: string }>
        playRpc: <T = unknown>(name: string, payload?: Record<string, unknown>) => Promise<T>
        playRealtimeConnect: () => Promise<{ id: string; userId: string }>
        playRealtimeListMatches: (id: string, query: string) => Promise<{ match_id?: string }[]>
        playRealtimeJoinMatch: (
          id: string,
          matchId: string
        ) => Promise<{
          matchId: string
          presences: { sessionId?: string; userId?: string; username?: string }[]
        }>
        playRealtimeCreateMatch: (id: string, payload: Record<string, unknown>) => Promise<{ matchId: string }>
        playRealtimeSend: (id: string, opCode: number, payload: Record<string, unknown>) => Promise<void>
        playRealtimeClose: (id: string) => Promise<void>
        authStart: () => Promise<{ state: 'pending' }>
        authStatus: () => Promise<QuizverseAuthStatus>
        onAuthEvent: (callback: (event: QuizverseAuthEvent) => void) => () => void
        productRequest: (input: QuizverseProductRequest) => Promise<QuizverseProductResponse>
        productStream: (
          input: QuizverseProductRequest,
          onChunk: (chunk: string) => void
        ) => Promise<QuizverseProductResponse>
        productCancel: (streamId: string) => Promise<void>
        mcpStatus: () => Promise<QuizverseMcpStatus>
        onPlayRealtimeEvent: (callback: (event: QuizversePlayRealtimeEvent) => void) => () => void
        updateCheck: () => Promise<IxUpdateStatus>
        updateApply: () => Promise<{ opened: boolean; detail: string }>
        onTutorEvent: (callback: (status: DeepTutorRendererStatus) => void) => () => void
      }
    }
  }
}

export interface QuizversePlayRealtimeEvent {
  data?: unknown
  id: string
  type: 'disconnect' | 'error' | 'match-data' | 'match-presence' | 'matchmaker-matched' | 'notification'
}

export interface QuizverseAuthStatus {
  authenticated: boolean
  configured: boolean
  userId: string
  username: string
}

export interface QuizverseAuthEvent {
  authenticated: boolean
  error?: string
  userId?: string
  username?: string
}

export interface QuizverseProductRequest {
  body?: unknown
  cacheMode?: 'default' | 'reload'
  form?: Array<{
    dataBase64?: string
    filename?: string
    mime?: string
    name: string
    value?: string
  }>
  method?: 'DELETE' | 'GET' | 'PATCH' | 'POST' | 'PUT'
  path: string
  streamId?: string
}

export interface QuizverseProductResponse {
  body: string
  contentType: string
  etag?: string
  offline?: boolean
  status: number
}

export interface QuizverseMcpStatus {
  auth: 'authenticated' | 'guest' | 'pending'
  detail: string
  state: 'error' | 'ready'
  toolCount: number
}

export interface QuizverseRendererSettings {
  tutorMode: 'local' | 'remote'
  remoteUrl: string
  localCommand: string
  localDirectory: string
  /** 0 = allocate a free port at spawn time (the default). */
  apiPort: number
  /** 0 = allocate a free port at spawn time (the default). */
  webPort: number
  // The raw API key never crosses back into the renderer.
  apiKeySet: boolean
  litellmUrl: string
  // The raw LiteLLM key never crosses back into the renderer.
  litellmKeySet: boolean
  cognitoDomain: string
  cognitoClientId: string
  cognitoIssuer: string
}

export interface QuizverseProvisionEvent {
  done?: boolean
  error?: string
  line?: string
}

export interface QuizverseTutorRequest {
  body?: string
  form?: { data?: ArrayBuffer; filename?: string; name: string; type?: string; value?: string }[]
  headers?: Record<string, string>
  method?: string
  path: string
}

export interface QuizverseTutorWsEvent {
  data?: unknown
  id: string
  type: 'close' | 'error' | 'message' | 'open'
}

export interface QuizverseSettingsInput {
  tutorMode?: 'local' | 'remote'
  remoteUrl?: string
  localCommand?: string
  localDirectory?: string
  apiPort?: number
  webPort?: number
  apiKey?: string
  litellmUrl?: string
  litellmKey?: string
  cognitoDomain?: string
  cognitoClientId?: string
  cognitoIssuer?: string
}

export interface DeepTutorRendererStatus {
  state: 'error' | 'remote' | 'running' | 'starting' | 'stopped'
  mode: 'local' | 'remote'
  webUrl: string
  apiUrl: string
  pid: null | number
  detail: string
  logTail: string[]
  reachable?: boolean
}

export interface IxAgencyRendererSettings {
  portalUrl: string
  gatewayUrl: string
  // The raw bearer token never crosses back into the renderer.
  gatewayTokenSet: boolean
  vpnConfPath: string
  // Whether a usa-vpn.conf has been imported into the keychain (contents
  // never cross back into the renderer).
  vpnConfImported: boolean
  vpnExitIp: string
  litellmUrl: string
  // The raw LiteLLM key never crosses back into the renderer.
  litellmKeySet: boolean
  customChatModels: string
  updateManifestUrl: string
  cognitoOauth2Url: string
  cognitoClientId: string
  cognitoClientSecretSet: boolean
  cognitoScope: string
  hermesInitialized: boolean
}

export interface IxAgencySettingsInput {
  portalUrl?: string
  gatewayUrl?: string
  gatewayToken?: string
  vpnConfPath?: string
  vpnExitIp?: string
  litellmUrl?: string
  litellmKey?: string
  customChatModels?: string
  updateManifestUrl?: string
  cognitoOauth2Url?: string
  cognitoClientId?: string
  cognitoClientSecret?: string
  cognitoScope?: string
}

export interface IxVpnDeepStatus {
  state: 'connected' | 'connecting' | 'degraded' | 'disconnected' | 'error' | 'unavailable'
  tunnelUp: boolean
  handshakeAgeSecs: null | number
  egressIp: null | string
  expectedExitIp: string
  detail: string
}

export interface IxMcpLampStatus {
  state: 'green' | 'grey' | 'red'
  reachable: boolean
  authenticated: boolean
  toolCount: number
  detail: string
}

export interface IxUpdateStatus {
  updateAvailable: boolean
  /** True when clicking Update installs in place (NSIS, signed Squirrel.Mac,
   *  AppImage). Unset/false ⇒ clicking opens the download page instead. */
  inPlace?: boolean
  currentVersion: string
  latestVersion: string
  url: string
  notes: string
  detail: string
}

export interface IxStatusSummary {
  vpn: IxVpnDeepStatus | null
  mcp: IxMcpLampStatus | null
  update: IxUpdateStatus | null
}

export interface IxHermesStatus {
  initialized: boolean
  configPath: string
  configExists: boolean
  hasCognitoCreds: boolean
  installerAvailable: boolean
  detail: string
}

export interface IxChatConversationMeta {
  id: string
  title: string
  model: string
  updatedAt: number
}

export interface IxChatDisplayItem {
  kind: 'assistant' | 'confirm' | 'tool' | 'user'
  inputModality?: 'text' | 'voice'
  text?: string
  name?: string
  argsSummary?: string
  result?: string
  status?: 'approved' | 'error' | 'gated' | 'ok'
  nonce?: string
  state?: 'approved' | 'denied' | 'pending'
  at: number
}

export interface IxChatConversationDetail {
  id: string
  title: string
  model: string
  skills: string[]
  display: IxChatDisplayItem[]
}

export interface IxChatSendInput {
  conversationId?: null | string
  /** Trusted renderer metadata; never concatenated into user text. */
  inputModality?: 'text' | 'voice'
  text: string
  model?: string
  skills?: { name: string; content: string }[]
}

export interface IxChatRendererEvent {
  conversationId: string
  type: 'confirmation-required' | 'done' | 'error' | 'step' | 'text-delta' | 'tool-call' | 'tool-result'
  delta?: string
  step?: number
  nonce?: string
  tool?: string
  name?: string
  argsSummary?: string
  status?: 'approved' | 'error' | 'gated' | 'ok'
  result?: string
  message?: string
}

export interface IxAgencyVpnStatus {
  state: 'connected' | 'connecting' | 'disconnected' | 'unavailable' | 'unknown'
  detail: string
  interfaceName?: string
}

export interface IxAgencyMcpTile {
  id: string
  label: string
  blurb?: string
  group?: string
  mcpUrl: string
  domain: string
  mcpAuthHint?: string
  hasDefaultToken?: boolean
}

/** User-level SKILL.md draft under ~/.hermes/skills/ix-user/<id>/. */
export interface IxAgencyUserSkill {
  id: string
  title: string
  description: string
  content: string
  updatedAt: number
  /** adminSkill.<slug> portal document id once published globally. */
  publishedId: null | string
}

export interface IxAgencySkillTemplate {
  id: string
  title: string
  description: string
  content: string
}

/** Per-MCP-tile health lamp (mirrors electron/ix-mcp-health.ts). */
export interface IxMcpTileHealth {
  tileId: string
  state: 'green' | 'grey' | 'red'
  detail: string
  toolCount?: number
}

/** Portal dynamic connector row — tokens never reach the renderer. */
export interface IxDynamicConnectorRow {
  id: string
  label: string
  url: string
  transport: 'cluster-mcp' | 'remote-mcp'
  authHeader: string
  hasToken: boolean
  category: string
  appIds: string[]
  bundles: string[]
  readOnlyTools: string[]
  enabled: boolean
  lastTestedAt?: string
  toolCount?: number
  toolNames?: string[]
}

/** Connector create/update payload (token is write-only). */
export interface IxConnectorDraftInput {
  id?: string
  label: string
  url: string
  transport: 'cluster-mcp' | 'remote-mcp'
  authHeader: string
  token?: string
  category: string
  appIds: string[]
  bundles: string[]
  readOnlyTools: string[]
  enabled?: boolean
}

/** Live org skill from the portal's admin catalog (built-in + team). */
export interface IxPortalCatalogSkill {
  id: string
  title: string
  description: string
  persona: string
  rank: null | number
  superAdminOnly: boolean
  content: string
  starterPrompts: string[]
  tiers: string[]
  bundles: string[]
  appIds: string[]
  source: 'built-in' | 'team'
}

/** Result of the login/boot auto-attach sync (main process). */
export interface IxAgencySyncState {
  syncedAt: null | number
  tiles: IxAgencyMcpTile[]
  tilesDetail: string
  connectors: IxDynamicConnectorRow[]
  connectorsError: null | string
  orgSkills: IxPortalCatalogSkill[]
  orgSkillsError: null | string
}

export interface DesktopMarketplaceSearchItem {
  extensionId: string
  displayName: string
  publisher: string
  description: string
  installs: number
}

export interface DesktopMarketplaceThemeFile {
  label: string
  /** VS Code's `uiTheme` for this entry (vs-dark / vs / hc-black). */
  uiTheme?: string
  /** Raw theme JSON (JSONC) text, parsed + converted by the renderer. */
  contents: string
}

export interface DesktopMarketplaceThemeResult {
  extensionId: string
  displayName: string
  themes: DesktopMarketplaceThemeFile[]
}

export interface HermesTerminalSession {
  cwd: string
  id: string
  shell: string
}

export interface HermesTerminalExit {
  code: number | null
  signal: string | null
}

export interface DesktopVersionInfo {
  appVersion: string
  electronVersion: string
  nodeVersion: string
  platform: string
  hermesRoot: string
}

export interface DesktopStudioStatus {
  state: 'absent' | 'available' | 'running' | 'degraded' | 'stopped'
  mode: 'absent' | 'bring-your-own' | 'managed'
  version: string | null
  executable: string | null
  workspacePath: string | null
  sessionId: string | null
  windowId: string | null
  pid: number | null
  detail: string
}

export type DesktopUninstallMode = 'full' | 'gui' | 'lite'

export interface DesktopUninstallSummary {
  hermes_home: string
  agent_installed: boolean
  gui_installed: boolean
  source_built_artifacts: string[]
  packaged_app_paths: string[]
  userdata_dir: string
  userdata_exists: boolean
  platform: string
  running_app_path?: null | string
  probe?: string
}

export interface DesktopUninstallResult {
  ok: boolean
  mode?: DesktopUninstallMode
  willRemoveAppBundle?: boolean
  scriptPath?: string
  error?: string
  message?: string
}

export interface DesktopUpdateCommit {
  sha: string
  summary: string
  author: string
  at: number
}

export interface DesktopUpdateStatus {
  supported: boolean
  updateAvailable?: boolean
  branch?: string
  currentBranch?: string
  reason?: string
  message?: string
  error?: string
  behind?: number
  currentSha?: string
  targetSha?: string
  commits?: DesktopUpdateCommit[]
  dirty?: boolean
  fetchedAt?: number
}

export type DesktopUpdateDirtyStrategy = 'abort' | 'stash' | 'force'

export interface DesktopUpdateApplyOptions {
  dirtyStrategy?: DesktopUpdateDirtyStrategy
}

export interface DesktopUpdateApplyResult {
  ok: boolean
  branch?: string
  error?: string
  message?: string
  /** True when no staged updater exists (CLI install) and the user should run
   *  `hermes update` themselves. `command` is the exact line to run. */
  manual?: boolean
  command?: string
  hermesRoot?: string
  /** True when the backend was updated but the GUI couldn't be relaunched in
   *  place (AppImage / dev run): the new version loads on next launch. */
  backendUpdated?: boolean
  /** False when the running GUI package was NOT replaced by this update
   *  (Linux GUI/backend skew, or a sandbox-blocked relaunch). Distinguishes
   *  "backend only" outcomes from a real in-place GUI relaunch. (#45205) */
  guiUpdated?: boolean
  /** True for the Linux GUI/backend-skew terminal state: backend updated but
   *  the running AppImage/.deb/.rpm shell is unchanged and must be
   *  reinstalled. Renders a closeable "update the desktop app" message. */
  guiSkew?: boolean
  /** True when the update finished but the app must be quit + reopened by hand
   *  (e.g. the rebuilt sandbox helper isn't launchable): keep a working
   *  window, don't auto-quit into a dead app. (#45205) */
  manualRestart?: boolean
  /** True when the auto-relaunch was skipped specifically because the rebuilt
   *  chrome-sandbox helper is not launchable (not root:root + setuid). */
  sandboxBlocked?: boolean
  /** True when a detached relauncher took over (macOS bundle swap / Linux
   *  re-exec): the app is about to quit and reopen itself. */
  handedOff?: boolean
}

export type DesktopUpdateStage =
  | 'idle'
  | 'prepare'
  | 'fetch'
  | 'pull'
  | 'pydeps'
  | 'update'
  | 'rebuild'
  | 'restart'
  | 'done'
  | 'manual'
  /** Backend updated but the running GUI package (AppImage/.deb/.rpm) was NOT
   *  changed — the user must update/reinstall the desktop app. Terminal,
   *  closeable; never claims the GUI was updated. (#45205) */
  | 'guiSkew'
  | 'error'

export interface DesktopUpdateProgress {
  stage: DesktopUpdateStage
  message: string
  percent: number | null
  error: string | null
  at: number
}

export interface HermesConnection {
  baseUrl: string
  isFullscreen: boolean
  // The live, RESOLVED connection mode. Only ever 'local' or 'remote' — a
  // 'cloud' saved-config entry resolves to a 'remote' connection under the hood
  // (cloud-auto-discovery Q3/Q6), so this never carries 'cloud'.
  mode?: 'local' | 'remote'
  authMode?: 'oauth' | 'token'
  remoteHost?: string
  remoteIdentity?: string
  remoteKind?: 'cloud' | 'ssh' | 'url'
  remoteHermesVersion?: string
  nativeOverlayWidth: number
  source?: 'env' | 'local' | 'settings'
  token: string
  wsUrl: string
  logs: string[]
  // Set for pool (non-primary) backends so the renderer knows which profile a
  // connection belongs to.
  profile?: string
  windowButtonPosition: { x: number; y: number } | null
}

export interface HermesTitleBarTheme {
  background: string
  foreground: string
}

export interface HermesWindowState {
  isFullscreen: boolean
  nativeOverlayWidth: number
  windowButtonPosition: { x: number; y: number } | null
}

export interface DesktopActiveProfile {
  // The desktop's stored profile preference, or null when unset (legacy launch
  // that defers to the sticky active_profile / default).
  profile: string | null
}

export interface DesktopConnectionConfig {
  envOverride: boolean
  // The saved connection mode. 'cloud' is a Hermes Cloud connection: it carries
  // a remote-shaped block (remoteUrl = the selected agent's dashboardUrl,
  // remoteAuthMode 'oauth') but is remembered as cloud so settings reopens into
  // the cloud picker. Resolution treats cloud exactly as remote
  // (cloud-auto-discovery Q3/Q6).
  mode: 'local' | 'remote' | 'cloud' | 'ssh'
  // The profile this config describes, or null for the global/default
  // connection. Per-profile entries let a profile point at its own backend.
  profile: null | string
  remoteAuthMode: 'oauth' | 'token'
  remoteOauthConnected: boolean
  remoteTokenPreview: string | null
  remoteTokenSet: boolean
  remoteUrl: string
  // For a 'cloud' connection: the persisted Hermes Cloud org (slug or id) the
  // connected instance was discovered under, so Settings → Gateway can reopen
  // into that org. Empty string for remote/local.
  cloudOrg: string
  sshHost: string
  sshUser: string
  sshPort: number | null
  sshKeyPath: string
  sshRemoteHermesPath: string
}

export interface DesktopConnectionConfigInput {
  mode: 'local' | 'remote' | 'cloud' | 'ssh'
  // When set, the save/apply/test targets this profile's per-profile remote
  // override instead of the global connection.
  profile?: null | string
  remoteAuthMode?: 'oauth' | 'token'
  remoteToken?: string
  remoteUrl?: string
  // For a 'cloud' connection: the selected Hermes Cloud org (slug or id) to
  // persist so Settings can reopen into it. Ignored for remote/local modes.
  cloudOrg?: string
  sshHost?: string
  sshUser?: string
  sshPort?: number | null
  sshKeyPath?: string
  sshRemoteHermesPath?: string
}

export interface DesktopConnectionTestResult {
  baseUrl?: string
  ok?: boolean
  version?: string | null
  reachable?: boolean
  sshError?:
    | 'auth-failed'
    | 'hermes-not-found'
    | 'host-key-changed'
    | 'timeout'
    | 'unreachable'
    | 'unsupported-platform'
    | 'update-required'
    | 'unknown'
    | null
  error?: string | null
  host?: string
  remoteHermesPath?: string
  remoteHermesVersion?: string
  remotePlatform?: string
}

export interface DesktopSshResolveResult {
  hostname: string | null
  identityFile: string | null
  port: number | null
  user: string | null
}

export interface DesktopSshHostsResult {
  hosts: string[]
}

export interface DesktopAuthProvider {
  name: string
  displayName: string
  // True when this provider authenticates with a username + password
  // (the gateway's /login page renders a credential form) rather than an
  // OAuth redirect. The session/cookie/ws-ticket machinery is identical;
  // only the login-page form and the desktop's button copy differ.
  supportsPassword?: boolean
}

export interface DesktopConnectionProbeResult {
  baseUrl: string
  reachable: boolean
  authMode: 'oauth' | 'token' | 'unknown'
  providers: DesktopAuthProvider[]
  version: string | null
  error: string | null
}

export interface DesktopOauthLoginResult {
  ok: boolean
  baseUrl: string
  connected: boolean
}

export interface DesktopOauthLogoutResult {
  ok: boolean
  connected: boolean
}

// --- Hermes Cloud (cloud-auto-discovery Phase 3) ---

export interface DesktopCloudStatus {
  // The portal base URL the desktop talks to (default or env-overridden).
  portalBaseUrl: string
  // Whether the OAuth partition holds a live Nous portal (Privy) session — the
  // portal authenticates via Privy, so this reflects the privy-token cookie, NOT
  // the hermes gateway session cookies. See cookiesHavePrivySession.
  signedIn: boolean
}

// A discovered Hermes Cloud agent — the trimmed DTO from NAS GET /api/agents.
export interface DesktopCloudAgent {
  id: string
  name: string
  status: string
  // null until the agent has a provisioned dashboard (show "provisioning…").
  dashboardUrl: string | null
  // "active" | "degraded" | "down" | "unknown".
  dashboardGatewayState: string
}

// An org the signed-in user belongs to — for the org picker shown when a
// multi-org user's discovery call needs disambiguation (NAS 409).
export interface DesktopCloudOrg {
  id: string
  slug: string | null
  name: string
  isPersonal: boolean
  // "OWNER" | "MEMBER".
  role: string
}

// Discovery result: either the agent list, OR a request to pick an org first
// (multi-org user, no org chosen yet). The renderer shows a picker on the
// latter and re-calls discover(org). On the agents branch, `org` echoes the
// authoritatively-resolved org the list was scoped to (from NAS), so the
// desktop persists it without relying on transient picker state.
export type DesktopCloudDiscoverResult =
  | { agents: DesktopCloudAgent[]; org?: DesktopCloudOrg | null; needsOrgSelection?: false }
  | { needsOrgSelection: true; orgs: DesktopCloudOrg[] }

export interface DesktopCloudAgentSignInResult {
  // The agent gateway base URL the silent sign-in targeted.
  baseUrl: string
  // Whether the agent's gateway session cookie landed (silent cascade done).
  connected: boolean
}

export interface DesktopBootProgress {
  error: string | null
  fakeMode: boolean
  message: string
  phase: string
  progress: number
  running: boolean
  timestamp: number
}

// First-launch install ("bootstrap") event types -- emitted by
// electron/bootstrap-runner.ts and observed by the renderer install overlay.
// Mirrors the event shapes emitted by runBootstrap()'s onEvent callback.

export interface DesktopBootstrapStageDescriptor {
  name: string
  title?: string
  category?: string
  needs_user_input?: boolean
}

export type DesktopBootstrapStageState = 'pending' | 'running' | 'succeeded' | 'skipped' | 'failed'

export interface DesktopBootstrapStageResult {
  state: DesktopBootstrapStageState
  durationMs: number | null
  startedAt: number | null
  json: { ok: boolean; skipped?: boolean; reason?: string | null; stage: string } | null
  error: string | null
}

export interface DesktopBootstrapUnsupportedPlatform {
  platform: string
  activeRoot: string
  installCommand: string
  docsUrl: string
}

export interface DesktopBootstrapState {
  active: boolean
  manifest: { type: 'manifest'; stages: DesktopBootstrapStageDescriptor[]; protocolVersion: number | null } | null
  stages: Record<string, DesktopBootstrapStageResult>
  error: string | null
  log: Array<{ ts: number; stage: string | null; line: string; stream?: 'stdout' | 'stderr' }>
  startedAt: number | null
  completedAt: number | null
  unsupportedPlatform: DesktopBootstrapUnsupportedPlatform | null
}

export type DesktopBootstrapEvent =
  | { type: 'manifest'; stages: DesktopBootstrapStageDescriptor[]; protocolVersion: number | null }
  | {
      type: 'stage'
      name: string
      state: DesktopBootstrapStageState
      durationMs?: number
      json?: DesktopBootstrapStageResult['json']
      error?: string | null
    }
  | { type: 'log'; stage?: string | null; line: string; stream?: 'stdout' | 'stderr' }
  | { type: 'complete'; marker: Record<string, unknown> }
  | { type: 'failed'; stage?: string | null; error: string }
  | {
      type: 'unsupported-platform'
      platform: string
      activeRoot: string
      installCommand: string
      docsUrl: string
    }

export interface HermesApiRequest {
  path: string
  method?: string
  body?: unknown
  // Single-file multipart upload (FastAPI UploadFile endpoints). Mutually
  // exclusive with `body`; bytes transfer over IPC as a structured-clone
  // ArrayBuffer. Token-mode backends only.
  upload?: { filename: string; contentType?: string; bytes: ArrayBuffer }
  timeoutMs?: number
  // Route this REST call to a specific profile's backend. Omit for the primary
  // (window) backend. Read-only cross-profile data is served by the primary, so
  // this is only needed for profile-scoped live/settings calls.
  profile?: string | null
}

export interface HermesNotification {
  title?: string
  body?: string
  silent?: boolean
  kind?: string
  sessionId?: string
  actions?: { id: string; text: string }[]
}

export interface HermesPreviewTarget {
  binary?: boolean
  byteSize?: number
  kind: 'file' | 'url'
  label: string
  large?: boolean
  language?: string
  mimeType?: string
  path?: string
  previewKind?: 'binary' | 'html' | 'image' | 'text'
  renderMode?: 'preview' | 'source'
  source: string
  url: string
}

export interface HermesReadFileTextResult {
  binary?: boolean
  byteSize?: number
  language?: string
  mimeType?: string
  path: string
  text: string
  truncated?: boolean
}

export interface HermesPreviewWatch {
  id: string
  path: string
}

// A real git worktree as reported by `git worktree list` (source of truth for
// the "Start work" flow), as opposed to the session-cwd-derived grouping above.
export interface HermesGitWorktree {
  path: string
  branch: null | string
  isMain: boolean
  detached: boolean
  locked: boolean
}

// A local branch as offered by the "convert a branch into a worktree" picker.
// `checkedOut` means selecting opens that checkout; `isDefault` means selecting
// switches the main checkout instead of creating `.worktrees/main`.
export interface HermesGitBranch {
  name: string
  checkedOut: boolean
  isDefault: boolean
  worktreePath: null | string
}

// A branch the new worktree can be based on: local heads + remote-tracking
// refs. `isRemote` distinguishes `origin/main` from a local `main` (the UI
// may show a remote glyph); `isDefault` flags origin/HEAD so the dialog can
// preselect it.
export interface HermesGitBaseBranch {
  name: string
  isRemote: boolean
  isDefault: boolean
}

// A single changed path from `git status --porcelain=v2`, classified by state
// so the coding rail / switcher can group + open the right diff.
export interface HermesRepoStatusFile {
  path: string
  staged: boolean
  unstaged: boolean
  untracked: boolean
  conflicted: boolean
}

// Compact working-tree status for the composer coding rail (parsed from
// `git status --porcelain=v2 --branch`).
export interface HermesRepoStatus {
  branch: null | string
  // The repo's trunk ("main" / "master" / …), so the UI can offer "branch off
  // the default" from anywhere. Null when no trunk is detected.
  defaultBranch: null | string
  detached: boolean
  ahead: number
  behind: number
  staged: number
  unstaged: number
  untracked: number
  conflicted: number
  // Total distinct changed paths (tracked modified + conflicts + untracked).
  changed: number
  // +/- line counts of tracked changes vs HEAD (staged + unstaged). Untracked
  // files aren't in the diff, so they don't contribute lines.
  added: number
  removed: number
  // Capped changed-file list (REPO_STATUS_FILE_CAP) for the diff/open actions.
  files: HermesRepoStatusFile[]
}

// Diff scope for the review pane, mirroring Codex: uncommitted working-tree
// changes, all changes vs the branch base, or everything since the current
// turn began.
export type HermesReviewScope = 'branch' | 'lastTurn' | 'uncommitted'

// One changed file in the review pane (status letter, +/- lines, staged flag).
export interface HermesReviewFile {
  path: string
  added: number
  removed: number
  // M(odified) A(dded) D(eleted) R(enamed) C(opied) U(nmerged) ?(untracked)
  status: string
  staged: boolean
}

export interface HermesReviewList {
  files: HermesReviewFile[]
  // The resolved base ref the scope diffed against (branch merge-base / turn
  // baseline), or null for the uncommitted scope.
  base: null | string
}

// The branch's PR (if any) as reported by `gh pr view`.
export interface HermesReviewPr {
  url: string
  state: string
  number: number
}

// gh availability/auth + the current branch's PR — drives the review pane's PR
// button (disabled when gh isn't ready, "Open PR" vs "Create PR" otherwise).
export interface HermesReviewShipInfo {
  ghReady: boolean
  pr: HermesReviewPr | null
}

export interface HermesReadDirEntry {
  name: string
  path: string
  isDirectory: boolean
}

export interface HermesReadDirResult {
  entries: HermesReadDirEntry[]
  error?: string
}

export interface HermesPreviewFileChanged {
  id: string
  path: string
  url: string
}

export interface HermesSelectPathsOptions {
  title?: string
  defaultPath?: string
  directories?: boolean
  multiple?: boolean
  filters?: Array<{ name: string; extensions: string[] }>
}

export interface BackendExit {
  code: number | null
  signal: string | null
}
