/**
 * Pure helpers for hermes://chat/new deep links.
 * Kept free of React so unit tests can cover cwd sanity + sticky + delivery correlation.
 */

export const DEEPLINK_STICKY_PREFIX = 'hermes.desktop.deeplink.sticky.'
/** @deprecated single-slot pending — kept for migration read-clear only */
export const DEEPLINK_STICKY_PENDING = 'hermes.desktop.deeplink.sticky.pending'
export const DEEPLINK_STICKY_PENDING_QUEUE = 'hermes.desktop.deeplink.sticky.pending.queue.v2'

export const STICKY_DELIVERY_TTL_MS = 60_000

export type StickyDeliveryBinding = {
  deliveryId: string
  slot: string
  /** Normalized profile key, or null if link omitted profile */
  profile: null | string
  createdAt: number
}

export function deeplinkStickyStorageKey(slot: string): string {
  return `${DEEPLINK_STICKY_PREFIX}${slot.trim().toLowerCase()}`
}

/**
 * Accept absolute workspace paths only. Reject relative and `..` traversal.
 * Unix: `/…` · Windows: `C:\…` / `C:/…` · UNC: `\\server\share\…`
 */
export function cwdLooksSane(cwd: string): boolean {
  const path = cwd.trim()
  if (!path) return false
  if (path.includes('\0')) return false
  const normalized = path.replace(/\\/g, '/')
  if (
    normalized.includes('/../') ||
    normalized.endsWith('/..') ||
    normalized === '..' ||
    normalized.startsWith('../')
  ) {
    return false
  }
  if (normalized.startsWith('/')) return true
  if (/^[A-Za-z]:[\\/]/.test(path)) return true
  if (path.startsWith('\\\\') || normalized.startsWith('//')) return true
  return false
}

export function normalizeStickySlot(raw: string | undefined | null): string {
  return String(raw || '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 64)
}

export function readStickySessionId(slot: string, storage: Storage = localStorage): null | string {
  const key = normalizeStickySlot(slot)
  if (!key) return null
  try {
    const id = storage.getItem(deeplinkStickyStorageKey(key))?.trim()
    return id || null
  } catch {
    return null
  }
}

export function writeStickySessionId(slot: string, sessionId: string, storage: Storage = localStorage): void {
  const key = normalizeStickySlot(slot)
  const id = sessionId.trim()
  if (!key || !id) return
  try {
    storage.setItem(deeplinkStickyStorageKey(key), id)
  } catch {
    /* quota / private mode */
  }
}

export function clearStickySessionId(slot: string, storage: Storage = localStorage): void {
  const key = normalizeStickySlot(slot)
  if (!key) return
  try {
    storage.removeItem(deeplinkStickyStorageKey(key))
  } catch {
    /* ignore */
  }
}

/** Monotonic delivery ids for chat/new (module scope; tests can reset). */
let chatNewDeliverySeq = 0
let chatNewLatestDeliveryId = 0

export function resetChatNewDeliverySeqForTests(): void {
  chatNewDeliverySeq = 0
  chatNewLatestDeliveryId = 0
}

export function nextChatNewDeliveryId(): number {
  chatNewDeliverySeq += 1
  chatNewLatestDeliveryId = chatNewDeliverySeq
  return chatNewDeliverySeq
}

/** True if a newer chat/new delivery started after `deliveryId`. */
export function isChatNewDeliveryStale(deliveryId: number): boolean {
  return deliveryId !== chatNewLatestDeliveryId
}

function readPendingQueue(sessionStore: Storage): StickyDeliveryBinding[] {
  try {
    const raw = sessionStore.getItem(DEEPLINK_STICKY_PENDING_QUEUE)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (b): b is StickyDeliveryBinding =>
        !!b &&
        typeof b === 'object' &&
        typeof (b as StickyDeliveryBinding).deliveryId === 'string' &&
        typeof (b as StickyDeliveryBinding).slot === 'string' &&
        typeof (b as StickyDeliveryBinding).createdAt === 'number'
    )
  } catch {
    return []
  }
}

