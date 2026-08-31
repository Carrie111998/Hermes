import { Codecs, persistentAtom } from '@/lib/persisted'
import {
  ownerQualifiedSessionIdentity,
  sessionRowForOwner,
  sessionRowIdentity,
  sessionRowIdentityForOwner
} from '@/lib/session-row-identity'
import { stableArray } from '@/lib/stable-array'
import { readKey } from '@/lib/storage'
import { $activeGatewayProfile } from '@/store/profile'
import { normalizeProfileKey } from '@/store/profile-key'
import type { SessionOwnerRoute } from '@/store/session-request-router'
import type { SessionInfo } from '@/types/hermes'

import {
  $cronSessions,
  $messagingSessions,
  $primarySessionOwnerIntent,
  $selectedStoredSessionId,
  $sessions,
  $unreadFinishedSessionIds,
  getSessionOwnerHint,
  sessionMatchesStoredId,
  sessionPinId
} from './session'
import { isBrowserWindow, isSecondaryWindow } from './windows'

/**
 * PERSISTED UNREAD ("finished — unread" green dot) — the durable layer under
 * the transient `$unreadFinishedSessionIds` atom, ported from the webui's
 * proven design (sessions.js: viewed-count watermarks + completion markers).
 *
 * Two persisted records, both bucketed BY PROFILE and then keyed by the
 * DURABLE lineage id (sessionPinId) so state survives auto-compression's
 * session-id rotation — same durable-id rule as `$sessionColorOverrides`:
 *
 *  1. SEEN WATERMARKS — the message_count last acknowledged for a session
 *     (set when the user opens it, kept current while it stays selected, and
 *     seeded on FIRST SIGHT of an unknown session so a fresh install doesn't
 *     paint every row green). A row whose live `message_count` exceeds its
 *     watermark is unread — this is what reconstructs the green dot for a
 *     session that finished while the app was CLOSED, which the live
 *     busy→idle edge in session-states.ts can never replay after a restart.
 *
 *  2. EXPLICIT MARKERS — the live busy→idle edge, persisted. Covers the gap
 *     where a turn finishes in the background but the sidebar list hasn't
 *     refreshed its message_count yet (and any completion that adds no
 *     stored messages), so a restart right after a finish keeps the dot.
 *
 * PROFILE SCOPING is not optional: session ids are caller-supplied and each
 * remote-profile backend is its own namespace, so two profiles can hold the
 * same id — and the lists routinely mix profiles (cron/messaging are ALWAYS
 * cross-profile, `$sessions` is too in all-profiles mode). Unscoped keys let
 * one profile's watermark/marker paint (or silence) another's row. The bucket
 * is always the ROW's own `profile` (normalizeProfileKey, absent → "default"),
 * never the live gateway's. Only an id with NO loaded row has no profile to
 * read: it uses the caller's `profileHint` when there is one (a permanently
 * hidden session, e.g. a Bot Mode canonical chat, is never listed and can
 * belong to a profile the gateway isn't homed on), else `$activeGatewayProfile`
 * — the only place a live busy→idle edge can come from.
 *
 * Both records are BOUNDED: markers cap per profile (oldest evicted),
 * watermarks cap in total (unlisted, unmarked entries dropped first), and
 * deleting/archiving a session drops its entries outright via
 * {@link forgetSessionUnread}.
 *
 * Scope of watermark-based unread: chat + cron rows. Messaging rows get their
 * message_count bumped by inbound human messages, so count-over-watermark
 * would paint "finished — unread" on every new inbound text; they keep only
 * the explicit live-edge markers (now persisted across restarts too).
 */

/** profile key → durable id → last acknowledged message_count. */
type SeenCounts = Record<string, Record<string, number>>

/** profile key → durable ids flagged by the live busy→idle edge. */
type Markers = Record<string, string[]>

const isPlainRecord = (value: unknown): value is Record<string, unknown> =>
  Boolean(value) && typeof value === 'object' && !Array.isArray(value)

export const $sessionSeenCounts = persistentAtom<SeenCounts>(
  'hermes.desktop.sessionSeenCounts',
  {},
  Codecs.json(sanitizeSeenCounts)
)

