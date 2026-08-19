/**
 * IX Agency desktop status + provisioning helpers (Electron port of the
 * intelliverse-x-desktop Tauri shell's vpn.rs / mcp.rs / hermes.rs / updater):
 *
 *  - Update manifest polling: an S3-hosted latest.json (Tauri-updater shape
 *    or a plain {version,url,notes}) is compared against the running app
 *    version; newer ⇒ a NON-BLOCKING "Update available" button in the IX
 *    strip + tray. Electron adaptation: clicking opens the release URL (no
 *    signed electron-updater feed exists for this app — no fake states).
 *  - VPN deep status: green ONLY when the tunnel is up AND a fresh WireGuard
 *    handshake is visible AND the exit-IP check confirms egress through the
 *    company EC2 VPN exit (18.209.130.93 — vpn.intelli-verse-x.ai).
 *    `wg show` usually needs root on macOS; when unreadable, egress match
 *    still counts as connected and the detail says the handshake was
 *    unverifiable (same tradeoff the Tauri shell documents).
 *  - admin-mcp lamp: GET /healthz (reachable) + authenticated tools/list
 *    (green). 401 ⇒ grey; unreachable ⇒ red.
 *  - Cognito S2S: client_credentials grant (basic auth, form body — the
 *    DeepTutor pattern) + REAL JWKS RS256 verification with node:crypto
 *    (issuer, kid, signature, expiry, token_use, client_id, scope).
 *
 * Pure logic with injected fetch/exec so everything is unit-testable.
 */
import { execFileSync } from 'node:child_process'
import crypto from 'node:crypto'

/* ── Update manifest ────────────────────────────────────────────────────────── */

/** Compare dotted versions; returns >0 when a > b (non-numeric parts = 0). */
export function compareVersions(a: string, b: string): number {
  const pa = String(a ?? '')
    .replace(/^v/, '')
    .split('.')
    .map(n => parseInt(n, 10) || 0)

  const pb = String(b ?? '')
    .replace(/^v/, '')
    .split('.')
    .map(n => parseInt(n, 10) || 0)

  for (let i = 0; i < Math.max(pa.length, pb.length); i++) {
    const diff = (pa[i] ?? 0) - (pb[i] ?? 0)

    if (diff !== 0) {
      return diff
    }
  }

  return 0
}

export interface UpdateStatus {
  updateAvailable: boolean
  /** True when clicking Update genuinely installs in place (NSIS, signed
   *  Squirrel.Mac, AppImage). Unset/false ⇒ clicking opens a download link. */
  inPlace?: boolean
  currentVersion: string
  latestVersion: string
  /** Where the Update button sends the user (platform artifact or release page). */
  url: string
  notes: string
  detail: string
}

/** Platform key in a Tauri-updater latest.json (e.g. darwin-aarch64). */
export function updatePlatformKey(platform = process.platform, arch = process.arch): string {
  const os = platform === 'win32' ? 'windows' : platform === 'darwin' ? 'darwin' : 'linux'
  const cpu = arch === 'arm64' ? 'aarch64' : 'x86_64'

  return `${os}-${cpu}`
}

/** Parse a latest.json manifest (Tauri-updater shape or plain {version,url}). */
export function parseUpdateManifest(
  manifest: unknown,
  currentVersion: string,
  platformKey = updatePlatformKey()
): UpdateStatus {
  const m = (manifest ?? {}) as Record<string, unknown>
  const latestVersion = String(m.version ?? '').trim()

  if (!latestVersion) {
    return {
      updateAvailable: false,
      currentVersion,
      latestVersion: '',
      url: '',
      notes: '',
      detail: 'Manifest has no version field'
    }
  }

  const platforms = (m.platforms ?? {}) as Record<string, { url?: string }>
  const url = String(platforms?.[platformKey]?.url ?? m.url ?? '').trim()
  const updateAvailable = compareVersions(latestVersion, currentVersion) > 0

  return {
    updateAvailable,
    currentVersion,
    latestVersion,
    url,
    notes: String(m.notes ?? ''),
    detail: updateAvailable
      ? `Update available: ${currentVersion} → ${latestVersion}`
      : `Up to date (${currentVersion}; manifest ${latestVersion})`
  }
}

