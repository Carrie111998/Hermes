import type { RegistryBackendRequestScope } from './connection-config'
import type { NativeTokenSet } from './native-oauth'
import type { NativeTokenStoreIo } from './native-token-store'

export function createSecretStorageOauth(deps: Record<string, any>): Record<string, any> {
  let {
    BrowserWindow,
    DEFAULT_FETCH_TIMEOUT_MS,
    LEGACY_OAUTH_PARTITION,
    app,
    buildGatewayWsUrlWithTicket,
    cookiesHaveLiveSession,
    cookiesHavePrivyAccessToken,
    cookiesHavePrivySession,
    cookiesHaveSession,
    dialog,
    downloadViaTokenToFile,
    electronNet,
    ensureBackend,
    ensureRegistryBackend,
    fetchJson,
    filenameFromContentDisposition,
    fs,
    gatewayFilePath,
    gatewayFileRequestPaths,
    http,
    https,
    installWindowRendererLifecycle,
    isNotFoundError,
    loadNativeTokenSet,
    mainWindow,
    nativeRefreshUrl,
    normalizeRemoteBaseUrl,
    oauthSession,
    parseDataUrlToBuffer,
    parseTokenResponse,
    path,
    pathForRegistryBackendRequest,
    pathWithGlobalRemoteProfile,
    persistNativeTokenSet,
    profileRouteOptions,
    protocol,
    pumpStreamToFile,
    rememberLog,
    resolveGatedDownloadAuth,
    resolveJsonBody,
    resolveOauthPartition,
    resolveRemoteBackend,
    resolveTimeoutMs,
    safeStorage,
    screen,
    serializeJsonBody,
    session,
    setJsonRequestHeaders,
    startHermes,
    tokenNeedsRefresh,
    withTransientRetries,
    writeSecretFileAtomic,
    encryptDesktopSecret,
    decryptDesktopSecret,
    rememberRemoteWsHeaders,
    headersForRemoteRequest,
    readDesktopConnectionConfig,
    readDesktopConnectionsRegistry,
    resolveGatewayFileBackend: resolveGatewayFileBackendRaw
  } = deps
  const resolveGatewayFileBackend = resolveGatewayFileBackendRaw as <T>(...args: any[]) => any


  // ---------------------------------------------------------------------------

  const OAUTH_SESSION_PARTITION = LEGACY_OAUTH_PARTITION

  function getOauthSession() {
    if (oauthSession || !app.isReady()) {
      return oauthSession
    }

    oauthSession = session.fromPartition(OAUTH_SESSION_PARTITION)

    return oauthSession
  }

  // Per-connection cookie jars (#92183). A NON-primary v2 registry remote with
  // cookie auth rides its own partition so two registered gateways can never
  // evict — or be handed — each other's session cookies (Chromium jars ignore
  // the port, so two dashboards on one VPN host used to collide in the shared
  // jar above). The primary / v1 remote / cloud / portal flows keep the legacy
  // shared partition; see oauth-partition.ts for the full rules.
  const oauthSessionsByPartition = new Map()

  function resolveOauthPartitionForUrl(url) {
    try {
      return resolveOauthPartition(url, {
        registry: readDesktopConnectionsRegistry(),
        v1RemoteUrl: readDesktopConnectionConfig()?.remote?.url
      })
    } catch {
      // A broken registry read must never take cookie auth down with it.
      return OAUTH_SESSION_PARTITION
    }
  }

  function getOauthSessionForUrl(url) {
    const partition = resolveOauthPartitionForUrl(url)

    if (partition === OAUTH_SESSION_PARTITION) {
      return getOauthSession()
    }

    if (!app.isReady()) {
      return null
    }

    let sess = oauthSessionsByPartition.get(partition)

    if (!sess) {
      sess = session.fromPartition(partition)
      oauthSessionsByPartition.set(partition, sess)
    }

    return sess
  }

  // Cold-start cookie-jar warm-up. A `persist:` partition materialized via
  // session.fromPartition() loads its on-disk cookie store LAZILY: the very first
  // cookies.get() on a fresh cold start can resolve BEFORE the jar has finished
  // hydrating from disk and return an empty array — even though the user is
  // signed in. That false-negative used to make hasLiveOauthSession() report
  // "not signed in", which on the initial boot path (startHermes → the renderer's
  // single-shot boot() with no retry) surfaced as the "Hermes couldn't start"
  // OAuth overlay that vanishes the instant the user clicks Retry.
  //
  // We force the store to hydrate once, up front: flushStorageData() then a
  // throwaway cookies.get(). The promise is memoized so every caller awaits the
  // same single warm-up. Best-effort — any error resolves so we fall back to the
  // live read (which then does its own bounded re-check).
  // Memoized per PARTITION: per-connection jars (#92183) hydrate independently.
  const oauthCookieWarmups = new Map()

  function warmOauthCookieStore(url?) {
    const partition = resolveOauthPartitionForUrl(url)
    const pending = oauthCookieWarmups.get(partition)

    if (pending) {
      return pending
    }

    const warmup = (async () => {
      const sess = getOauthSessionForUrl(url)

      if (!sess) {
        // App not ready yet — don't memoize a no-op; let a later call retry.
        oauthCookieWarmups.delete(partition)

        return
      }

      try {
        // flushStorageData() forces Chromium to reconcile the in-memory cookie
        // monster with the on-disk SQLite store; the subsequent get() then reads
        // a populated jar rather than racing the lazy first-access load.
        sess.flushStorageData?.()
        await sess.cookies.get({})
      } catch {
        // Best effort; the real read below re-checks with bounded retries.
      }
    })()

    oauthCookieWarmups.set(partition, warmup)

    return warmup
  }

  // Bare + prefixed variants of the session cookies live in
  // connection-config.ts (cookiesHaveSession / cookiesHaveLiveSession). See
  // that module for details.

  async function hasOauthSessionCookie(baseUrl) {
    const sess = getOauthSessionForUrl(baseUrl)

    if (!sess) {
      return false
    }

    const parsed = new URL(baseUrl)

    try {
      // Query by URL so the cookie jar applies Domain/Path/Secure scoping for us.
      const cookies = await sess.cookies.get({ url: baseUrl })

      return cookiesHaveSession(cookies)
    } catch {
      // Fall back to a host match if the URL query path errors.
      try {
        const cookies = await sess.cookies.get({ domain: parsed.hostname })

        return cookiesHaveSession(cookies)
      } catch {
        return false
      }
    }
  }

  // Like hasOauthSessionCookie, but returns true when EITHER a live access-token
  // cookie OR a (longer-lived) refresh-token cookie is present. This is the right
  // "is the user signed in at all?" check: an expired AT with a live RT is still
  // a connectable session because the gateway rotates a fresh AT server-side on
  // the next authenticated request. Gating on the AT alone forces a needless full
  // re-login every ~15 min. Used for the Settings "connected" indicator and as a
  // cheap early-out before attempting a network round-trip in resolveRemoteBackend.
  async function hasLiveOauthSession(baseUrl) {
    const sess = getOauthSessionForUrl(baseUrl)

    if (!sess) {
      return false
    }

    const parsed = new URL(baseUrl)

    const readLive = async () => {
      try {
        const cookies = await sess.cookies.get({ url: baseUrl })

        return cookiesHaveLiveSession(cookies)
      } catch {
        try {
          const cookies = await sess.cookies.get({ domain: parsed.hostname })

          return cookiesHaveLiveSession(cookies)
        } catch {
          return false
        }
      }
    }

    // First read against the (possibly still-hydrating) jar.
    if (await readLive()) {
      return true
    }

    // Cold-start false-negative guard. A `persist:` partition's cookie store
    // loads lazily, so the FIRST read on a fresh boot can come back empty even
    // for a signed-in user — the exact race that produced the transient "Hermes
    // couldn't start / not signed in" overlay that Retry always cleared. Before
    // trusting a negative, force the store to hydrate and re-read a couple of
    // times with a short backoff. A genuinely signed-out user still resolves
    // false quickly (≤ ~180ms); a signed-in user racing the load now wins.
    await warmOauthCookieStore(baseUrl)

    for (const delayMs of [30, 60, 90]) {
      if (await readLive()) {
        return true
      }

      await new Promise(resolve => setTimeout(resolve, delayMs))
    }

    return readLive()
  }

  async function clearOauthSession(baseUrl) {
    const sess = getOauthSessionForUrl(baseUrl)

    if (!sess) {
      return
    }

    try {
      const cookies = await sess.cookies.get(baseUrl ? { url: baseUrl } : {})
      await Promise.all(
        cookies.map(c => {
          const scheme = c.secure ? 'https' : 'http'
          const cookieUrl = `${scheme}://${c.domain.replace(/^\./, '')}${c.path || '/'}`

          return sess.cookies.remove(cookieUrl, c.name).catch(() => undefined)
        })
      )
    } catch {
      // Best effort — a stale cookie self-expires anyway.
    }
  }

  // Open a gateway login window in the OAuth session partition, resolving once
  // the access-token cookie appears (login done) or rejecting if the user closes
  // the window first. The window navigates through the IDP and back to
  // /auth/callback, which sets the session cookies on the partition; we poll the
  // cookie jar rather than try to read the HttpOnly value.
  //
  // `silent` selects the URL the window loads, which decides interactive-vs-silent:
  //   - silent=false (default): load ``/login`` — the public interstitial that
  //     renders the "Log in with X" provider chooser. This is the interactive
  //     remote-gateway login the settings UI drives.
  //   - silent=true: load the PROTECTED root ``/`` instead. ``/login`` is a public
  //     route, so loading it NEVER triggers the gate's auto-SSO and always shows
  //     the chooser. Loading a protected page with no session cookie makes the
  //     gate run ``_auto_sso_response``: single registered provider + a live
  //     portal session in this partition → a silent 302 through
  //     ``/auth/login`` → portal ``/oauth/authorize`` (auto-approves org members)
  //     → ``/auth/callback``, which sets the gateway cookie with NO interactive
  //     prompt. This is the per-agent cloud cascade (decisions.md Q5).
  function openOauthLoginWindow(baseUrl, { silent = false } = {}) {
    return new Promise((resolve, reject) => {
      if (!app.isReady()) {
        reject(new Error('Desktop is not ready to start an OAuth login.'))

        return
      }

      const sess = getOauthSessionForUrl(baseUrl)

      if (!sess) {
        reject(new Error('OAuth session partition is unavailable.'))

        return
      }

      let settled = false
      let win = null
      let pollTimer = null
      let revealTimer = null

      const finish = err => {
        if (settled) {
          return
        }

        settled = true

        if (pollTimer) {
          clearInterval(pollTimer)
        }

        if (revealTimer) {
          clearTimeout(revealTimer)
        }

        try {
          if (win && !win.isDestroyed()) {
            win.destroy()
          }
        } catch {
          // window already torn down
        }

        if (err) {
          reject(err)
        } else {
          resolve({ baseUrl, ok: true })
        }
      }

      const checkCookie = async () => {
        if (settled) {
          return
        }

        if (await hasOauthSessionCookie(baseUrl)) {
          finish(null)
        }
      }

      try {
        win = new BrowserWindow({
          width: 520,
          height: 720,
          title: silent ? 'Connecting to Hermes Cloud agent…' : 'Sign in to Hermes gateway',
          autoHideMenuBar: true,
          // Silent cascade: start HIDDEN. The auto-SSO 302 chain completes in
          // well under a second, so the window normally never needs to show. We
          // only reveal it as a fallback if the cascade DOESN'T complete quickly
          // (e.g. the portal session lapsed and the gate fell through to the
          // interactive chooser) — see the reveal timer below.
          show: !silent,
          webPreferences: {
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
            session: sess,
            webSecurity: true
          }
        })
      } catch (error) {
        finish(error instanceof Error ? error : new Error(String(error)))

        return
      }

      // Re-check the cookie jar on every successful navigation (the callback
      // redirect is the moment cookies get set) plus a low-frequency poll as a
      // belt-and-braces fallback for IDPs that finish via in-page JS.
      win.webContents.on('did-navigate', () => void checkCookie())
      win.webContents.on('did-redirect-navigation', () => void checkCookie())
      win.webContents.on('did-frame-navigate', () => void checkCookie())
      // Log-only lifecycle diagnostics: a crashed sign-in renderer is invisible
      // to the window's promise path (it never settles), so without this the
      // failure leaves no trace in desktop.log (#81290 follow-up).
      installWindowRendererLifecycle(win, { kind: 'oauth', callbacks: { log: rememberLog } })
      pollTimer = setInterval(() => void checkCookie(), 750)

      // Silent-mode reveal fallback: if the cascade hasn't settled shortly, the
      // auto-SSO didn't go through silently (no portal session, multi-provider,
      // loop-guard tripped, etc.) and the window is now showing an interactive
      // page. Reveal it so the user can complete sign-in manually rather than
      // staring at nothing. Cleared on finish().
      if (silent && win) {
        revealTimer = setTimeout(() => {
          try {
            if (!settled && win && !win.isDestroyed() && !win.isVisible()) {
              win.show()
            }
          } catch {
            // window torn down
          }
        }, 2500)
      }

      win.on('closed', () => {
        if (!settled) {
          finish(new Error('Login window closed before authentication completed.'))
        }
      })

      // ``next`` is intentionally omitted: the gateway lands on ``/`` after
      // login, which is a valid authenticated page that sets the cookies. We
      // only care that the cookie jar is populated.
      //
      // silent=true loads the protected root so the gate auto-SSOs (no chooser);
      // silent=false loads the public ``/login`` chooser for interactive sign-in.
      const normalizedBase = normalizeRemoteBaseUrl(baseUrl)
      const loginUrl = silent ? `${normalizedBase}/` : `${normalizedBase}/login`
      win.loadURL(loginUrl).catch(error => {
        finish(error instanceof Error ? error : new Error(String(error)))
      })
    })
  }

  // JSON request routed through the OAuth session partition so the HttpOnly
  // session cookie is attached automatically by Electron's net stack. Used for
  // authed REST against a gated gateway, including minting WS tickets.
  function fetchJsonViaOauthSession(url, options: any = {}) {
    return new Promise((resolve, reject) => {
      const sess = getOauthSessionForUrl(url)

      if (!sess) {
        reject(new Error('OAuth session partition is unavailable.'))

        return
      }

      let parsed

      try {
        parsed = new URL(url)
      } catch (error) {
        reject(new Error(`Invalid URL: ${error.message}`))

        return
      }

      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
        reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

        return
      }

      const body = serializeJsonBody(options.body)
      const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)

      const request = electronNet.request({
        method: options.method || 'GET',
        url,
        session: sess,
        useSessionCookies: true,
        redirect: 'follow'
      } as any)

      setJsonRequestHeaders(request)

      for (const [name, value] of Object.entries({ ...headersForRemoteRequest(url), ...(options.headers || {}) })) {
        request.setHeader(name, String(value))
      }

      let timedOut = false

      const timer = setTimeout(() => {
        timedOut = true

        try {
          request.abort()
        } catch {
          // already finished
        }

        reject(new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`))
      }, timeoutMs)

      request.on('response', res => {
        const chunks = []
        res.on('data', chunk => chunks.push(Buffer.from(chunk)))
        res.on('end', () => {
          if (timedOut) {
            return
          }

          clearTimeout(timer)
          const text = Buffer.concat(chunks).toString('utf8')
          const statusCode = res.statusCode || 500

          if (statusCode >= 400) {
            const err = new Error(`${statusCode}: ${text || ''}`) as any
            err.statusCode = statusCode
            reject(err)

            return
          }

          if (!text) {
            resolve(null)

            return
          }

          const looksHtml = /^\s*<(?:!doctype|html)/i.test(text)
          const contentType = String(res.headers['content-type'] || res.headers['Content-Type'] || '')

          if (looksHtml || contentType.includes('text/html')) {
            reject(new Error(`Expected JSON from ${url} but got HTML (status ${statusCode}).`))

            return
          }

          try {
            resolve(JSON.parse(text))
          } catch {
            reject(new Error(`Invalid JSON from ${url} (status ${statusCode}): ${text.slice(0, 200)}`))
          }
        })
      })
      request.on('error', error => {
        if (timedOut) {
          return
        }

        clearTimeout(timer)
        reject(error)
      })

      if (body) {
        request.write(body)
      }

      request.end()
    })
  }

  // ---------------------------------------------------------------------------
  // RFC 8252 native-app tokens (system-browser + loopback + PKCE).
  //
  // Unlike the cookie flow, the native flow hands the desktop opaque bearer
  // tokens it holds itself: the access token authenticates REST via
  // ``Authorization: Bearer`` (which the gateway gate now accepts) and mints WS
  // tickets the same way, so NO browser session cookie or embedded webview is
  // involved. Tokens are persisted encrypted at rest via Electron ``safeStorage``
  // (OS keychain) keyed by gateway base URL, and refreshed via
  // ``/auth/native/refresh`` before expiry. This is the desktop half of the
  // feature; the server half lives in hermes_cli/dashboard_auth/native_flow.py.
  // ---------------------------------------------------------------------------

  // In-memory cache of decrypted native tokens, keyed by normalized base URL.
  // Backed by the encrypted on-disk store so it survives restarts.
  const _nativeTokens = new Map<string, NativeTokenSet>()

  function _nativeTokenStorePath() {
    // Co-located with the connection config under userData; one JSON file mapping
    // baseUrl → { encoding, value } safeStorage payloads.
    return path.join(app.getPath('userData'), 'native-oauth-tokens.json')
  }

  // The electron-coupled half of the token store: safeStorage encryption plus the
  // userData file. native-token-store.ts owns the serialization/parse round trip
  // so it can be tested without an Electron runtime.
  function _nativeTokenStoreIo(): NativeTokenStoreIo {
    return {
      encrypt: encryptDesktopSecret,
      decrypt: decryptDesktopSecret,
      readStoreText: () => fs.readFileSync(_nativeTokenStorePath(), 'utf8'),
      readBackupStoreText: () => fs.readFileSync(`${_nativeTokenStorePath()}.bak`, 'utf8'),
      writeStoreText: (text: string) => {
        const target = _nativeTokenStorePath()
        const backup = `${target}.bak`
        fs.mkdirSync(path.dirname(target), { recursive: true })

        // Keep the last complete predecessor before publishing the replacement.
        // Both writes use temp-file + rename; an interrupted write therefore
        // leaves either the old primary, the old backup, or a complete new file.
        if (fs.existsSync(target)) {
          const current = fs.readFileSync(target, 'utf8')
          try {
            const parsed = JSON.parse(current)
            if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
              writeSecretFileAtomic(backup, current, { encoding: 'utf8' })
            }
          } catch {
            // Preserve an existing valid backup when the primary is corrupt.
          }
        }
        writeSecretFileAtomic(target, text, { encoding: 'utf8' })
      },
      rememberLog
    }
  }

  function _persistNativeTokens(baseUrl: string, tokens: NativeTokenSet | null) {
    persistNativeTokenSet(baseUrl, tokens, _nativeTokenStoreIo())
  }

  function _loadNativeTokens(baseUrl: string): NativeTokenSet | null {
    const cached = _nativeTokens.get(baseUrl)

    if (cached) {
      return cached
    }

    const tokens = loadNativeTokenSet(baseUrl, _nativeTokenStoreIo())

    if (tokens) {
      _nativeTokens.set(baseUrl, tokens)
    }

    return tokens
  }

  function _storeNativeTokens(baseUrl: string, tokens: NativeTokenSet) {
    // Publish first. A failed atomic write must not leave memory claiming a
    // credential that a fresh process cannot recover.
    _persistNativeTokens(baseUrl, tokens)
    _nativeTokens.set(baseUrl, tokens)
  }

  function _clearNativeTokens(baseUrl: string) {
    _persistNativeTokens(baseUrl, null)
    _nativeTokens.delete(baseUrl)
  }

  // True when we hold native bearer tokens for this gateway (the native-flow
  // analogue of hasLiveOauthSession's cookie check).
  function hasNativeSession(baseUrl: string): boolean {
    return _loadNativeTokens(baseUrl) !== null
  }

  // POST JSON WITHOUT the OAuth cookie partition — used for the native token +
  // refresh exchanges, which are cookieless by design. Thin wrapper over
  // fetchJson (no token) so it shares timeout/JSON handling.
  function postJsonNoAuth(url: string, body: unknown, opts: any = {}) {
    // resolveJsonBody passes the object through UNCHANGED — fetchJson owns
    // JSON.stringify. Pre-stringifying here double-encodes the body (a JSON
    // string inside a JSON string), which the gateway's Pydantic model rejects
    // with a 422 "Input should be a valid dictionary" (the native
    // /auth/native/token + /auth/native/refresh legs both go through here).
    return fetchJson(url, null, { method: 'POST', body: resolveJsonBody(body), ...opts })
  }

  // Return a valid native access token for baseUrl, refreshing via
  // /auth/native/refresh if the stored one is at/near expiry. Returns null when
  // there are no tokens or the refresh is terminally rejected (caller re-logins).
  async function ensureNativeAccessToken(baseUrl: string): Promise<string | null> {
    const tokens = _loadNativeTokens(baseUrl)

    if (!tokens) {
      return null
    }

    if (!tokenNeedsRefresh(tokens, Math.floor(Date.now() / 1000))) {
      return tokens.accessToken
    }

    if (!tokens.refreshToken) {
      // Access token expired and no RT to rotate — force re-login.
      _clearNativeTokens(baseUrl)

      return null
    }

    try {
      const body = await postJsonNoAuth(
        nativeRefreshUrl(baseUrl),
        { refresh_token: tokens.refreshToken, provider: tokens.provider },
        { timeoutMs: 10_000 }
      )

      const rotated = parseTokenResponse(body)
      _storeNativeTokens(baseUrl, rotated)

      return rotated.accessToken
    } catch (error: any) {
      // A 401 means the RT is dead (session_expired) — drop tokens so the UI
      // prompts a fresh native login. A 503/transient keeps them for a retry.
      if (error && error.statusCode === 401) {
        _clearNativeTokens(baseUrl)

        return null
      }

      throw error
    }
  }

  // OAuth-session download that streams the response body straight to a
  // user-selected destination (via finalizeGatewayDownload). The connect timeout
  // is cleared once the response headers arrive.
  function downloadViaOauthSessionToFile(url, ctx, options: any = {}) {
    return new Promise((resolve, reject) => {
      const sess = getOauthSessionForUrl(url)

      if (!sess) {
        reject(new Error('OAuth session partition is unavailable.'))

        return
      }

      let parsed

      try {
        parsed = new URL(url)
      } catch (error) {
        reject(new Error(`Invalid URL: ${error.message}`))

        return
      }

      if (parsed.protocol !== 'http:' && parsed.protocol !== 'https:') {
        reject(new Error(`Unsupported Hermes backend URL protocol: ${parsed.protocol}`))

        return
      }

      const timeoutMs = resolveTimeoutMs(options.timeoutMs, DEFAULT_FETCH_TIMEOUT_MS)

      const request = electronNet.request({
        method: 'GET',
        url,
        session: sess,
        useSessionCookies: true,
        redirect: 'follow'
      } as any)

      let settled = false

      const timer = setTimeout(() => {
        if (settled) {
          return
        }

        settled = true

        try {
          request.abort()
        } catch {
          // already finished
        }

        reject(new Error(`Timed out connecting to Hermes backend after ${timeoutMs}ms`))
      }, timeoutMs)

      request.on('response', res => {
        if (settled) {
          return
        }

        // Response headers arrived — cancel the connect timeout so it can't abort
        // the stream while the save dialog is open or bytes are still flowing.
        settled = true
        clearTimeout(timer)
        finalizeGatewayDownload(res, res.statusCode || 500, res.headers || {}, {
          ...ctx,
          abort: () => {
            try {
              request.abort()
            } catch {
              // already finished
            }
          }
        }).then(resolve, reject)
      })
      request.on('error', error => {
        if (settled) {
          return
        }

        settled = true
        clearTimeout(timer)
        reject(error)
      })
      request.end()
    })
  }

  // Shared tail for both transports: validate status, pick a filename, prompt the
  // save dialog, then stream the (still-unconsumed) response body to the chosen
  // destination. On an HTTP error the status code is attached so saveGatewayFile
  // can trigger the 404-only compatibility fallback.
  async function finalizeGatewayDownload(res, statusCode, headers, ctx: any = {}) {
    if (statusCode >= 400) {
      const message = await readGatewayErrorText(res)
      const error: any = new Error(`${statusCode}: ${message}`)
      error.statusCode = statusCode
      throw error
    }

    const disposition = headers['content-disposition'] || headers['Content-Disposition']
    const filename = filenameFromContentDisposition(disposition) || ctx.suggested || ctx.fallbackName

    const result = await dialog.showSaveDialog(mainWindow, {
      defaultPath: filename,
      title: 'Save File'
    })

    if (result.canceled || !result.filePath) {
      ctx.abort?.()

      return { canceled: true, saved: false }
    }

    try {
      await pumpStreamToFile(res, result.filePath, {
        createWriteStream: (destPath: string) => fs.createWriteStream(destPath),
        unlink: (destPath: string) => fs.promises.unlink(destPath)
      })
    } catch (error) {
      ctx.abort?.()
      throw error
    }

    return { path: result.filePath, saved: true }
  }

  // Read a bounded amount of an error response body for the thrown message.
  function readGatewayErrorText(res): Promise<string> {
    return new Promise(resolve => {
      const chunks = []
      let total = 0

      res.on('data', chunk => {
        if (total >= 500) {
          return
        }

        const buffer = Buffer.from(chunk)

        total += buffer.length
        chunks.push(buffer)
      })
      res.on('end', () => resolve(Buffer.concat(chunks).toString('utf8').slice(0, 500)))
      res.on('error', () => resolve(Buffer.concat(chunks).toString('utf8').slice(0, 500)))
    })
  }

  interface GatewayFileConnection extends RegistryBackendRequestScope {
    authMode?: 'oauth' | 'token'
    baseUrl: string
    token?: null | string
  }

  interface GatewayFileSaveContext {
    fallbackName: string
    suggested: string
  }

  interface GatewayFileSavePayload {
    connectionId?: unknown
    path?: unknown
    profile?: unknown
    suggestedName?: unknown
  }

  async function gatedFileAuth(connection: GatewayFileConnection) {
    const nativeAt =
      connection.authMode === 'oauth' ? await ensureNativeAccessToken(connection.baseUrl).catch(() => null) : null

    return resolveGatedDownloadAuth(connection.authMode, nativeAt, connection.token)
  }

  function gatewayFileRequestPath(
    connection: GatewayFileConnection,
    connectionId: null | string,
    profile: null | string,
    requestPath: string
  ) {
    return connectionId
      ? pathForRegistryBackendRequest(requestPath, profile, connection)
      : pathWithGlobalRemoteProfile(requestPath, profile, profileRouteOptions(profile))
  }

  async function saveGatewayFile(payload: GatewayFileSavePayload = {}) {
    const filePath = gatewayFilePath(payload.path)

    if (!filePath) {
      throw new Error('Missing gateway file path')
    }

    const { connection, connectionId, profile } = await resolveGatewayFileBackend<GatewayFileConnection>(payload, {
      ensureLegacy: ensureBackend,
      ensureRegistry: ensureRegistryBackend
    })

    const suggested = String(payload.suggestedName || '').trim()
    const fallbackName = path.basename(filePath) || suggested || 'download'
    const ctx = { suggested, fallbackName }

    const requestPaths = gatewayFileRequestPaths(filePath, requestPath =>
      gatewayFileRequestPath(connection, connectionId, profile, requestPath)
    )

    const url = `${connection.baseUrl}${requestPaths.download}`

    try {
      const auth = await gatedFileAuth(connection)

      if (auth.kind === 'bearer') {
        return await downloadViaTokenToFile(url, auth.token, ctx, { bearer: auth.token })
      }

      if (auth.kind === 'cookie') {
        return await downloadViaOauthSessionToFile(url, ctx)
      }

      return await downloadViaTokenToFile(url, auth.token, ctx)
    } catch (error) {
      // Desktop and the remote gateway update independently. A gateway predating
      // /api/fs/download 404s here; fall back (ONLY on 404) to the older capped
      // data-URL route so downloads keep working against older backends.
      if (isNotFoundError(error)) {
        return await saveGatewayFileViaDataUrl(connection, requestPaths.dataUrl, ctx)
      }

      throw error
    }
  }

  // Compatibility fallback: fetch the file through the capped
  // `/api/fs/read-data-url` route, decode it, and save. Bounded by the gateway's
  // data-URL cap, so it only serves smaller files — enough to keep older gateways
  // working until they gain the streaming route.
  async function saveGatewayFileViaDataUrl(
    connection: GatewayFileConnection,
    requestPath: string,
    ctx: GatewayFileSaveContext
  ) {
    const url = `${connection.baseUrl}${requestPath}`
    const auth = await gatedFileAuth(connection)
    let json: unknown

    if (auth.kind === 'bearer') {
      json = await fetchJson(url, null, { bearer: auth.token })
    } else if (auth.kind === 'cookie') {
      json = await fetchJsonViaOauthSession(url)
    } else {
      json = await fetchJson(url, auth.token)
    }

    const dataUrl =
      json && typeof json === 'object' && 'dataUrl' in json && typeof json.dataUrl === 'string' ? json.dataUrl : ''

    if (!dataUrl) {
      throw new Error('Gateway returned no file data')
    }

    const buffer = parseDataUrlToBuffer(dataUrl)
    const filename = ctx.suggested || ctx.fallbackName

    const result = await dialog.showSaveDialog(mainWindow, {
      defaultPath: filename,
      title: 'Save File'
    })

    if (result.canceled || !result.filePath) {
      return { canceled: true, saved: false }
    }

    await fs.promises.writeFile(result.filePath, buffer)

    return { path: result.filePath, saved: true }
  }

  // Mint a single-use WS ticket for a gated gateway. Returns the ticket string.
  // Prefers a native bearer token (cookieless RFC 8252 flow) when present,
  // falling back to the OAuth cookie partition otherwise.
  // Throws (with statusCode 401) if the session cookie is missing/expired —
  // callers treat that as "needs re-login".
  // Transient transport blips (brief host unreachable, 5xx, timeouts) are retried
  // a few times before failing — those 1-3s flaps were promoting into the
  // full-screen "couldn't start" lockout on reconnect.
  async function mintGatewayWsTicket(baseUrl, headers = {}) {
    return withTransientRetries(async () => {
      // Native flow: mint the ticket with the bearer token, no cookie involved.
      const nativeAt = await ensureNativeAccessToken(baseUrl).catch(() => null)

      if (nativeAt) {
        const body = (await fetchJson(`${baseUrl}/api/auth/ws-ticket`, null, {
          method: 'POST',
          timeoutMs: 8_000,
          bearer: nativeAt,
          headers
        })) as any

        const ticket = body?.ticket

        if (!ticket || typeof ticket !== 'string') {
          throw new Error('Gateway did not return a WS ticket.')
        }

        return ticket
      }

      const body = (await fetchJsonViaOauthSession(`${baseUrl}/api/auth/ws-ticket`, {
        method: 'POST',
        timeoutMs: 8_000,
        headers
      })) as any

      const ticket = body?.ticket

      if (!ticket || typeof ticket !== 'string') {
        throw new Error('Gateway did not return a WS ticket.')
      }

      return ticket
    })
  }

  // Build a fresh WS URL for the *current* connection. Critical for reconnects:
  // OAuth WS tickets are single-use with a ~30s TTL, so the ticket baked into
  // the cached connection's wsUrl is stale on the second connect. The renderer
  // calls this immediately before every gateway.connect() so each WS upgrade
  // carries a freshly-minted ticket. For local/token connections this just
  // reuses the static token (no minting needed).
  async function freshGatewayWsUrl(profile) {
    // Mint for the requested profile's backend, NOT always the primary. The
    // renderer re-mints right before every gateway.connect(); when swapping to a
    // pooled profile we must return THAT backend's ws URL, otherwise the connect
    // silently lands back on the primary (default) backend and writes sessions to
    // the wrong profile's DB. A null/empty profile resolves to the primary, so
    // legacy callers and single-profile users are unchanged.
    const connection = await ensureBackend(profile)

    if (connection.authMode === 'oauth') {
      const ticket = await mintGatewayWsTicket(connection.baseUrl, connection.headers)
      const wsUrl = buildGatewayWsUrlWithTicket(connection.baseUrl, ticket)

      rememberRemoteWsHeaders(wsUrl, connection.headers)

      return wsUrl
    }

    // Local/token: the cached wsUrl already carries the (long-lived) token.
    rememberRemoteWsHeaders(connection.wsUrl, connection.headers)

    return connection.wsUrl
  }

  // --- Hermes Cloud discovery + silent per-agent sign-in (cloud-auto-discovery
  // Phase 3) ---------------------------------------------------------------
  //
  // The "cloud" connection mode lets a user sign in to the Nous portal ONCE in
  // the OAuth session partition, then (a) discover their hosted agents and (b)
  // connect to any of them with no second interactive sign-in. Both ride the one
  // portal session cookie living in `persist:hermes-remote-oauth`:
  //   - discovery  → GET {portal}/api/agents over the partition-bound net; the
  //     portal session cookie authenticates it (NAS Phase 2.5 accepts the cookie).
  //   - cascade    → opening an agent's own /login in the same partition hits the
  //     portal's silent auto-approve (org member, existing session) and 302s back
  //     with that agent's session cookie — no prompt. Each agent still completes
  //     its own PKCE exchange; SSO removes the human click, not a security check.

  // Canonical Nous portal base URL, overridable for staging/dev. Mirrors the CLI
  // convention (hermes_cli/auth.py DEFAULT_NOUS_PORTAL_URL + the same env names)
  // so a single override flips every Hermes surface to the same portal.
  const DEFAULT_NOUS_PORTAL_URL = 'https://portal.nousresearch.com'

  function resolvePortalBaseUrl() {
    const raw = process.env.HERMES_PORTAL_BASE_URL || process.env.NOUS_PORTAL_BASE_URL || DEFAULT_NOUS_PORTAL_URL

    return String(raw).trim().replace(/\/+$/, '')
  }

  // Whether the OAuth partition currently holds a live Nous portal session — the
  // credential that powers both discovery and the silent cascade. The portal
  // authenticates via PRIVY, not the Hermes gateway session cookies, so this
  // checks for the `privy-token` cookie on the portal host (NOT
  // hasLiveOauthSession, which looks for hermes_session_at/rt that the portal
  // never sets). See connection-config.ts cookiesHavePrivySession.
  //
  // Mirrors hasLiveOauthSession's cold-start guard (#73495): a `persist:`
  // partition's cookie store hydrates lazily, so the FIRST read on a fresh boot
  // can come back empty even for a signed-in user. The renderer checks Cloud
  // status exactly once on entering cloud mode, so a single false-negative here
  // used to clear the discovered agent list and demand a re-login that a plain
  // retry would have avoided. Warm the store and re-read with a short backoff
  // before trusting a negative.
  async function hasLivePortalSession() {
    const sess = getOauthSession()

    if (!sess) {
      return false
    }

    const portalBaseUrl = resolvePortalBaseUrl()
    const parsed = new URL(portalBaseUrl)

    const readPortal = async () => {
      try {
        const cookies = await sess.cookies.get({ url: portalBaseUrl })

        return cookiesHavePrivySession(cookies)
      } catch {
        try {
          const cookies = await sess.cookies.get({ domain: parsed.hostname })

          return cookiesHavePrivySession(cookies)
        } catch {
          return false
        }
      }
    }

    if (await readPortal()) {
      return true
    }

    await warmOauthCookieStore()

    for (const delayMs of [30, 60, 90]) {
      if (await readPortal()) {
        return true
      }

      await new Promise(resolve => setTimeout(resolve, delayMs))
    }

    return readPortal()
  }

  // Whether the jar holds the short-lived Privy ACCESS token — the exact cookie
  // `/api/agents` validates. hasLivePortalSession() answers "signed in at all?"
  // (renewal material counts); this answers "can discovery succeed right now?".
  async function hasPortalAccessToken() {
    const sess = getOauthSession()

    if (!sess) {
      return false
    }

    const portalBaseUrl = resolvePortalBaseUrl()
    const parsed = new URL(portalBaseUrl)

    try {
      const cookies = await sess.cookies.get({ url: portalBaseUrl })

      return cookiesHavePrivyAccessToken(cookies)
    } catch {
      try {
        const cookies = await sess.cookies.get({ domain: parsed.hostname })

        return cookiesHavePrivyAccessToken(cookies)
      } catch {
        return false
      }
    }
  }

  // Bounded silent renewal of the short-lived Privy access token (#73495).
  //
  // After a Desktop restart the long-lived `privy-session` / `privy-refresh-token`
  // cookies routinely survive while the ~1h `privy-token` access cookie has
  // expired. Discovery then 401s and the only offered recovery used to be a full
  // interactive re-login — even though the persisted refresh material can mint a
  // fresh access token with no user action: loading any portal page runs the
  // Privy client, which rotates a new `privy-token` from the refresh session.
  //
  // This drives exactly that, headlessly: a hidden window on the portal root in
  // the OAuth partition, polled until the access cookie lands, torn down on a
  // bounded timeout. Never shown — if renewal can't complete silently the caller
  // falls back to the interactive needsCloudLogin path. The in-flight promise is
  // shared so concurrent discovery + cascade calls ride one renewal.
  let portalAccessRenewal: Promise<boolean> | null = null

  function renewPortalAccessSilently() {
    if (portalAccessRenewal) {
      return portalAccessRenewal
    }

    portalAccessRenewal = (async () => {
      if (!app.isReady()) {
        return false
      }

      const sess = getOauthSession()

      if (!sess) {
        return false
      }

      // No renewal material at all → nothing to renew; interactive login is
      // genuinely required.
      if (!(await hasLivePortalSession())) {
        return false
      }

      if (await hasPortalAccessToken()) {
        return true
      }

      const portalBaseUrl = resolvePortalBaseUrl()

      return await new Promise<boolean>(resolve => {
        let settled = false
        let win = null
        let pollTimer = null
        let deadlineTimer = null

        const finish = (ok: boolean) => {
          if (settled) {
            return
          }

          settled = true

          if (pollTimer) {
            clearInterval(pollTimer)
          }

          if (deadlineTimer) {
            clearTimeout(deadlineTimer)
          }

          try {
            if (win && !win.isDestroyed()) {
              win.destroy()
            }
          } catch {
            // window already torn down
          }

          rememberLog(`[cloud] silent portal access renewal ${ok ? 'succeeded' : 'did not complete'}`)
          resolve(ok)
        }

        const checkCookie = async () => {
          if (settled) {
            return
          }

          if (await hasPortalAccessToken()) {
            finish(true)
          }
        }

        try {
          win = new BrowserWindow({
            width: 520,
            height: 720,
            show: false,
            title: 'Renewing Hermes Cloud session…',
            autoHideMenuBar: true,
            webPreferences: {
              contextIsolation: true,
              nodeIntegration: false,
              sandbox: true,
              session: sess,
              webSecurity: true
            }
          })
        } catch {
          finish(false)

          return
        }

        win.webContents.on('did-navigate', () => void checkCookie())
        win.webContents.on('did-redirect-navigation', () => void checkCookie())
        win.webContents.on('did-frame-navigate', () => void checkCookie())
        installWindowRendererLifecycle(win, { kind: 'portal-renew', callbacks: { log: rememberLog } })
        pollTimer = setInterval(() => void checkCookie(), 500)
        // Hard deadline: this window is never revealed, so an unrenewable session
        // (revoked refresh token, portal down) must resolve false rather than
        // hang the discovery call behind an invisible window.
        deadlineTimer = setTimeout(() => finish(false), 12_000)

        win.on('closed', () => finish(false))

        win.loadURL(portalBaseUrl).catch(() => finish(false))
      })
    })().finally(() => {
      portalAccessRenewal = null
    }) as Promise<boolean>

    return portalAccessRenewal
  }

  // Drive a one-time interactive portal sign-in in the OAuth partition. Unlike
  // openOauthLoginWindow (which targets a gateway's /login), this lands on the
  // portal itself so the resulting session cookie is portal-scoped — the cookie
  // that authenticates discovery AND is reused for every silent per-agent
  // cascade. Resolves once the portal session cookie appears.
  function openPortalLoginWindow() {
    const portalBaseUrl = resolvePortalBaseUrl()

    return new Promise((resolve, reject) => {
      if (!app.isReady()) {
        reject(new Error('Desktop is not ready to start a Hermes Cloud sign-in.'))

        return
      }

      const sess = getOauthSession()

      if (!sess) {
        reject(new Error('OAuth session partition is unavailable.'))

        return
      }

      let settled = false
      let win = null
      let pollTimer = null

      const finish = err => {
        if (settled) {
          return
        }

        settled = true

        if (pollTimer) {
          clearInterval(pollTimer)
        }

        try {
          if (win && !win.isDestroyed()) {
            win.destroy()
          }
        } catch {
          // window already torn down
        }

        if (err) {
          reject(err)
        } else {
          resolve({ portalBaseUrl, ok: true })
        }
      }

      const checkCookie = async () => {
        if (settled) {
          return
        }

        // A live portal (Privy) session cookie means sign-in completed.
        if (await hasLivePortalSession()) {
          finish(null)
        }
      }

      try {
        win = new BrowserWindow({
          width: 520,
          height: 720,
          title: 'Sign in to Hermes Cloud',
          autoHideMenuBar: true,
          webPreferences: {
            contextIsolation: true,
            nodeIntegration: false,
            sandbox: true,
            session: sess,
            webSecurity: true
          }
        })
      } catch (error) {
        finish(error instanceof Error ? error : new Error(String(error)))

        return
      }

      win.webContents.on('did-navigate', () => void checkCookie())
      win.webContents.on('did-redirect-navigation', () => void checkCookie())
      win.webContents.on('did-frame-navigate', () => void checkCookie())
      // Log-only lifecycle diagnostics, same rationale as the OAuth window:
      // a crashed portal sign-in renderer never settles the promise, so the
      // failure would otherwise leave no trace in desktop.log (#81290
      // follow-up).
      installWindowRendererLifecycle(win, { kind: 'portal', callbacks: { log: rememberLog } })
      pollTimer = setInterval(() => void checkCookie(), 750)

      win.on('closed', () => {
        if (!settled) {
          finish(new Error('Sign-in window closed before authentication completed.'))
        }
      })

      // Land on the portal root; any authenticated portal page sets the session
      // cookie. We only care that the partition cookie jar is populated.
      win.loadURL(portalBaseUrl).catch(error => {
        finish(error instanceof Error ? error : new Error(String(error)))
      })
    })
  }

  // Discover the hosted (Hermes Cloud) agents the signed-in user can see. Calls
  // the NAS trimmed-summary endpoint over the partition-bound net, so the portal
  // session cookie is attached automatically (no bearer needed — NAS accepts the
  // cookie). Returns { agents } on success, or { needsOrgSelection: true, orgs }
  // when the user belongs to multiple orgs and hasn't picked one yet (NAS 409
  // org_selection_required). Pass `org` (a slug/id from a prior org list) to
  // scope discovery to that org. Throws a needsCloudLogin-tagged error when no
  // portal session is present.
  async function discoverCloudAgents(org?: string) {
    const portalBaseUrl = resolvePortalBaseUrl()

    if (!(await hasLivePortalSession())) {
      const err = new Error(
        'You are not signed in to Hermes Cloud. Open Settings → Gateway, choose Hermes Cloud, and sign in.'
      ) as any

      err.needsCloudLogin = true
      throw err
    }

    // Renewable session present but the short-lived access token `/api/agents`
    // validates is gone (typical after a restart — `privy-token` is ~1h,
    // `privy-session`/`privy-refresh-token` last ~30 days). Renew silently up
    // front instead of letting the request 401 into a re-login demand (#73495).
    if (!(await hasPortalAccessToken())) {
      await renewPortalAccessSilently()
    }

    const orgQuery = org ? `?org=${encodeURIComponent(org)}` : ''
    let body

    const fetchAgents = () =>
      fetchJsonViaOauthSession(`${portalBaseUrl}/api/agents${orgQuery}`, {
        method: 'GET',
        timeoutMs: 15_000
      })

    try {
      body = (await fetchAgents()) as any
    } catch (initialError) {
      let error = initialError as any

      // A 401 with renewal material still in the jar: attempt ONE bounded silent
      // renewal and retry, so a lapsed access token doesn't surface as a full
      // interactive re-login while a 30-day refresh session sits unused. Only a
      // rejected/failed renewal (or a second 401 on genuinely fresh access)
      // falls through to needsCloudLogin.
      if (error && error.statusCode === 401 && (await renewPortalAccessSilently())) {
        try {
          body = (await fetchAgents()) as any
        } catch (retryError) {
          error = retryError
        }
      }

      if (body === undefined) {
        // A 401 means the portal session lapsed (and silent renewal could not
        // recover it) — surface it as a re-login, not a generic failure.
        if (error && error.statusCode === 401) {
          const err = new Error(
            'Your Hermes Cloud session has expired. Open Settings → Gateway and sign in again.'
          ) as any

          err.needsCloudLogin = true
          err.cause = error
          throw err
        }

        // A 409 means we're a multi-org user who hasn't picked an org. The body
        // carries the user's org list; surface it so the renderer shows a picker
        // and re-calls discovery with the chosen org. (fetchJsonViaOauthSession
        // throws on >=400 with err.statusCode + err.message "409: <json body>".)
        if (error && error.statusCode === 409) {
          const orgs = parseOrgSelectionError(error)

          if (orgs) {
            return { needsOrgSelection: true, orgs }
          }
        }

        throw error
      }
    }

    return { agents: trimCloudAgents(body), org: trimCloudOrg(body?.org) }
  }

  // Project a NAS response org ({ id, slug, name, isPersonal }) to the trimmed
  // shape the renderer persists, or null when absent/malformed.
  function trimCloudOrg(org) {
    if (!org || typeof org !== 'object' || typeof org.id !== 'string') {
      return null
    }

    return {
      id: org.id,
      slug: typeof org.slug === 'string' ? org.slug : null,
      name: typeof org.name === 'string' ? org.name : org.id,
      isPersonal: Boolean(org.isPersonal),
      role: typeof org.role === 'string' ? org.role : 'MEMBER'
    }
  }

  // Extract the org list from a 409 org_selection_required error body. The error
  // message is "409: <raw json>" (see fetchJsonViaOauthSession); parse defensively
  // and return null if it isn't the shape we expect (caller then rethrows).
  function parseOrgSelectionError(error) {
    const msg = String(error?.message || '')
    const jsonStart = msg.indexOf('{')

    if (jsonStart < 0) {
      return null
    }

    let parsed

    try {
      parsed = JSON.parse(msg.slice(jsonStart))
    } catch {
      return null
    }

    if (parsed?.error !== 'org_selection_required' || !Array.isArray(parsed.orgs)) {
      return null
    }

    return parsed.orgs
      .filter(o => o && typeof o === 'object' && typeof o.id === 'string')
      .map(o => ({
        id: o.id,
        slug: typeof o.slug === 'string' ? o.slug : null,
        name: typeof o.name === 'string' ? o.name : o.id,
        isPersonal: Boolean(o.isPersonal),
        role: typeof o.role === 'string' ? o.role : 'MEMBER'
      }))
  }

  // Project NAS's agent rows to the trimmed DTO the renderer consumes.
  function trimCloudAgents(body) {
    const agents = Array.isArray(body?.agents) ? body.agents : []

    return agents
      .filter(a => a && typeof a === 'object' && typeof a.id === 'string')
      .map(a => ({
        id: a.id,
        name: typeof a.name === 'string' ? a.name : a.id,
        status: typeof a.status === 'string' ? a.status : 'unknown',
        dashboardUrl: typeof a.dashboardUrl === 'string' ? a.dashboardUrl : null,
        dashboardGatewayState: typeof a.dashboardGatewayState === 'string' ? a.dashboardGatewayState : 'unknown'
      }))
  }


  return {
    OAUTH_SESSION_PARTITION,
    getOauthSession,
    oauthSessionsByPartition,
    resolveOauthPartitionForUrl,
    getOauthSessionForUrl,
    oauthCookieWarmups,
    warmOauthCookieStore,
    hasOauthSessionCookie,
    hasLiveOauthSession,
    clearOauthSession,
    openOauthLoginWindow,
    fetchJsonViaOauthSession,
    _nativeTokens,
    _nativeTokenStorePath,
    _nativeTokenStoreIo,
    _persistNativeTokens,
    _loadNativeTokens,
    _storeNativeTokens,
    _clearNativeTokens,
    hasNativeSession,
    postJsonNoAuth,
    ensureNativeAccessToken,
    downloadViaOauthSessionToFile,
    finalizeGatewayDownload,
    readGatewayErrorText,
    gatedFileAuth,
    gatewayFileRequestPath,
    saveGatewayFile,
    saveGatewayFileViaDataUrl,
    mintGatewayWsTicket,
    freshGatewayWsUrl,
    DEFAULT_NOUS_PORTAL_URL,
    resolvePortalBaseUrl,
    hasLivePortalSession,
    hasPortalAccessToken,
    portalAccessRenewal,
    renewPortalAccessSilently,
    openPortalLoginWindow,
    discoverCloudAgents,
    trimCloudOrg,
    parseOrgSelectionError,
    trimCloudAgents
  }
}