export const $unreadFinishedMarkers = persistentAtom<Markers>(
  'hermes.desktop.unreadFinishedSessions',
  {},
  Codecs.json(sanitizeMarkers)
)

// Also the migration: a pre-profile-scoping flat record (durable id → number)
// has non-object values, so it drops wholesale rather than mis-attributing
// every id to a profile named after it. Dropped watermarks reseed on next
// sight, which reads as "not unread" — the safe default.
function sanitizeSeenCounts(value: unknown): SeenCounts {
  if (!isPlainRecord(value)) {
    return {}
  }

  const next: SeenCounts = {}

  for (const [profile, bucket] of Object.entries(value)) {
    if (!isPlainRecord(bucket)) {
      continue
    }

    const counts = Object.fromEntries(
      Object.entries(bucket).filter(
        (entry): entry is [string, number] => typeof entry[1] === 'number' && Number.isFinite(entry[1])
      )
    )

    if (Object.keys(counts).length) {
      next[profile] = counts
    }
  }

  return next
}

// Same story for markers: the legacy flat shape was a bare string[], which is
// not an object of arrays, so it decodes to {}.
function sanitizeMarkers(value: unknown): Markers {
  if (!isPlainRecord(value)) {
    return {}
  }

  const next: Markers = {}

  for (const [profile, bucket] of Object.entries(value)) {
    if (!Array.isArray(bucket)) {
      continue
    }

    const ids = bucket.filter((id): id is string => typeof id === 'string' && id.length > 0)

    if (ids.length) {
      next[profile] = ids
    }
  }

  return next
}

// Watermarks accrete one entry per session ever seen; keep the record bounded.
// Past the cap, entries for sessions no longer in any loaded list (and not
// explicitly marked) are dropped — a pruned session reseeds on next sight,
// which reads as "not unread", the safe default.
const SEEN_COUNTS_CAP = 2000

// Markers only matter while their session still exists, and a marker whose
// session is gone is invisible (nothing paints it) but immortal — deletes we
// never observed (another client, a wiped backend) would accrete forever. Cap
// per profile and evict oldest-first: the freshest finishes are the ones the
// user still cares about.
const MARKERS_CAP = 200

const rowsFor = (lists: readonly SessionInfo[][]): SessionInfo[] => lists.flat()

/** The persistence bucket a row belongs to — its OWN profile, never the live
 *  gateway's (the lists are routinely cross-profile). */
const profileKeyForRow = (row: SessionInfo): string => normalizeProfileKey(row.profile)

/** The row's durable unread key. Connection-tagged rows are exact-owner
 * identities; ownerless/local legacy rows keep their historical raw key. */
const persistenceIdForRow = (row: SessionInfo): string =>
  row.connection_id ? sessionRowIdentity(row) : sessionPinId(row)

/** The row a bare stored id refers to. Ids are caller-supplied and each
 *  profile's backend is its own namespace, so two profiles can hold the same
 *  id; a tie breaks toward the live gateway, since both opening a session and
 *  running one swap the gateway onto that session's profile. */
function resolveLoadedRow(storedSessionId: string, ownerRoute?: SessionOwnerRoute): SessionInfo | undefined {
  const rows = rowsFor([$sessions.get(), $cronSessions.get(), $messagingSessions.get()])

  if (ownerRoute) {
    return sessionRowForOwner(rows, storedSessionId, ownerRoute)
  }

  const matches = rows.filter(row => sessionMatchesStoredId(row, storedSessionId))

  if (matches.length < 2) {
    return matches[0]
  }

  const gateway = normalizeProfileKey($activeGatewayProfile.get())

  return matches.find(row => profileKeyForRow(row) === gateway) ?? matches[0]
}

/** Bucket for a stored id with NO loaded row. The live gateway's profile is
 *  the right answer for anything born of a live turn, but a permanently
 *  hidden session (a Bot Mode canonical chat) is never in any list and can
 *  belong to a profile the gateway isn't homed on — those callers pass the
 *  owning profile they already know. */