export async function fetchUpdateStatus(
  manifestUrl: string,
  currentVersion: string,
  fetchImpl: typeof fetch = fetch
): Promise<UpdateStatus> {
  try {
    const res = await fetchImpl(manifestUrl, { signal: AbortSignal.timeout(15_000), cache: 'no-store' })

    if (!res.ok) {
      return {
        updateAvailable: false,
        currentVersion,
        latestVersion: '',
        url: '',
        notes: '',
        detail: `Manifest fetch failed: HTTP ${res.status} (publish latest.json at ${manifestUrl})`
      }
    }

    return parseUpdateManifest(await res.json(), currentVersion)
  } catch (error) {
    return {
      updateAvailable: false,
      currentVersion,
      latestVersion: '',
      url: '',
      notes: '',
      detail: `Manifest fetch failed: ${error instanceof Error ? error.message : String(error)}`
    }
  }
}

/* ── VPN deep status (handshake + exit-IP) ──────────────────────────────────── */

/** wg-easy NEW-account EC2 exit IP (intelli-verse-kube-infra/usa-vpn). */
export const DEFAULT_VPN_EXIT_IP = '18.209.130.93'

const HANDSHAKE_FRESH_SECS = 3 * 60

const WG_PATHS = ['/opt/homebrew/bin/wg', '/usr/local/bin/wg', '/usr/bin/wg', '/bin/wg']

/**
 * Best-effort `wg show <tunnel> latest-handshakes` WITHOUT privileges.
 * wg-quick tunnels usually need root to query on macOS, so null means
 * "unreadable", not "no handshake".
 */
export function wgHandshakeAgeSecs(
  tunnelName: string,
  exec: (cmd: string, args: string[]) => string = (cmd, args) =>
    execFileSync(cmd, args, { encoding: 'utf8', timeout: 4000 })
): null | number {
  for (const wg of WG_PATHS) {
    try {
      const out = exec(wg, ['show', tunnelName, 'latest-handshakes'])
      const ts = parseInt(out.trim().split(/\s+/).pop() ?? '', 10)

      if (!Number.isFinite(ts) || ts === 0) {
        return null
      }

      return Math.max(0, Math.floor(Date.now() / 1000) - ts)
    } catch {
      continue
    }
  }

  return null
}

export async function fetchEgressIp(fetchImpl: typeof fetch = fetch): Promise<string> {
  const res = await fetchImpl('https://checkip.amazonaws.com', { signal: AbortSignal.timeout(8000) })

  if (!res.ok) {
    throw new Error(`exit-IP check failed: HTTP ${res.status}`)
  }

  return (await res.text()).trim()
}

export interface VpnDeepStatus {
  /** 'connected' = green; 'connecting'/'degraded' = amber; rest grey/red. */
  state: 'connected' | 'connecting' | 'degraded' | 'disconnected' | 'error' | 'unavailable'
  tunnelUp: boolean
  handshakeAgeSecs: null | number
  egressIp: null | string
  expectedExitIp: string
  detail: string
}

/**
 * Combine the three signals. Green requires tunnel up + egress via the VPN
 * exit + a fresh handshake when one is readable (unreadable handshakes are
 * reported in the detail, not treated as failure — macOS `wg show` needs
 * root for wg-quick tunnels).
 */
export function combineVpnStatus(input: {
  tunnelUp: boolean
  handshakeAgeSecs: null | number
  egressIp: null | string
  egressError?: string
  expectedExitIp: string
}): VpnDeepStatus {
  const { tunnelUp, handshakeAgeSecs, egressIp, egressError, expectedExitIp } = input

  if (!tunnelUp) {
    return {
      state: 'disconnected',
      tunnelUp,
      handshakeAgeSecs,
      egressIp,
      expectedExitIp,
      detail: 'Tunnel is down'
    }
  }

  if (!egressIp) {
    return {
      state: 'error',
      tunnelUp,
      handshakeAgeSecs,
      egressIp,
      expectedExitIp,
      detail: egressError || 'Exit-IP check failed'
    }
  }

  const egressMatch = egressIp === expectedExitIp
  const handshakeFresh = handshakeAgeSecs !== null && handshakeAgeSecs <= HANDSHAKE_FRESH_SECS
  const handshakeUnreadable = handshakeAgeSecs === null

  if (egressMatch && handshakeFresh) {
    return {
      state: 'connected',
      tunnelUp,
      handshakeAgeSecs,
      egressIp,
      expectedExitIp,
      detail: `Egress ${egressIp} == VPN exit; handshake ${handshakeAgeSecs}s ago`
    }
  }

  if (egressMatch && handshakeUnreadable) {
    return {
      state: 'connected',
      tunnelUp,
      handshakeAgeSecs,
      egressIp,
      expectedExitIp,
      detail: `Egress ${egressIp} == VPN exit (handshake unreadable without root — egress check is authoritative)`
    }
  }

  if (egressMatch) {
    return {
      state: 'degraded',
      tunnelUp,
      handshakeAgeSecs,
      egressIp,
      expectedExitIp,
      detail: `Egress matches but last handshake was ${handshakeAgeSecs}s ago (stale)`
    }
  }

  return {
    state: 'degraded',
    tunnelUp,
    handshakeAgeSecs,
    egressIp,
    expectedExitIp,
    detail: `Tunnel up but egress is ${egressIp}, not the VPN exit ${expectedExitIp}`
  }
}

