import fs from 'node:fs'
import path from 'node:path'

export const HERMES_PET_SESSION_VIEW_SCHEMA_VERSION = 1
export const HERMES_PET_SESSION_VIEW_MAX_RECENT = 64

const SAFE_IDENTIFIER = /^[A-Za-z0-9._~-]{1,256}$/

export interface HermesPetSessionView {
  sessionID: string
  profile: string
  viewedAt: number
}

export interface HermesPetSessionViewSnapshot {
  schemaVersion: typeof HERMES_PET_SESSION_VIEW_SCHEMA_VERSION
  updatedAt: number
  producerPID: number
  current: HermesPetSessionView | null
  recentViews: HermesPetSessionView[]
}

export interface HermesPetSessionViewPayload {
  sessionID: null | string
  profile?: null | string
}

export type HermesPetSessionViewResult =
  | { ok: true }
  | { error: 'invalid-runtime' | 'invalid-session-view' | 'write-failed' | 'not-focused'; ok: false }

export function safePetSessionIdentifier(value: unknown): string | null {
  if (typeof value !== 'string') {
    return null
  }

  const normalized = value.trim()

  return SAFE_IDENTIFIER.test(normalized) ? normalized : null
}

function normalizedProfile(value: unknown): string | null {
  const normalized = typeof value === 'string' ? value.trim() : ''

  return safePetSessionIdentifier(normalized || 'default')
}

function normalizedView(value: unknown): HermesPetSessionView | null {
  if (!value || typeof value !== 'object') {
    return null
  }

  const candidate = value as Partial<HermesPetSessionView>
  const sessionID = safePetSessionIdentifier(candidate.sessionID)
  const profile = normalizedProfile(candidate.profile)
  const viewedAt = Number(candidate.viewedAt)

  if (!sessionID || !profile || !Number.isFinite(viewedAt) || viewedAt <= 0) {
    return null
  }

  return { sessionID, profile, viewedAt }
}

/**
 * Parse a marker without trusting it. A malformed or future marker is treated
 * as absent so Pet can fail closed instead of acknowledging a completion from
 * an unverified Desktop process.
 */
export function readHermesPetSessionViewSnapshot(targetPath: string): HermesPetSessionViewSnapshot | null {
  try {
    const parsed: unknown = JSON.parse(fs.readFileSync(targetPath, 'utf8'))

    if (!parsed || typeof parsed !== 'object') {
      return null
    }

    const value = parsed as Partial<HermesPetSessionViewSnapshot>
    const updatedAt = Number(value.updatedAt)
    const producerPID = Number(value.producerPID)

    if (
      value.schemaVersion !== HERMES_PET_SESSION_VIEW_SCHEMA_VERSION ||
      !Number.isFinite(updatedAt) ||
      updatedAt < 0 ||
      !Number.isSafeInteger(producerPID) ||
      producerPID < 0 ||
      !Array.isArray(value.recentViews) ||
      value.recentViews.length > HERMES_PET_SESSION_VIEW_MAX_RECENT
    ) {
      return null
    }

    const recentViews = value.recentViews.map(normalizedView)

    if (recentViews.some(view => view == null)) {
      return null
    }

    const current = value.current == null ? null : normalizedView(value.current)

    if (value.current != null && current == null) {
      return null
    }

    return {
      schemaVersion: HERMES_PET_SESSION_VIEW_SCHEMA_VERSION,
      updatedAt,
      producerPID,
      current,
      recentViews: recentViews as HermesPetSessionView[]
    }
  } catch {
    return null
  }
}

function writeHermesPetSessionViewAtomic(targetPath: string, snapshot: HermesPetSessionViewSnapshot): void {
  fs.mkdirSync(path.dirname(targetPath), { recursive: true })

  // Keep the temporary file beside the target so rename is atomic on every
  // supported filesystem. Include a random suffix so multiple Desktop windows
  // cannot accidentally rename one another's in-flight marker.
  const temporary = `${targetPath}.tmp-${process.pid}-${Math.random().toString(36).slice(2)}`

  try {
    fs.writeFileSync(temporary, `${JSON.stringify(snapshot, null, 2)}\n`, {
      encoding: 'utf8',
      mode: 0o600
    })
    fs.renameSync(temporary, targetPath)
  } finally {
    try {
      fs.unlinkSync(temporary)
    } catch {
      // The rename succeeded, or another cleanup path already removed it.
    }
  }
}

export function recordHermesPetSessionView(
  targetPath: string,
  payload: HermesPetSessionViewPayload | null | undefined,
  { now = Date.now() / 1000, producerPID = process.pid }: { now?: number; producerPID?: number } = {}
): HermesPetSessionViewResult {
  const timestamp = Number(now)
  const pid = Number(producerPID)

  if (!Number.isFinite(timestamp) || timestamp <= 0 || !Number.isSafeInteger(pid) || pid <= 0) {
    return { ok: false, error: 'invalid-runtime' }
  }

  if (!payload || typeof payload !== 'object' || !Object.hasOwn(payload, 'sessionID')) {
    return { ok: false, error: 'invalid-session-view' }
  }

  const previous = readHermesPetSessionViewSnapshot(targetPath)
  let recentViews = previous?.recentViews ?? []
  let current: HermesPetSessionView | null = null

  if (payload.sessionID != null) {
    const sessionID = safePetSessionIdentifier(payload.sessionID)
    const profile = normalizedProfile(payload.profile)

    if (!sessionID || !profile) {
      return { ok: false, error: 'invalid-session-view' }
    }

    current = { sessionID, profile, viewedAt: timestamp }
    recentViews = recentViews.filter(view => view.sessionID !== sessionID || view.profile !== profile)
    recentViews = [...recentViews, current].slice(-HERMES_PET_SESSION_VIEW_MAX_RECENT)
  }

  writeHermesPetSessionViewAtomic(targetPath, {
    schemaVersion: HERMES_PET_SESSION_VIEW_SCHEMA_VERSION,
    updatedAt: timestamp,
    producerPID: pid,
    current,
    recentViews
  })

  return { ok: true }
}

/**
 * Minimal structural view of an Electron BrowserWindow for the IPC sender
 * guard. Frontmost-window status belongs to Electron, not the renderer, so a
 * marker request is rejected when its sender window is missing, destroyed, or
 * not focused — it must neither mint foreground evidence nor clear/overwrite
 * the existing marker.
 */
export interface HermesPetSessionViewSenderWindow {
  isDestroyed(): boolean
  isFocused(): boolean
}

export function petSessionViewFocusGuard(
  senderWindow: HermesPetSessionViewSenderWindow | null | undefined
): 'not-focused' | null {
  if (!senderWindow || senderWindow.isDestroyed() || !senderWindow.isFocused()) {
    return 'not-focused'
  }

  return null
}