const unlistedProfile = (hint?: null | string): string =>
  normalizeProfileKey((hint ?? '').trim() || $activeGatewayProfile.get())

function selectedSessionRowIdentity(rows: readonly SessionInfo[], selected: null | string): null | string {
  if (!selected) {
    return null
  }

  const primaryIntent = $primarySessionOwnerIntent.get()

  const ownerRoute =
    primaryIntent?.storedSessionId === selected ? primaryIntent.ownerRoute : getSessionOwnerHint(selected)

  if (ownerRoute) {
    return sessionRowIdentityForOwner(rows, selected, ownerRoute)
  }

  const candidates = rows.filter(row => sessionMatchesStoredId(row, selected))

  if (candidates.length === 1) {
    return sessionRowIdentity(candidates[0])
  }

  const activeProfile = normalizeProfileKey($activeGatewayProfile.get())
  const activeCandidates = candidates.filter(row => profileKeyForRow(row) === activeProfile)

  return activeCandidates.length === 1 ? sessionRowIdentity(activeCandidates[0]) : null
}

/** Write a profile's marker bucket, dropping the key when it empties so we
 *  don't persist a graveyard of `[]`. */
function setMarkerBucket(profile: string, ids: readonly string[]): void {
  const markers = $unreadFinishedMarkers.get()
  const next = { ...markers }

  if (ids.length) {
    next[profile] = [...ids]
  } else {
    delete next[profile]
  }

  $unreadFinishedMarkers.set(next)
}

/** UNREAD WRITER — the live busy→idle edge (session-states.ts) and any surface
 *  that learns out-of-band that a session produced something the user hasn't
 *  seen. Flags the transient atom (immediate paint) AND persists the marker so
 *  the dot survives a restart. A full owner route keeps same-id twins exact; a
 *  profile-only hint supports permanently hidden Bot Mode chats; otherwise the
 *  active gateway profile is the safe live-edge fallback. */
export function markSessionUnreadFinished(
  storedSessionId: string,
  ownerHint?: null | string | SessionOwnerRoute
): void {
  const ownerRoute = ownerHint && typeof ownerHint === 'object' ? ownerHint : undefined
  const profileHint = typeof ownerHint === 'string' ? ownerHint : ownerRoute?.profile
  const row = resolveLoadedRow(storedSessionId, ownerRoute)

  const transientId = ownerRoute
    ? row
      ? sessionRowIdentity(row)
      : ownerQualifiedSessionIdentity(ownerRoute.connectionId, ownerRoute.profile, storedSessionId)
    : storedSessionId

  const current = $unreadFinishedSessionIds.get()

  if (!current.includes(transientId)) {
    $unreadFinishedSessionIds.set([...current, transientId])
  }

  const profile = row ? profileKeyForRow(row) : unlistedProfile(profileHint)
  const durableId = ownerRoute ? transientId : row ? sessionPinId(row) : storedSessionId
  const bucket = $unreadFinishedMarkers.get()[profile] ?? []

  if (bucket.includes(durableId)) {
    return
  }

  const next = [...bucket, durableId]

  setMarkerBucket(profile, next.length > MARKERS_CAP ? next.slice(next.length - MARKERS_CAP) : next)
}

/** ACK — the user opened (or is looking at) this session: watermark := its
 *  current message_count, and any explicit marker is retired. */
function ackSessionRow(row: SessionInfo, exactOwner = false): void {
  const profile = profileKeyForRow(row)
  const durableId = exactOwner ? sessionRowIdentity(row) : persistenceIdForRow(row)

  if (Number.isFinite(row.message_count)) {
    const seen = $sessionSeenCounts.get()
    const bucket = seen[profile]

    if (bucket?.[durableId] !== row.message_count) {
      $sessionSeenCounts.set({ ...seen, [profile]: { ...bucket, [durableId]: row.message_count } })
    }
  }

  const markers = $unreadFinishedMarkers.get()[profile]

  if (!markers) {
    return
  }

  const next = markers.filter(id => id !== durableId && !sessionMatchesStoredId(row, id))

  if (next.length !== markers.length) {
    setMarkerBucket(profile, next)
  }
}

