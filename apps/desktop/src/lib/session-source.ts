import { normalize } from '@/lib/text'

const SOURCE_LABELS: Record<string, string> = {
  api_server: 'API',
  bluebubbles: 'iMessage',
  claude: 'Claude',
  cli: 'CLI',
  codex: 'Codex',
  desktop: 'Desktop',
  discord: 'Discord',
  email: 'Email',
  gateway: 'Gateway',
  local: 'Local',
  matrix: 'Matrix',
  mattermost: 'Mattermost',
  qqbot: 'QQ',
  signal: 'Signal',
  slack: 'Slack',
  sms: 'SMS',
  telegram: 'Telegram',
  tui: 'TUI',
  webhook: 'Webhook',
  weixin: 'WeChat',
  whatsapp: 'WhatsApp',
  yuanbao: 'Yuanbao'
}

const SOURCE_ALIASES: Record<string, string[]> = {
  bluebubbles: ['apple messages', 'imessage'],
  claude: ['anthropic', 'claude code', 'claude-code'],
  cli: ['terminal'],
  desktop: ['app', 'gui'],
  local: ['machine'],
  qqbot: ['qq'],
  telegram: ['tg'],
  tui: ['terminal'],
  weixin: ['wechat'],
  whatsapp: ['wa']
}

// Sources that run on the local machine rather than an external messaging
// platform. A handoff *from* one of these isn't a platform origin worth a badge.
// Exported so the recents fetch can keep these in the main list while the
// messaging fetch excludes them.
export const LOCAL_SESSION_SOURCE_IDS = ['claude', 'cli', 'codex', 'desktop', 'gateway', 'local', 'tui']
const LOCAL_SOURCE_IDS = new Set(LOCAL_SESSION_SOURCE_IDS)

// External messaging platforms that each get their own self-managed sidebar
// section (fetched separately from local recents). Mirrors the gateway platform
// adapters; keep in sync with PLATFORM_ICONS in app/messaging/platform-icon.tsx.
export const MESSAGING_SESSION_SOURCE_IDS = [
  'telegram',
  'discord',
  'slack',
  'mattermost',
  'matrix',
  'signal',
  'whatsapp',
  'bluebubbles',
  'homeassistant',
  'email',
  'sms',
  'webhook',
  'api_server',
  'weixin',
  'wecom',
  'qqbot',
  'yuanbao',
  'dingtalk',
  'feishu'
]
const MESSAGING_SOURCE_IDS = new Set(MESSAGING_SESSION_SOURCE_IDS)

/** True when a source id is an external messaging platform (gets its own
 *  sidebar section) rather than a local/CLI/desktop session. */
export function isMessagingSource(source: null | string | undefined): boolean {
  const id = normalizeSessionSource(source)

  return id != null && MESSAGING_SOURCE_IDS.has(id)
}

export function normalizeSessionSource(source: null | string | undefined): string | null {
  return normalize(source) || null
}

/**
 * Resolve the origin messaging platform for a handed-off session. Returns the
 * normalized platform id (e.g. 'telegram') when the session completed a handoff
 * from a real messaging platform, otherwise null. After a handoff the live
 * source is local, so this is what drives the row's origin-platform badge.
 */
export function handoffOriginSource(
  handoffState: null | string | undefined,
  handoffPlatform: null | string | undefined
): string | null {
  if (handoffState !== 'completed') {
    return null
  }

  const id = normalizeSessionSource(handoffPlatform)

  if (!id || LOCAL_SOURCE_IDS.has(id)) {
    return null
  }

  return id
}

// Driver slugs (persisted by hermes_state.detect_session_driver) that map to a
// harness with an existing source label; unknown slugs fall back to the
// generic capitalized rendering in sessionSourceLabel.
const DRIVER_SOURCE_IDS: Record<string, string> = {
  'claude-code': 'claude',
  codex: 'codex'
}

/**
 * Label for the agent that DROVE a local session (recorded at creation from
 * the spawning process environment) — distinct from bridge_provider, which
 * identifies whose transcript a catalog row mirrors.
 */
export function sessionDriverLabel(driver: null | string | undefined): string | null {
  const id = normalizeSessionSource(driver)

  if (!id) {
    return null
  }

  return sessionSourceLabel(DRIVER_SOURCE_IDS[id] ?? id)
}

export function sessionSourceLabel(source: null | string | undefined): string | null {
  const id = normalizeSessionSource(source)

  if (!id) {
    return null
  }

  return SOURCE_LABELS[id] || id.replace(/[_-]+/g, ' ').replace(/\b\w/g, char => char.toUpperCase())
}

export function sessionSourceSearchTerms(source: null | string | undefined): string[] {
  const id = normalizeSessionSource(source)
  const label = sessionSourceLabel(id)

  if (!id) {
    return []
  }

  return [id, label ?? '', ...(SOURCE_ALIASES[id] ?? [])].filter(Boolean)
}

const BRIDGE_MIRROR_STATE_LABELS: Record<string, string> = {
  catalog_only: 'Catalog only',
  continued: 'Continued',
  diverged: 'Diverged',
  failed: 'Failed',
  mirrored: 'Mirrored',
  queued: 'Queued'
}

const BRIDGE_MIRROR_STATE_ALIASES: Record<string, string[]> = {
  catalog_only: ['カタログのみ', '仅目录', '僅目錄'],
  continued: ['継続', '已继续', '已繼續'],
  diverged: ['分岐', '已分歧'],
  failed: ['失敗', '失败'],
  mirrored: ['ミラー済み', '已镜像', '已鏡像'],
  queued: ['待機中', '已排队', '已排隊']
}

export function bridgeProviderLabel(provider: null | string | undefined): string | null {
  return sessionSourceLabel(provider)
}

export function bridgeProviderSearchTerms(provider: null | string | undefined): string[] {
  return sessionSourceSearchTerms(provider)
}

export function bridgeMirrorStateLabel(state: null | string | undefined): string | null {
  const id = normalizeSessionSource(state)

  return id ? (BRIDGE_MIRROR_STATE_LABELS[id] ?? null) : null
}

export function bridgeMirrorStateSearchTerms(state: null | string | undefined): string[] {
  const id = normalizeSessionSource(state)
  const label = bridgeMirrorStateLabel(id)

  return id && label ? [id, label, ...(BRIDGE_MIRROR_STATE_ALIASES[id] ?? [])] : []
}

const BRIDGE_SIDEBAR_STATE_LABELS: Record<string, string> = {
  failed: 'Failed',
  pending: 'Pending',
  retrying: 'Retrying',
  visible: 'Visible in Codex'
}

export function bridgeSidebarStateLabel(state: null | string | undefined): string | null {
  const id = normalizeSessionSource(state)

  return id ? (BRIDGE_SIDEBAR_STATE_LABELS[id] ?? null) : null
}

export function bridgeSidebarStateSearchTerms(state: null | string | undefined): string[] {
  const id = normalizeSessionSource(state)
  const label = bridgeSidebarStateLabel(id)

  return id && label ? ['codex sidebar', id, label] : []
}
