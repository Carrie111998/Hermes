/**
 * IX Agency integration for the desktop app: WireGuard VPN control, the
 * locally-persisted IX settings (portal URL, admin-mcp gateway URL/token,
 * VPN profile path), and the admin-mcp gateway client used by the MCP panel.
 *
 * VPN backend — the company VPN ("usa-vpn", intelli-verse-kube-infra/usa-vpn)
 * is plain WireGuard; each employee holds a .conf profile:
 *
 * - macOS / Linux: `wg-quick up/down <conf>`. wg-quick must run as root; on
 *   macOS we elevate through `osascript … with administrator privileges`
 *   (native admin-password prompt), on Linux we require a passwordless
 *   sudoers rule for wg-quick.
 * - Windows: WireGuard for Windows runs each tunnel as a Windows service.
 *   Connect = `wireguard.exe /installtunnelservice <conf>`, disconnect =
 *   `wireguard.exe /uninstalltunnelservice <name>`; both elevate via a real
 *   UAC prompt (Start-Process -Verb RunAs).
 *
 * There is no fake success path — every failure (not installed, no conf,
 * cancelled prompt, tool error) is surfaced verbatim to the UI.
 *
 * Status is read WITHOUT elevation:
 * - macOS: /var/run/wireguard/<name>.name exists while the tunnel is up (the
 *   artifact wg-quick leaves behind; `wg show` itself needs root).
 * - Linux: /sys/class/net/<name> exists while the tunnel is up.
 * - Windows: `sc.exe query WireGuardTunnel$<name>` works for standard users.
 */
import { execFile, execFileSync } from 'node:child_process'
import fs from 'node:fs'
import path from 'node:path'
import { promisify } from 'node:util'

const execFileAsync = promisify(execFile)

const WG_QUICK_PATHS = ['/opt/homebrew/bin', '/usr/local/bin', '/usr/bin', '/usr/sbin']

/** WireGuard for Windows tunnel names must match this (enforced upstream). */
const WINDOWS_TUNNEL_NAME_RE = /^[a-zA-Z0-9_=+.-]{1,32}$/

const IS_WINDOWS = process.platform === 'win32'

const GATEWAY_TIMEOUT_MS = 20_000

export interface IxAgencySettings {
  /** IX Agency admin portal origin (webview target + skill deep links). */
  portalUrl: string
  /** admin-mcp gateway endpoint (JSON-RPC 2.0 over HTTP). */
  gatewayUrl: string
  /** Bearer token for the admin-mcp gateway (stored via safeStorage). */
  gatewayToken: string
  /** Absolute path to a WireGuard .conf profile (e.g. usa-vpn.conf). */
  vpnConfPath: string
  /** Imported usa-vpn.conf CONTENTS, keychain-backed via safeStorage
   *  (preferred over vpnConfPath; materialized to a 0600 scratch file only
   *  for the duration of each wg-quick invocation). */
  vpnConfSecret: string
  /** Expected VPN egress IP (the Lightsail wg-easy exit). */
  vpnExitIp: string
  /** LiteLLM OpenAI-compatible base URL for the native IX Agency chat. */
  litellmUrl: string
  /** LiteLLM API key for the native chat (stored via safeStorage). */
  litellmKey: string
  /** Extra model ids for the chat picker, comma-separated (user override). */
  customChatModels: string
  /** S3-hosted latest.json the update poller checks (user-publishable). */
  updateManifestUrl: string
  /** Cognito S2S (client_credentials) — the DeepTutor pattern. */
  cognitoOauth2Url: string
  cognitoClientId: string
  /** Stored via safeStorage. */
  cognitoClientSecret: string
  cognitoScope: string
  /** Set after a successful Hermes local init from this app. */
  hermesInitialized: boolean
}