/** Clear persisted unread for a stored id even when its row isn't loaded —
 *  the marker alone can be retired; the watermark needs the row's count. A
 *  full owner route identifies an exact same-id twin; a profile-only hint
 *  supports hidden Bot Mode canonical chats; without either, use the active
 *  gateway profile and never touch another profile's identically named id. */
export function ackStoredSessionId(
  storedSessionId: null | string,
  ownerHint?: null | string | SessionOwnerRoute
): void {
  if (!storedSessionId) {
    return
  }

  const ownerRoute = ownerHint && typeof ownerHint === 'object' ? ownerHint : undefined
  const profileHint = typeof ownerHint === 'string' ? ownerHint : ownerRoute?.profile
  const row = resolveLoadedRow(storedSessionId, ownerRoute)

  if (row) {
    ackSessionRow(row, Boolean(ownerRoute))

    return
  }

  const profile = unlistedProfile(profileHint)
  const markers = $unreadFinishedMarkers.get()[profile]

  if (!markers) {
    return
  }

  const markerId = ownerRoute
    ? ownerQualifiedSessionIdentity(ownerRoute.connectionId, ownerRoute.profile, storedSessionId)
    : storedSessionId

  const next = markers.filter(id => id !== markerId)

  if (next.length !== markers.length) {
    setMarkerBucket(profile, next)
  }
}

/** Sidebar "Mark all as read" — ack every LOADED row (watermark := its current
 *  count, markers retired) so the persisted layer doesn't repaint the dots the
 *  user just dismissed on the next list refresh. Rows not loaded keep their
 *  state: an unseen session in a collapsed profile stays honestly unread. */
export function ackAllSessionsRead(): void {
  for (const row of rowsFor([$sessions.get(), $cronSessions.get(), $messagingSessions.get()])) {
    ackSessionRow(row)
  }
}

/** DELETE/ARCHIVE CLEANUP — the session is gone from the user's world, so its
 *  watermark and marker are dead weight. Drops every candidate id (stored id,
 *  live id, lineage root) from both persisted records in `profile`'s bucket
 *  and from the transient atom. Same-id sessions in other profiles survive. */
export function forgetSessionUnread(
  candidateIds: readonly (null | string | undefined)[],
  profile?: null | string
): void {
  const ids = new Set(candidateIds.filter((id): id is string => Boolean(id)))

  if (!ids.size) {
    return
  }

  const key = normalizeProfileKey(profile)
  const seen = $sessionSeenCounts.get()
  const counts = seen[key]

  if (counts) {
    const kept = Object.fromEntries(Object.entries(counts).filter(([id]) => !ids.has(id)))

    if (Object.keys(kept).length !== Object.keys(counts).length) {
      const next = { ...seen }

      if (Object.keys(kept).length) {
        next[key] = kept
      } else {
        delete next[key]
      }

      $sessionSeenCounts.set(next)
    }
  }

  const markers = $unreadFinishedMarkers.get()[key]

  if (markers) {
    const kept = markers.filter(id => !ids.has(id))

    if (kept.length !== markers.length) {
      setMarkerBucket(key, kept)
    }
  }

  const current = $unreadFinishedSessionIds.get()
  const nextIds = current.filter(id => !ids.has(id))

  if (nextIds.length !== current.length) {
    $unreadFinishedSessionIds.set(nextIds)
  }
}

/** Seed/refresh watermarks from freshly loaded lists: the SELECTED session
 *  tracks its live count (it's on screen — nothing there is unread), and a
 *  session never seen before seeds at its current count instead of lighting
 *  up green on first sight. Known, unselected rows are left alone — the gap
 *  between their watermark and live count IS the unread signal. */
