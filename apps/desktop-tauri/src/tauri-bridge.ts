/**
 * Hermes Desktop Tauri Bridge
 *
 * Exposes an invoke()-based API matching the window.hermesDesktop interface
 * that the legacy Electron preload provided. Imports as a module in the React
 * app via `import '@/tauri-bridge'` — the side-effect installs itself on
 * `window.hermesDesktop` at import time.
 *
 * Each method proxies to `@tauri-apps/api/core`'s `invoke`, translating
 * the Electron ipcRenderer.invoke call to a Tauri command invocation.
 */

import { invoke } from '@tauri-apps/api/core'
import { writeText, readText } from '@tauri-apps/plugin-clipboard-manager'
import { open } from '@tauri-apps/plugin-shell'

// ---------------------------------------------------------------------------
// Stubs for methods that need a real Electron process (not available in Tauri).
// These return sensible defaults so the renderer doesn't crash.
// ---------------------------------------------------------------------------

function notAvailable(name: string) {
  console.warn(`[tauri-bridge] ${name} is not available in the Tauri shell`)
}

const noop = () => { /* noop */ }

// ---------------------------------------------------------------------------
// Install bridge
// ---------------------------------------------------------------------------

export function installDesktopBridge() {
  if (typeof window === 'undefined') return
  // Already installed
  if (window.hermesDesktop) return

  // Helper: invoke Tauri command with optional args
  const cmd = <T>(command: string, args?: Record<string, unknown>): Promise<T> =>
    invoke<T>(command, args ?? {})

  window.hermesDesktop = {
    // File system
    readDir: (dirPath: string) => cmd<unknown[]>('readDir', { path: dirPath }),
    readFileText: (filePath: string) => cmd<{ content: string; size: number }>('readFileText', { path: filePath }),
    writeTextFile: async (filePath: string, content: string) => {
      await cmd<void>('writeTextFile', { path: filePath, content })
      return { path: filePath }
    },
    readFileDataUrl: (filePath: string) => cmd<string>('readFileDataUrl', { path: filePath }),
    gitRoot: (startPath: string) => cmd<{ root: string }>('gitRoot', { startPath }),
    revealPath: async (targetPath: string) => {
      await cmd<void>('revealPath', { path: targetPath })
      return true
    },
    renamePath: async (targetPath: string, newName: string) => {
      await cmd<void>('renamePath', { path: targetPath, newName })
      return { path: targetPath.replace(/[^/]+$/, newName) }
    },
    trashPath: async (targetPath: string) => {
      await cmd<void>('trashPath', { path: targetPath })
      return true
    },

    // Clipboard
    writeClipboard: async (text: string) => {
      try {
        await writeText(text)
        return true
      } catch (e) {
        console.error('[tauri-bridge] writeClipboard failed:', e)
        return false
      }
    },

    // Dialogs
    selectPaths: (options?: { directories?: boolean; title?: string }) =>
      cmd<string[]>('selectPaths', { options: options ?? {} }),

    // Connection
    getConnection: (profile?: string | null) =>
      cmd<{ profile: string; status: string; wsUrl: string; apiUrl: string }>(
        'getConnection',
        profile != null ? { profile } : {}
      ),

    // External / API
    openExternal: (url: string) => open(url),
    // Frontend sends { path, method, body, timeoutMs, upload, profile }
    // Convert path to full URL via getConnection, proxy through Rust
    api: async <T>(request: { path: string; method?: string; body?: unknown; timeoutMs?: number; upload?: boolean; profile?: string }): Promise<T> => {
      const conn = await cmd<{ apiUrl?: string }>('getConnection', {})
      const baseUrl = conn?.apiUrl?.replace(/\/api.*$/, '') || 'http://127.0.0.1:44985'
      const url = `${baseUrl}${request.path}`
      const raw = await cmd<{ status: number; data: T }>('api', {
        request: {
          url,
          method: request.method || 'GET',
          headers: [] as Array<[string, string]>,
          body: request.body || null
        }
      })
      return raw.data
    },

    // --- Stubs for Electron-specific methods (return defaults, no crash) ---

    revalidateConnection: () => Promise.resolve({ ok: true, rebuilt: false }),
    touchBackend: () => Promise.resolve({ ok: true }),
    getGatewayWsUrl: async (profile?: string | null) => {
      // Get token from Rust's getConnection (reads HERMES_DASHBOARD_SESSION_TOKEN env var).
      // Direct fetch() from tauri:// origin is blocked by WebKitGTK cross-origin security.
      try {
        const conn = await cmd<{ wsUrl: string; apiUrl: string }>('getConnection', {})
        const base = conn?.wsUrl || ''
        if (!base) return ''
        if (!profile) return base
        return base + '&profile=' + encodeURIComponent(profile)
      } catch {
        return ''
      }
    },
    openSessionWindow: () => Promise.resolve({ ok: false, error: 'not available in Tauri shell' }),
    openNewSessionWindow: () => Promise.resolve({ ok: false, error: 'not available in Tauri shell' }),

    petOverlay: {
      open: () => Promise.resolve({ ok: false }),
      close: () => Promise.resolve({ ok: false }),
      setBounds: noop,
      setIgnoreMouse: noop,
      setFocusable: noop,
      pushState: noop,
      control: noop,
      onState: () => noop,
      onControl: () => noop,
    },

    getBootProgress: () => Promise.resolve({ status: 'done', step: '', progress: 1.0 }),
    getConnectionConfig: () => Promise.resolve({
      envOverride: false,
      mode: 'local' as const,
      profile: null,
      remoteAuthMode: 'token' as const,
      remoteOauthConnected: false,
      remoteTokenPreview: null,
      remoteTokenSet: false,
      remoteUrl: '',
      cloudOrg: ''
    }),
    saveConnectionConfig: (p: unknown) => Promise.resolve(p),
    applyConnectionConfig: (p: unknown) => Promise.resolve(p),
    testConnectionConfig: () => Promise.resolve({ ok: true, latencyMs: 0, baseUrl: 'http://127.0.0.1:44985', version: '0.18.2' }),
    probeConnectionConfig: () => Promise.resolve({ ok: true, wsUrl: 'ws://127.0.0.1:44985/ws', authMode: 'token' as const, baseUrl: 'http://127.0.0.1:44985', reachable: true, providers: [], version: '0.18.2' }),
    oauthLoginConnectionConfig: () => Promise.resolve({ ok: false, error: 'not available' }),
    oauthLogoutConnectionConfig: () => Promise.resolve({ ok: true }),

    cloud: {
      status: () => Promise.resolve({ ok: false, signedIn: false, agents: [] }),
      login: () => Promise.resolve({ ok: false, signedIn: false, agents: [] }),
      logout: () => Promise.resolve({ ok: false, signedIn: false, agents: [] }),
      discover: () => Promise.resolve({ ok: false, agents: [], error: 'not available' }),
      agentSignIn: () => Promise.resolve({ ok: false, error: 'not available' }),
    },

    profile: {
      get: () => Promise.resolve({ name: 'default', home: '' }),
      set: async (name: string) => {
        try {
          const result = await window.hermesDesktop!.api<{ active: string }>({
            path: '/api/profiles/active',
            method: 'POST',
            body: { name }
          })
          return { name: result.active }
        } catch {
          return { name: 'default', home: '' }
        }
      }
    },

    notify: () => Promise.resolve(false),
    requestMicrophoneAccess: () => Promise.resolve(true),
    saveImageFromUrl: () => Promise.resolve(false),
    saveImageBuffer: () => Promise.resolve(''),
    saveClipboardImage: () => Promise.resolve(''),
    getPathForFile: () => '',
    normalizePreviewTarget: () => Promise.resolve(null),
    watchPreviewFile: () => Promise.resolve({ id: '', url: '' }),
    stopPreviewFileWatch: () => Promise.resolve(false),
    setTitleBarTheme: noop,
    setNativeTheme: noop,
    setTranslucency: noop,
    setPreviewShortcutActive: noop,
    openPreviewInBrowser: () => Promise.resolve(),
    fetchLinkTitle: () => Promise.resolve(''),
    sanitizeWorkspaceCwd: (cwd?: string | null) => Promise.resolve({ cwd: cwd ?? '', sanitized: false }),

    settings: {
      getDefaultProjectDir: () => Promise.resolve({ defaultLabel: 'home', dir: null, resolvedCwd: '' }),
      setDefaultProjectDir: () => Promise.resolve({ dir: null }),
      pickDefaultProjectDir: () => Promise.resolve({ canceled: true, dir: null }),
    },

    zoom: {
      get: () => Promise.resolve({ level: 0, percent: 1.0 }),
      setPercent: noop,
      onChanged: () => noop,
    },

    revealLogs: () => notAvailable('revealLogs'),
    getRecentLogs: () => Promise.resolve(''),
    openDir: async (dirPath: string) => {
      await cmd<void>('revealPath', { path: dirPath })
      return { ok: true }
    },

    git: {
      worktreeList: () => Promise.resolve([]),
      worktreeAdd: () => Promise.resolve({ ok: false, error: 'not available' }),
      worktreeRemove: () => Promise.resolve({ ok: false, error: 'not available' }),
      branchSwitch: () => Promise.resolve({ ok: false, error: 'not available' }),
      branchList: () => Promise.resolve([]),
      baseBranchList: () => Promise.resolve([]),
      repoStatus: () => Promise.resolve({}),
      fileDiff: () => Promise.resolve(''),
      scanRepos: () => Promise.resolve([]),
      review: {
        list: () => Promise.resolve([]),
        diff: () => Promise.resolve(''),
        stage: () => Promise.resolve({ ok: false, error: 'not available' }),
        unstage: () => Promise.resolve({ ok: false, error: 'not available' }),
        revert: () => Promise.resolve({ ok: false, error: 'not available' }),
        revParse: () => Promise.resolve(''),
        commit: () => Promise.resolve({ ok: false, error: 'not available' }),
        commitContext: () => Promise.resolve({}),
        push: () => Promise.resolve({ ok: false, error: 'not available' }),
        shipInfo: () => Promise.resolve({}),
        createPr: () => Promise.resolve({ ok: false, error: 'not available' }),
      },
    },

    terminal: {
      cwd: () => Promise.resolve(''),
      dispose: () => notAvailable('terminal'),
      resize: noop,
      start: () => Promise.resolve(''),
      write: noop,
      onData: () => noop,
      onExit: () => noop,
    },

    onClosePreviewRequested: () => noop,
    onOpenUpdatesRequested: () => noop,
    onDeepLink: () => noop,
    signalDeepLinkReady: () => Promise.resolve(),
    onWindowStateChanged: () => noop,
    onFocusSession: () => noop,
    onNotificationAction: () => noop,
    onPreviewFileChanged: () => noop,
    onBackendExit: () => noop,
    onConnectionApplied: () => noop,
    onPowerResume: () => noop,
    onBootProgress: () => noop,

    getBootstrapState: () => Promise.resolve(null),
    resetBootstrap: () => Promise.resolve(),
    repairBootstrap: () => Promise.resolve(),
    cancelBootstrap: () => Promise.resolve(),
    onBootstrapEvent: () => noop,

    getVersion: () => Promise.resolve('0.17.0-tauri'),
    getRemoteDisplayReason: () => Promise.resolve(null),

    uninstall: {
      summary: () => Promise.resolve({ removableItems: [], totalSize: 0 }),
      run: () => Promise.resolve({ ok: false, error: 'not available in Tauri shell' }),
    },

    updates: {
      check: () => Promise.resolve({ available: false }),
      apply: () => Promise.resolve({ ok: false }),
      getBranch: () => Promise.resolve('stable'),
      setBranch: () => Promise.resolve(),
      onProgress: () => noop,
    },

    themes: {
      fetchMarketplace: () => Promise.resolve(null),
      searchMarketplace: () => Promise.resolve([]),
    },
  }

  console.log('[tauri-bridge] window.hermesDesktop installed (Tauri shell)')
}

// Auto-install on module import (side-effect)
installDesktopBridge()