export const DEFAULT_IX_AGENCY_SETTINGS: IxAgencySettings = {
  portalUrl: 'https://admin.intelli-verse-x.ai',
  gatewayUrl: 'https://admin-mcp.intelli-verse-x.ai/',
  gatewayToken: '',
  vpnConfPath: '',
  vpnConfSecret: '',
  vpnExitIp: '18.209.130.93',
  litellmUrl: 'https://litellm.intelli-verse-x.ai',
  litellmKey: '',
  customChatModels: '',
  // electron-updater feed base (CI publishes channel files + installers here
  // — see .github/workflows/desktop-release.yml). A URL ending in .json is
  // treated as a legacy hand-published manifest (poll-and-open-URL only).
  updateManifestUrl: 'https://intelliverse-x-desktop.s3.amazonaws.com/ix-agency',
  cognitoOauth2Url: 'https://aicartx.auth.us-east-1.amazoncognito.com/oauth2/token',
  cognitoClientId: '7i9clgl5c6dv2qk755ssrrlo80',
  cognitoClientSecret: '',
  cognitoScope: 'yourapi/all',
  hermesInitialized: false
}

/** Tunnel name used when the conf is imported (keychain mode). */
export const IX_VPN_TUNNEL = 'usa-vpn'

/** Materialize keychain-stored conf contents to a private 0600 scratch file. */
export function materializeVpnConf(scratchDir: string, contents: string): string {
  const confPath = path.join(scratchDir, `${IX_VPN_TUNNEL}.conf`)

  fs.mkdirSync(scratchDir, { recursive: true, mode: 0o700 })
  fs.writeFileSync(confPath, contents, { mode: 0o600 })

  return confPath
}

export interface IxVpnStatus {
  state: 'connected' | 'connecting' | 'disconnected' | 'unavailable' | 'unknown'
  /** Human-readable explanation (error, missing tooling, interface name…). */
  detail: string
  interfaceName?: string
}

// Set while a wg-quick / wireguard.exe invocation is in flight so status
// polls report "connecting" instead of flapping through disconnected.
let vpnBusy = false

function findWgQuick() {
  for (const dir of WG_QUICK_PATHS) {
    const candidate = path.join(dir, 'wg-quick')

    if (fs.existsSync(candidate)) {
      return candidate
    }
  }

  return null
}

/** Locate wireguard.exe (WireGuard for Windows). */
function findWireGuardExe() {
  const bases = [process.env['ProgramFiles'], process.env['ProgramFiles(x86)'], process.env['ProgramW6432']]

  for (const base of bases) {
    if (!base) {
      continue
    }

    const candidate = path.join(base, 'WireGuard', 'wireguard.exe')

    if (fs.existsSync(candidate)) {
      return candidate
    }
  }

  return null
}

/** Tunnel name as WireGuard derives it: the conf basename without .conf. */
export function ixVpnTunnelName(confPath) {
  return path.basename(String(confPath || '')).replace(/\.conf$/i, '')
}

function shellQuote(value) {
  return `'${String(value).replace(/'/g, `'\\''`)}'`
}

/** Escape a shell command for embedding inside an AppleScript string literal. */
function appleScriptQuote(value) {
  return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"')
}

/** Escape a value for a single-quoted PowerShell string literal. */
function psQuote(value) {
  return `'${String(value).replace(/'/g, "''")}'`
}

/**
 * Run wireguard.exe elevated via a native UAC prompt and propagate its exit
 * code. Start-Process -Verb RunAs cannot capture the elevated process's
 * output, so failures are reported by exit code with a remediation hint.
 */
async function runElevatedWindows(exe, args) {
  const argList = args.map(psQuote).join(',')

  const ps =
    `$p = Start-Process -FilePath ${psQuote(exe)} -ArgumentList @(${argList}) ` +
    `-Verb RunAs -Wait -PassThru; exit $p.ExitCode`

  let exitCode

  try {
    await execFileAsync(
      'powershell.exe',
      ['-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-Command', ps],
      {
        timeout: 120_000,
        windowsHide: true
      }
    )

    return
  } catch (error) {
    const message = String(error?.stderr || error?.message || error)

    if (/canceled by the user|cancelled by the user|0x800704C7|1223/i.test(message)) {
      throw new Error('Cancelled — the UAC elevation prompt was dismissed.')
    }

    exitCode = typeof error?.code === 'number' ? error.code : NaN
  }

  throw new Error(
    `wireguard.exe ${args[0]} failed` +
      (Number.isFinite(exitCode) ? ` (exit code ${exitCode})` : '') +
      `. Run "${exe}" ${args.map(a => `"${a}"`).join(' ')} in an elevated terminal to see the underlying error.`
  )
}