function ingestRows(rows: readonly SessionInfo[]): void {
  const selected = $selectedStoredSessionId.get()
  const loadedRows = rowsFor([$sessions.get(), $cronSessions.get(), $messagingSessions.get()])
  const selectedIdentity = selectedSessionRowIdentity(loadedRows, selected)
  const seen = $sessionSeenCounts.get()
  let next: null | SeenCounts = null

  const write = (profile: string, durableId: string, count: number): void => {
    next ??= { ...seen }
    next[profile] = { ...next[profile], [durableId]: count }
  }

  for (const row of rows) {
    if (!Number.isFinite(row.message_count)) {
      continue
    }

    const profile = profileKeyForRow(row)
    const durableId = persistenceIdForRow(row)
    const bucket = next?.[profile] ?? seen[profile]

    const rowSelected = Boolean(selectedIdentity && sessionRowIdentity(row) === selectedIdentity)

    if (rowSelected) {
      if (bucket?.[durableId] !== row.message_count) {
        write(profile, durableId, row.message_count)
      }

      // Any lingering explicit marker for the on-screen session is stale.
      const markers = $unreadFinishedMarkers.get()[profile]

      if (markers) {
        const kept = markers.filter(id => id !== durableId && !sessionMatchesStoredId(row, id))

        if (kept.length !== markers.length) {
          setMarkerBucket(profile, kept)
        }
      }
    } else if (!bucket || !(durableId in bucket)) {
      write(profile, durableId, row.message_count)
    }
  }

  if (next) {
    $sessionSeenCounts.set(pruneSeenCounts(next, rows))
  }
}

function pruneSeenCounts(seen: SeenCounts, rows: readonly SessionInfo[]): SeenCounts {
  const total = Object.values(seen).reduce((sum, bucket) => sum + Object.keys(bucket).length, 0)

  if (total <= SEEN_COUNTS_CAP) {
    return seen
  }

  const markers = $unreadFinishedMarkers.get()
  const keep = new Map<string, Set<string>>()

  const keepFor = (profile: string): Set<string> => {
    let ids = keep.get(profile)

    if (!ids) {
      ids = new Set(markers[profile] ?? [])
      keep.set(profile, ids)
    }

    return ids
  }

  for (const row of rows) {
    keepFor(profileKeyForRow(row)).add(persistenceIdForRow(row))
  }

  const next: SeenCounts = {}

  for (const [profile, bucket] of Object.entries(seen)) {
    const allowed = keepFor(profile)
    const kept = Object.fromEntries(Object.entries(bucket).filter(([id]) => allowed.has(id)))

    if (Object.keys(kept).length) {
      next[profile] = kept
    }
  }

  return next
}

/** Recompute the transient unread atom from persisted state + loaded rows.
 *  Runs after every list refresh — including the first one after a cold
 *  start, which is what brings green dots back from a restart. Stored ids
 *  already flagged by the live edge but not (yet) present in any list — a
 *  brand-new session's first turn isn't flushed to the list until persisted —
 *  are preserved, not dropped. */
function recomputeUnread(): void {
  const selected = $selectedStoredSessionId.get()
  const markers = $unreadFinishedMarkers.get()
  const seen = $sessionSeenCounts.get()
  const unread: string[] = []
  const loadedRows = rowsFor([$sessions.get(), $cronSessions.get(), $messagingSessions.get()])
  const selectedIdentity = selectedSessionRowIdentity(loadedRows, selected)

  const rowIsSelected = (row: SessionInfo): boolean =>
    Boolean(selectedIdentity && sessionRowIdentity(row) === selectedIdentity)

  const isMarked = (row: SessionInfo, durableId: string): boolean => {
    const bucket = markers[profileKeyForRow(row)]

    return Boolean(bucket?.includes(durableId) || (!row.connection_id && bucket?.includes(row.id)))
  }

  const paintIdentity = (row: SessionInfo): string => (row.connection_id ? sessionRowIdentity(row) : row.id)

  // Watermark + marker unread for chat and cron rows. The exact selected row
  // stays quiet; a same-id twin owned by another connection remains eligible.
  for (const row of rowsFor([$sessions.get(), $cronSessions.get()])) {
    if (rowIsSelected(row)) {
      continue
    }

    const durableId = persistenceIdForRow(row)
    const watermark = seen[profileKeyForRow(row)]?.[durableId]

    const exceedsWatermark =
      typeof watermark === 'number' && Number.isFinite(row.message_count) && row.message_count > watermark

    if (exceedsWatermark || isMarked(row, durableId)) {
      unread.push(paintIdentity(row))
    }
  }

  // Messaging rows: explicit (live-edge) markers only — see header comment.
  for (const row of $messagingSessions.get()) {
    if (!rowIsSelected(row) && isMarked(row, persistenceIdForRow(row))) {
      unread.push(paintIdentity(row))
    }
  }

  // Preserve live-edge ids whose row isn't loaded yet.
  for (const id of $unreadFinishedSessionIds.get()) {
    const loaded = loadedRows.some(row => sessionRowIdentity(row) === id || sessionMatchesStoredId(row, id))

    if (id !== selected && !unread.includes(id) && !loaded) {
      unread.push(id)
    }
  }

  const current = $unreadFinishedSessionIds.get()
  const next = stableArray(current, unread)

  if (next !== current) {
    $unreadFinishedSessionIds.set([...next])
  }
}