/** Sanity-check imported WireGuard config material (Tauri shell's rule). */
export function looksLikeWireGuardConf(contents: string): boolean {
  return contents.includes('[Interface]') && contents.includes('[Peer]') && contents.includes('PrivateKey')
}

/* ── admin-mcp gateway lamp ─────────────────────────────────────────────────── */

export interface McpLampStatus {
  state: 'green' | 'grey' | 'red'
  reachable: boolean
  authenticated: boolean
  toolCount: number
  detail: string
}

export async function fetchMcpLampStatus(
  gatewayUrl: string,
  gatewayToken: string,
  fetchImpl: typeof fetch = fetch
): Promise<McpLampStatus> {
  const base = String(gatewayUrl || '').replace(/\/+$/, '')

  if (!base) {
    return { state: 'grey', reachable: false, authenticated: false, toolCount: 0, detail: 'No gateway URL configured' }
  }

  try {
    const health = await fetchImpl(`${base}/healthz`, { signal: AbortSignal.timeout(8000) })

    if (!health.ok) {
      return {
        state: 'red',
        reachable: false,
        authenticated: false,
        toolCount: 0,
        detail: `/healthz returned HTTP ${health.status}`
      }
    }
  } catch (error) {
    return {
      state: 'red',
      reachable: false,
      authenticated: false,
      toolCount: 0,
      detail: `Unreachable: ${error instanceof Error ? error.message : String(error)}`
    }
  }

  if (!gatewayToken) {
    return {
      state: 'grey',
      reachable: true,
      authenticated: false,
      toolCount: 0,
      detail: 'Reachable, but no gateway bearer token configured'
    }
  }

  try {
    const res = await fetchImpl(`${base}/`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${gatewayToken}` },
      body: JSON.stringify({ jsonrpc: '2.0', id: 1, method: 'tools/list' }),
      signal: AbortSignal.timeout(10_000)
    })

    if (res.status === 401 || res.status === 403) {
      return {
        state: 'grey',
        reachable: true,
        authenticated: false,
        toolCount: 0,
        detail: `Token rejected (HTTP ${res.status}) — update the gateway bearer token`
      }
    }

    if (!res.ok) {
      return {
        state: 'red',
        reachable: true,
        authenticated: false,
        toolCount: 0,
        detail: `tools/list returned HTTP ${res.status}`
      }
    }

    const body = await res.json()
    const toolCount = Array.isArray(body?.result?.tools) ? body.result.tools.length : 0

    return {
      state: 'green',
      reachable: true,
      authenticated: true,
      toolCount,
      detail: `Reachable + authenticated (${toolCount} gateway tools)`
    }
  } catch (error) {
    return {
      state: 'red',
      reachable: true,
      authenticated: false,
      toolCount: 0,
      detail: `tools/list failed: ${error instanceof Error ? error.message : String(error)}`
    }
  }
}

/* ── Cognito S2S (client_credentials + real JWKS verification) ──────────────── */

// Same defaults the intelliverse-x-desktop Tauri shell ships (the client id
// is not a secret; the SECRET is always user-supplied here).
export const DEFAULT_COGNITO_OAUTH2_URL = 'https://aicartx.auth.us-east-1.amazoncognito.com/oauth2/token'

export const DEFAULT_COGNITO_CLIENT_ID = '7i9clgl5c6dv2qk755ssrrlo80'

export const DEFAULT_COGNITO_SCOPE = 'yourapi/all'

export async function fetchCognitoToken(
  oauth2Url: string,
  clientId: string,
  clientSecret: string,
  scope: string,
  fetchImpl: typeof fetch = fetch
): Promise<string> {
  const res = await fetchImpl(oauth2Url, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/x-www-form-urlencoded',
      Authorization: `Basic ${Buffer.from(`${clientId}:${clientSecret}`).toString('base64')}`
    },
    body: new URLSearchParams({ grant_type: 'client_credentials', scope }).toString(),
    signal: AbortSignal.timeout(15_000)
  })

  if (!res.ok) {
    const body = await res.text().catch(() => '')

    throw new Error(`Cognito token endpoint returned HTTP ${res.status}${body ? `: ${body.slice(0, 200)}` : ''}`)
  }

  const json = await res.json()

  if (typeof json?.access_token !== 'string' || !json.access_token) {
    throw new Error('Cognito token response has no access_token')
  }

  return json.access_token
}

function b64urlJson(part: string): Record<string, unknown> {
  return JSON.parse(Buffer.from(part, 'base64url').toString('utf8'))
}

/**
 * Real verification of the minted access token — not just "endpoint said
 * 200": RS256 signature against the pool's JWKS, issuer, expiry, token_use,
 * client_id and scope (port of the Tauri shell's verify_cognito_token).
 */
export async function verifyCognitoToken(
  token: string,
  expectedClientId: string,
  expectedScope: string,
  fetchImpl: typeof fetch = fetch
): Promise<string> {
  const parts = token.split('.')

  if (parts.length !== 3) {
    throw new Error('Malformed JWT')
  }

  const header = b64urlJson(parts[0])
  const kid = typeof header.kid === 'string' ? header.kid : ''

  if (!kid) {
    throw new Error('Token has no key id (kid)')
  }

  const payload = b64urlJson(parts[1])
  const iss = typeof payload.iss === 'string' ? payload.iss : ''

  if (!iss.startsWith('https://cognito-idp.')) {
    throw new Error(`Unexpected token issuer: ${iss || '(none)'}`)
  }

  const jwksRes = await fetchImpl(`${iss}/.well-known/jwks.json`, { signal: AbortSignal.timeout(15_000) })

  if (!jwksRes.ok) {
    throw new Error(`JWKS fetch failed: HTTP ${jwksRes.status}`)
  }

  const jwks = await jwksRes.json()
  const jwk = (Array.isArray(jwks?.keys) ? jwks.keys : []).find((k: { kid?: string }) => k?.kid === kid)

  if (!jwk) {
    throw new Error(`Signing key ${kid} not in ${iss} JWKS`)
  }

  const publicKey = crypto.createPublicKey({ key: jwk, format: 'jwk' })
  const signed = Buffer.from(`${parts[0]}.${parts[1]}`)
  const signature = Buffer.from(parts[2], 'base64url')

  if (!crypto.verify('RSA-SHA256', signed, publicKey, signature)) {
    throw new Error('Token signature verification failed')
  }

  const exp = typeof payload.exp === 'number' ? payload.exp : 0
  const now = Math.floor(Date.now() / 1000)

  if (exp <= now) {
    throw new Error('Token is expired')
  }

  if (payload.token_use !== 'access') {
    throw new Error(`token_use is ${String(payload.token_use)}, expected "access"`)
  }

  if (payload.client_id !== expectedClientId) {
    throw new Error('Token client_id does not match the credentials used')
  }

  const scope = typeof payload.scope === 'string' ? payload.scope : ''

  if (expectedScope && !scope.split(/\s+/).includes(expectedScope)) {
    throw new Error(`Token scope "${scope}" does not include requested "${expectedScope}"`)
  }

  return `Signature verified against ${iss} (kid ${kid}), scope "${scope}", expires in ${exp - now}s`
}

/* ── Hermes local init (config generation) ──────────────────────────────────── */

/** Minimal ~/.hermes/config.yaml (subset of hermes-deployment's config.yaml):
 *  LiteLLM gateway as the model provider + the admin-mcp server block. */
export function minimalHermesConfigYaml(litellmUrl: string, gatewayUrl: string): string {
  const base = String(litellmUrl || '').replace(/\/+$/, '')

  return `# Seeded by the Hermes desktop IX Agency setup (minimal). For the full
# config run hermes-deployment/scripts/install-local.sh
model:
  default: "anthropic/claude-opus-4.6"
  provider: "custom"
  base_url: "${/\/v1$/.test(base) ? base : `${base}/v1`}"

mcp_servers:
  admin-mcp:
    url: "${gatewayUrl}"
    timeout: 60
    headers:
      Authorization: "Bearer \${ADMIN_MCP_TOKEN}"

memory:
  enabled: true

context_files:
  enabled: true
`
}

/** Append/replace a KEY=value line in an env-file's contents. */
export function upsertEnvLine(existing: string, key: string, value: string): string {
  const lines = existing.split('\n').filter(line => line.trim() && !line.startsWith(`${key}=`))

  lines.push(`${key}=${value}`)

  return `${lines.join('\n')}\n`
}