async function runElevated(command, promptText) {
  if (process.platform === 'darwin') {
    const script = `do shell script "${appleScriptQuote(command)}" with administrator privileges with prompt "${appleScriptQuote(promptText)}"`

    try {
      await execFileAsync('osascript', ['-e', script], { timeout: 120_000 })
    } catch (error) {
      const message = String(error?.stderr || error?.message || error)

      if (/user cancell?ed/i.test(message) || /-128/.test(message)) {
        throw new Error('Cancelled — the admin-password prompt was dismissed.')
      }

      // osascript prefixes AppleScript noise; keep the useful tail.
      throw new Error(message.replace(/^execution error:\s*/i, '').trim())
    }

    return
  }

  // Linux: require a passwordless sudoers rule for wg-quick. We do NOT try to
  // capture a sudo password from the GUI.
  try {
    await execFileAsync('sudo', ['-n', 'sh', '-c', command], { timeout: 120_000 })
  } catch (error) {
    const message = String(error?.stderr || error?.message || error)

    if (/a password is required/i.test(message)) {
      throw new Error(
        'sudo needs a password for wg-quick. Add a sudoers rule, e.g.: ' +
          `"${process.env.USER ?? 'youruser'} ALL=(root) NOPASSWD: /usr/bin/wg-quick".`
      )
    }

    throw new Error(message.trim())
  }
}

function notInstalledMessage() {
  if (IS_WINDOWS) {
    return 'WireGuard for Windows is not installed. Install it from https://www.wireguard.com/install/.'
  }

  return 'wg-quick is not installed. Install WireGuard tools first (macOS: `brew install wireguard-tools`).'
}

function validateVpnProfile(confPath) {
  if (!confPath) {
    throw new Error('No VPN profile configured — set the WireGuard .conf path in IX Agency settings.')
  }

  if (!fs.existsSync(confPath)) {
    throw new Error(`VPN profile not found: ${confPath}`)
  }

  const tool = IS_WINDOWS ? findWireGuardExe() : findWgQuick()

  if (!tool) {
    throw new Error(notInstalledMessage())
  }

  if (IS_WINDOWS) {
    const name = ixVpnTunnelName(confPath)

    if (!WINDOWS_TUNNEL_NAME_RE.test(name)) {
      throw new Error(
        `Invalid tunnel name "${name}" — WireGuard for Windows requires the .conf ` +
          `filename to match [a-zA-Z0-9_=+.-]{1,32}. Rename the profile file.`
      )
    }
  }

  return { tool }
}

export async function ixVpnConnect(confPath) {
  const { tool } = validateVpnProfile(confPath)

  vpnBusy = true

  try {
    if (IS_WINDOWS) {
      await runElevatedWindows(tool, ['/installtunnelservice', confPath])

      return
    }

    const cmd = `PATH=${WG_QUICK_PATHS.join(':')}:$PATH ${shellQuote(tool)} up ${shellQuote(confPath)}`
    await runElevated(cmd, 'IX Agency wants to connect the WireGuard VPN.')
  } finally {
    vpnBusy = false
  }
}

export async function ixVpnDisconnect(confPath) {
  const { tool } = validateVpnProfile(confPath)

  vpnBusy = true

  try {
    if (IS_WINDOWS) {
      await runElevatedWindows(tool, ['/uninstalltunnelservice', ixVpnTunnelName(confPath)])

      return
    }

    const cmd = `PATH=${WG_QUICK_PATHS.join(':')}:$PATH ${shellQuote(tool)} down ${shellQuote(confPath)}`
    await runElevated(cmd, 'IX Agency wants to disconnect the WireGuard VPN.')
  } finally {
    vpnBusy = false
  }
}