// COLD-BOOT REHYDRATE — module evaluation can race Chromium's storage-area
// load: an early read comes back empty, the atoms seed their fallbacks, and
// the first ingest would then re-seed every row and write that emptiness
// back over the real record. By the time the first session list arrives
// (seconds later, over IPC) storage is reliably readable, so re-read both
// keys once right before the first ingest and adopt the disk state, keeping
// any entry this window already wrote in the meantime (a live edge or an
// ack beats the stale disk copy for its own key).
let rehydratedFromDisk = false

function rehydrateFromDiskOnce(): void {
  if (rehydratedFromDisk) {
    return
  }

  rehydratedFromDisk = true

  try {
    const rawSeen = readKey('hermes.desktop.sessionSeenCounts')
    const diskSeen = rawSeen ? sanitizeSeenCounts(JSON.parse(rawSeen) as unknown) : {}
    const memorySeen = $sessionSeenCounts.get()
    const mergedSeen: SeenCounts = { ...diskSeen }

    for (const [profile, bucket] of Object.entries(memorySeen)) {
      mergedSeen[profile] = { ...mergedSeen[profile], ...bucket }
    }

    $sessionSeenCounts.set(mergedSeen)

    const rawMarkers = readKey('hermes.desktop.unreadFinishedSessions')
    const diskMarkers = rawMarkers ? sanitizeMarkers(JSON.parse(rawMarkers) as unknown) : {}
    const memoryMarkers = $unreadFinishedMarkers.get()
    const merged: Markers = { ...diskMarkers }

    for (const [profile, ids] of Object.entries(memoryMarkers)) {
      merged[profile] = [...new Set([...(merged[profile] ?? []), ...ids])]
    }

    $unreadFinishedMarkers.set(merged)
  } catch {
    // Unreadable storage: keep the in-memory state — same fallback the
    // atoms started from.
  }
}

function onListChange(): void {
  rehydrateFromDiskOnce()
  ingestRows(rowsFor([$sessions.get(), $cronSessions.get(), $messagingSessions.get()]))
  recomputeUnread()
}

// Wiring: module-scope listeners, same pattern as session-states.ts's
// $selectedStoredSessionId listener. Loaded via session-states.ts (the live
// edge imports markSessionUnreadFinished), so it's active wherever unread is.
// ONLY the primary window maintains the persisted records: a secondary window
// (single-chat pop-out, watch window) sees a sliver of the session lists, and
// letting it seed/ack against that partial view would clobber the primary's
// whole-record writes — same isolation rule as the persisted session tiles.
if (!isSecondaryWindow() && !isBrowserWindow()) {
  $sessions.listen(onListChange)
  $cronSessions.listen(onListChange)
  $messagingSessions.listen(onListChange)

  // Opening a session acks it durably (the transient atom is already cleared
  // synchronously by setSelectedStoredSessionId — this is the persisted half).
  $selectedStoredSessionId.listen(selected => {
    if (!selected) {
      return
    }

    const primaryIntent = $primarySessionOwnerIntent.get()

    const ownerRoute =
      primaryIntent?.storedSessionId === selected ? primaryIntent.ownerRoute : getSessionOwnerHint(selected)

    ackStoredSessionId(selected, ownerRoute)
  })
}