function writePendingQueue(queue: StickyDeliveryBinding[], sessionStore: Storage): void {
  try {
    if (queue.length === 0) {
      sessionStore.removeItem(DEEPLINK_STICKY_PENDING_QUEUE)
    } else {
      sessionStore.setItem(DEEPLINK_STICKY_PENDING_QUEUE, JSON.stringify(queue))
    }
  } catch {
    /* ignore */
  }
}

function pruneQueue(queue: StickyDeliveryBinding[], now = Date.now()): StickyDeliveryBinding[] {
  return queue.filter(b => now - b.createdAt <= STICKY_DELIVERY_TTL_MS)
}

/**
 * Register a sticky bind for a specific deep-link delivery (not a global singleton).
 * Rapid successive links each push their own binding; session mount consumes FIFO match.
 */
export function pushStickyDelivery(
  binding: Omit<StickyDeliveryBinding, 'createdAt'> & { createdAt?: number },
  sessionStore: Storage = sessionStorage
): void {
  const slot = normalizeStickySlot(binding.slot)
  const deliveryId = String(binding.deliveryId || '').trim()
  if (!slot || !deliveryId) return
  const now = Date.now()
  const next = pruneQueue(readPendingQueue(sessionStore), now)
  next.push({
    deliveryId,
    slot,
    profile: binding.profile != null && String(binding.profile).trim() ? String(binding.profile).trim() : null,
    createdAt: binding.createdAt ?? now
  })
  writePendingQueue(next, sessionStore)
  // Clear legacy singleton so old builds don't double-bind
  try {
    sessionStore.removeItem(DEEPLINK_STICKY_PENDING)
  } catch {
    /* ignore */
  }
}

/**
 * Consume the oldest non-expired sticky delivery that matches the mounted session's profile.
 * Profile null on the binding matches any session; concrete profile must equal session profile key.
 */
export function takeStickyDeliveryForSession(
  opts: { profile: null | string; now?: number },
  sessionStore: Storage = sessionStorage
): null | string {
  const now = opts.now ?? Date.now()
  const sessionProfile = (opts.profile ?? '').trim() || null
  let queue = pruneQueue(readPendingQueue(sessionStore), now)

  // Migrate one-shot legacy pending into queue head once
  try {
    const legacy = normalizeStickySlot(sessionStore.getItem(DEEPLINK_STICKY_PENDING))
    if (legacy) {
      sessionStore.removeItem(DEEPLINK_STICKY_PENDING)
      queue = [{ deliveryId: `legacy-${now}`, slot: legacy, profile: null, createdAt: now }, ...queue]
    }
  } catch {
    /* ignore */
  }

  const idx = queue.findIndex(b => {
    if (!b.profile) return true
    if (!sessionProfile) return true
    return b.profile === sessionProfile
  })
  if (idx < 0) {
    writePendingQueue(queue, sessionStore)
    return null
  }
  const [hit] = queue.splice(idx, 1)
  writePendingQueue(queue, sessionStore)
  return hit?.slot || null
}

/** @deprecated use pushStickyDelivery — tests still cover migration */
export function setStickyPending(slot: string, sessionStore: Storage = sessionStorage): void {
  pushStickyDelivery({ deliveryId: `legacy-set-${Date.now()}`, slot, profile: null }, sessionStore)
}

/** @deprecated use takeStickyDeliveryForSession */
export function takeStickyPending(sessionStore: Storage = sessionStorage): null | string {
  return takeStickyDeliveryForSession({ profile: null }, sessionStore)
}

export type ProfileNameLike = { name?: null | string; is_default?: boolean }

/**
 * Allow-list external profile against installed Desktop profiles.
 * Empty installed list (boot) → only allow omitted profile or "default".
 */
export function isInstalledProfileName(
  profile: string,
  installed: ProfileNameLike[]
): boolean {
  const key = String(profile || '').trim()
  if (!key) return true
  const norm = key.toLowerCase()
  if (installed.length === 0) {
    return norm === 'default' || key === 'default'
  }
  return installed.some(p => {
    const n = String(p.name || '').trim()
    if (!n) return !!p.is_default && (norm === 'default' || key === 'default')
    return n === key || n.toLowerCase() === norm
  })
}