/** Windows tunnel status via `sc.exe query` (allowed for standard users). */
function windowsVpnStatus(confPath): IxVpnStatus {
  const name = ixVpnTunnelName(confPath)
  const service = `WireGuardTunnel$${name}`
  let out

  try {
    out = execFileSync('sc.exe', ['query', service], { encoding: 'utf8', timeout: 10_000, windowsHide: true })
  } catch (error) {
    // Exit code 1060: the service does not exist — tunnel is down.
    if (error?.status === 1060 || /1060/.test(String(error?.stdout ?? ''))) {
      return { state: 'disconnected', detail: `Tunnel ${name} is down` }
    }

    return { state: 'unknown', detail: `sc.exe query failed: ${String(error?.message ?? error).trim()}` }
  }

  if (/\bRUNNING\b/.test(out)) {
    return { state: 'connected', detail: `Tunnel ${name} up (service ${service})`, interfaceName: name }
  }

  if (/\bSTART_PENDING\b/.test(out)) {
    return { state: 'connecting', detail: `Tunnel service ${service} is starting…` }
  }

  // Service installed but not running: the tunnel is effectively down; a
  // fresh connect reinstalls the service.
  return { state: 'disconnected', detail: `Tunnel ${name} is down (service ${service} not running)` }
}

export function ixVpnStatus(confPath, confAvailable?: boolean): IxVpnStatus {
  if (vpnBusy) {
    return { state: 'connecting', detail: IS_WINDOWS ? 'wireguard.exe is running…' : 'wg-quick is running…' }
  }

  if (!confPath) {
    return {
      state: 'unavailable',
      detail: 'No VPN profile configured. Import usa-vpn.conf in IX Agency settings.'
    }
  }

  // confAvailable=true is passed when the profile lives in the keychain
  // (safeStorage) instead of on disk — the path is then only a tunnel name.
  if (!(confAvailable ?? fs.existsSync(confPath))) {
    return { state: 'unavailable', detail: `Profile not found: ${confPath}` }
  }

  if (IS_WINDOWS ? !findWireGuardExe() : !findWgQuick()) {
    return { state: 'unavailable', detail: notInstalledMessage() }
  }

  if (IS_WINDOWS) {
    return windowsVpnStatus(confPath)
  }

  const name = ixVpnTunnelName(confPath)

  if (process.platform === 'darwin') {
    const nameFile = `/var/run/wireguard/${name}.name`

    if (fs.existsSync(nameFile)) {
      let iface

      try {
        iface = fs.readFileSync(nameFile, 'utf8').trim()
      } catch {
        // Readable-by-root-only edge: existence still means "up".
      }

      return {
        state: 'connected',
        detail: iface ? `Tunnel ${name} up on ${iface}` : `Tunnel ${name} up`,
        interfaceName: iface
      }
    }

    return { state: 'disconnected', detail: `Tunnel ${name} is down` }
  }

  if (fs.existsSync(`/sys/class/net/${name}`)) {
    return { state: 'connected', detail: `Tunnel ${name} up`, interfaceName: name }
  }

  return { state: 'disconnected', detail: `Tunnel ${name} is down` }
}

// ── Settings persistence ────────────────────────────────────────────────────
// Stored in userData/ix-agency.json. The gateway token is a secret, so it is
// written through the caller-provided encrypt/decrypt pair (safeStorage in
// main.ts) instead of plaintext.

