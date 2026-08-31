import type { Translations } from '@/i18n'
import type { SidebarSessionEntry } from '@/lib/session-branch-tree'

// Date sectioning for the flat recents list (the Claude Code Desktop model:
// Today / Yesterday / This week / This month / one section per older month).
// Buckets are LOCAL calendar facts, not rolling 24h windows, so a session from
// 23:59 yesterday never reads as "today". These sections only make sense over
// the recency sort — the caller must skip sectioning when the user's manual
// drag-order is active.

export type SessionDateBucket =
  | { kind: 'month'; month: number; year: number }
  | { kind: 'thisMonth' }
  | { kind: 'thisWeek' }
  | { kind: 'today' }
  | { kind: 'yesterday' }

export type SidebarSessionListItem =
  | { bucket: SessionDateBucket; id: string; kind: 'header' }
  | { entry: SidebarSessionEntry; kind: 'entry' }

const DAY_MS = 24 * 60 * 60 * 1000

function startOfDay(date: Date): number {
  return new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime()
}

export function sessionDateBucket(timestampSeconds: number, now: Date): SessionDateBucket {
  const time = timestampSeconds * 1000
  const todayStart = startOfDay(now)

  // Clock skew between writers puts some records slightly in the future;
  // they're the freshest thing in the list, so they read as today.
  if (time >= todayStart) {
    return { kind: 'today' }
  }

  if (time >= todayStart - DAY_MS) {
    return { kind: 'yesterday' }
  }

  // "This week" is the six calendar days before yesterday — a stable window
  // that never collides with today/yesterday and, unlike an ISO week, never
  // renders an empty or one-day section right after a week boundary.
  if (time >= todayStart - 7 * DAY_MS) {
    return { kind: 'thisWeek' }
  }

  const date = new Date(time)

  if (date.getFullYear() === now.getFullYear() && date.getMonth() === now.getMonth()) {
    return { kind: 'thisMonth' }
  }

  return { kind: 'month', month: date.getMonth(), year: date.getFullYear() }
}

function bucketId(bucket: SessionDateBucket): string {
  return bucket.kind === 'month' ? `date:month:${bucket.year}-${bucket.month}` : `date:${bucket.kind}`
}

function entryTimestamp(entry: SidebarSessionEntry): number {
  const { last_active: lastActive, started_at: startedAt } = entry.session
  const timestamp = lastActive || startedAt || 0

  return Number.isFinite(timestamp) && timestamp > 0 ? timestamp : 0
}

// Locale-default month names, matching the `lib/time.ts` formatter convention.
const fmtMonth = new Intl.DateTimeFormat(undefined, { month: 'long' })
const fmtMonthYear = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' })

export function dateSectionLabel(
  bucket: SessionDateBucket,
  strings: Translations['sidebar']['dateSections'],
  now: Date = new Date()
): string {
  switch (bucket.kind) {
    case 'month': {
      const monthStart = new Date(bucket.year, bucket.month, 1)

      return bucket.year === now.getFullYear() ? fmtMonth.format(monthStart) : fmtMonthYear.format(monthStart)
    }

    case 'thisMonth':
      return strings.thisMonth

    case 'thisWeek':
      return strings.thisWeek

    case 'today':
      return strings.today

    case 'yesterday':
      return strings.yesterday
  }
}

export function withDateSections(entries: SidebarSessionEntry[], now: Date = new Date()): SidebarSessionListItem[] {
  const items: SidebarSessionListItem[] = []
  let currentId: null | string = null

  for (const entry of entries) {
    // Branch children render indented under their root; splitting a branch
    // family across date sections would detach them from their stem.
    if (!entry.branchStem) {
      const timestamp = entryTimestamp(entry)

      // An unparseable timestamp inherits the surrounding section rather than
      // fabricating a bogus one (or a header for the epoch).
      if (timestamp > 0) {
        const bucket = sessionDateBucket(timestamp, now)
        const id = bucketId(bucket)

        if (id !== currentId) {
          currentId = id
          items.push({ bucket, id, kind: 'header' })
        }
      }
    }

    items.push({ entry, kind: 'entry' })
  }

  return items
}