export function readIxAgencySettings(filePath, decryptSecret): IxAgencySettings {
  let raw

  try {
    raw = JSON.parse(fs.readFileSync(filePath, 'utf8'))
  } catch {
    return { ...DEFAULT_IX_AGENCY_SETTINGS }
  }

  if (!raw || typeof raw !== 'object') {
    return { ...DEFAULT_IX_AGENCY_SETTINGS }
  }

  return {
    portalUrl: String(raw.portalUrl || '').trim() || DEFAULT_IX_AGENCY_SETTINGS.portalUrl,
    gatewayUrl: String(raw.gatewayUrl || '').trim() || DEFAULT_IX_AGENCY_SETTINGS.gatewayUrl,
    gatewayToken: decryptSecret(raw.gatewayToken) || '',
    vpnConfPath: String(raw.vpnConfPath || '').trim(),
    vpnConfSecret: decryptSecret(raw.vpnConfSecret) || '',
    vpnExitIp: String(raw.vpnExitIp || '').trim() || DEFAULT_IX_AGENCY_SETTINGS.vpnExitIp,
    litellmUrl: String(raw.litellmUrl || '').trim() || DEFAULT_IX_AGENCY_SETTINGS.litellmUrl,
    litellmKey: decryptSecret(raw.litellmKey) || '',
    customChatModels: String(raw.customChatModels || '').trim(),
    updateManifestUrl: String(raw.updateManifestUrl || '').trim() || DEFAULT_IX_AGENCY_SETTINGS.updateManifestUrl,
    cognitoOauth2Url: String(raw.cognitoOauth2Url || '').trim() || DEFAULT_IX_AGENCY_SETTINGS.cognitoOauth2Url,
    cognitoClientId: String(raw.cognitoClientId || '').trim() || DEFAULT_IX_AGENCY_SETTINGS.cognitoClientId,
    cognitoClientSecret: decryptSecret(raw.cognitoClientSecret) || '',
    cognitoScope: String(raw.cognitoScope || '').trim() || DEFAULT_IX_AGENCY_SETTINGS.cognitoScope,
    hermesInitialized: Boolean(raw.hermesInitialized)
  }
}

export function writeIxAgencySettings(filePath, settings: IxAgencySettings, encryptSecret) {
  const payload = {
    portalUrl: settings.portalUrl,
    gatewayUrl: settings.gatewayUrl,
    gatewayToken: settings.gatewayToken ? encryptSecret(settings.gatewayToken) : null,
    vpnConfPath: settings.vpnConfPath,
    vpnConfSecret: settings.vpnConfSecret ? encryptSecret(settings.vpnConfSecret) : null,
    vpnExitIp: settings.vpnExitIp,
    litellmUrl: settings.litellmUrl,
    litellmKey: settings.litellmKey ? encryptSecret(settings.litellmKey) : null,
    customChatModels: settings.customChatModels,
    updateManifestUrl: settings.updateManifestUrl,
    cognitoOauth2Url: settings.cognitoOauth2Url,
    cognitoClientId: settings.cognitoClientId,
    cognitoClientSecret: settings.cognitoClientSecret ? encryptSecret(settings.cognitoClientSecret) : null,
    cognitoScope: settings.cognitoScope,
    hermesInitialized: settings.hermesInitialized
  }

  fs.mkdirSync(path.dirname(filePath), { recursive: true })
  fs.writeFileSync(filePath, `${JSON.stringify(payload, null, 2)}\n`, 'utf8')
}

/** Normalize a saved-settings payload from the renderer (all fields present). */
export function sanitizeIxAgencySettingsInput(input, current: IxAgencySettings): IxAgencySettings {
  const source = input && typeof input === 'object' ? input : {}

  type IxStringSettingKey = Exclude<keyof IxAgencySettings, 'hermesInitialized'>

  const pick = (key: IxStringSettingKey): string =>
    typeof source[key] === 'string' ? String(source[key]).trim() : current[key]

  return {
    portalUrl: pick('portalUrl') || DEFAULT_IX_AGENCY_SETTINGS.portalUrl,
    gatewayUrl: pick('gatewayUrl') || DEFAULT_IX_AGENCY_SETTINGS.gatewayUrl,
    gatewayToken: pick('gatewayToken'),
    vpnConfPath: pick('vpnConfPath'),
    // The conf secret is imported via its own IPC (file picker in main), so
    // renderer settings saves never touch it.
    vpnConfSecret: current.vpnConfSecret,
    vpnExitIp: pick('vpnExitIp') || DEFAULT_IX_AGENCY_SETTINGS.vpnExitIp,
    litellmUrl: pick('litellmUrl') || DEFAULT_IX_AGENCY_SETTINGS.litellmUrl,
    litellmKey: pick('litellmKey'),
    customChatModels: pick('customChatModels'),
    updateManifestUrl: pick('updateManifestUrl') || DEFAULT_IX_AGENCY_SETTINGS.updateManifestUrl,
    cognitoOauth2Url: pick('cognitoOauth2Url') || DEFAULT_IX_AGENCY_SETTINGS.cognitoOauth2Url,
    cognitoClientId: pick('cognitoClientId') || DEFAULT_IX_AGENCY_SETTINGS.cognitoClientId,
    cognitoClientSecret: pick('cognitoClientSecret'),
    cognitoScope: pick('cognitoScope') || DEFAULT_IX_AGENCY_SETTINGS.cognitoScope,
    hermesInitialized: current.hermesInitialized
  }
}

/** What the renderer sees: never the raw token, just whether one is stored. */
export function ixAgencySettingsForRenderer(settings: IxAgencySettings) {
  return {
    portalUrl: settings.portalUrl,
    gatewayUrl: settings.gatewayUrl,
    gatewayTokenSet: Boolean(settings.gatewayToken),
    vpnConfPath: settings.vpnConfPath,
    vpnConfImported: Boolean(settings.vpnConfSecret),
    vpnExitIp: settings.vpnExitIp,
    litellmUrl: settings.litellmUrl,
    litellmKeySet: Boolean(settings.litellmKey),
    customChatModels: settings.customChatModels,
    updateManifestUrl: settings.updateManifestUrl,
    cognitoOauth2Url: settings.cognitoOauth2Url,
    cognitoClientId: settings.cognitoClientId,
    cognitoClientSecretSet: Boolean(settings.cognitoClientSecret),
    cognitoScope: settings.cognitoScope,
    hermesInitialized: settings.hermesInitialized
  }
}

// ── admin-mcp gateway client ────────────────────────────────────────────────
// JSON-RPC 2.0 over HTTP with a bearer token. The renderer falls back to its
// bundled registry snapshot when this throws (no token / unreachable).

/** Raw JSON-RPC 2.0 call against the admin-mcp gateway; returns `result`. */
export async function ixGatewayRpc(gatewayUrl, gatewayToken, method, params?: Record<string, unknown>) {
  if (!gatewayUrl) {
    throw new Error('No gateway URL configured.')
  }

  if (!gatewayToken) {
    throw new Error('No gateway token configured.')
  }

  const res = await fetch(gatewayUrl, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${gatewayToken}`
    },
    body: JSON.stringify({ jsonrpc: '2.0', id: 1, method, params }),
    signal: AbortSignal.timeout(GATEWAY_TIMEOUT_MS)
  })

  if (!res.ok) {
    throw new Error(`Gateway HTTP ${res.status}${res.status === 401 ? ' — check the bearer token' : ''}`)
  }

  const body = await res.json()

  if (body?.error) {
    throw new Error(`Gateway RPC error: ${body.error.message}`)
  }

  return body?.result
}

async function callIxGatewayTool(gatewayUrl, gatewayToken, tool, args = {}) {
  const result = await ixGatewayRpc(gatewayUrl, gatewayToken, 'tools/call', { name: tool, arguments: args })
  const text = result?.content?.find(chunk => chunk?.type === 'text')?.text ?? ''

  if (result?.isError) {
    throw new Error(text || 'Gateway tool call failed')
  }

  try {
    return JSON.parse(text)
  } catch {
    return text
  }
}

/**
 * Live MCP tile list via the gateway's admin_mcp_directory tool. Throws when
 * no token is configured or the gateway is unreachable; the renderer then
 * shows its bundled registry snapshot instead.
 */
export async function fetchIxMcpDirectory(settings: IxAgencySettings) {
  const out = await callIxGatewayTool(settings.gatewayUrl, settings.gatewayToken, 'admin_mcp_directory')
  const servers = Array.isArray(out?.servers) ? out.servers : []

  const tiles = servers.map(server => {
    let domain = ''

    try {
      domain = new URL(server.mcpUrl).hostname
    } catch {
      domain = String(server.mcpUrl || '')
    }

    return {
      id: String(server.id || ''),
      label: String(server.label || server.id || ''),
      blurb: String(server.blurb || ''),
      group: String(server.category || server.group || ''),
      mcpUrl: String(server.mcpUrl || ''),
      domain,
      mcpAuthHint: String(server.mcpAuthHint || ''),
      hasDefaultToken: Boolean(server.hasDefaultToken)
    }
  })

  return { detail: `Live from ${settings.gatewayUrl} (${tiles.length} MCP servers in scope)`, tiles }
}
