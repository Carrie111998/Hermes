import { atom, computed } from 'nanostores'

import { Codecs, persistentAtom } from '@/lib/persisted'
import { normalizeProjectPath } from '@/lib/project-paths'

// ── Recent projects (desktop-local MRU) ─────────────────────────────────────
// The workspaces you've actually opened, most-recent-first, so the switcher can
// offer them without re-picking a folder every time.
//
// WHY localStorage and not config.yaml: this is per-window UI convenience, in
// the same family as $projectScope, the remembered session, and the composer's
// sticky model — all of which persist here. It carries no behavior the agent or
// any non-desktop surface (CLI, gateway, TUI) reads, so writing it into the
// shared per-profile config.yaml would put desktop view state into a file the
// backend treats as settings, and would make every reorder a gateway round
// trip. Durable, named, cross-surface projects already have a home: the
// projects.db-backed `projects.*` RPCs in `@/store/projects`. This list is the
// lightweight MRU layer over "folders I opened", not a competing registry.
// Per the repo rule, no HERMES_* env var is involved either way.

const RECENT_PROJECTS_KEY = 'hermes.desktop.recentProjects'

/** Hard cap on remembered workspaces, so the list can't grow without bound. */
export const MAX_RECENT_PROJECTS = 12

export interface RecentProject {
  /** Normalized absolute path — the identity of the entry. */
  path: string
  /** Epoch ms of the last open, used only for ordering/display. */
  openedAt: number
}

function sanitizeRecents(value: unknown): RecentProject[] {
  if (!Array.isArray(value)) {
    return []
  }

  const seen = new Set<string>()
  const entries: RecentProject[] = []

  for (const item of value) {
    if (!item || typeof item !== 'object') {
      continue
    }

    const { path, openedAt } = item as { openedAt?: unknown; path?: unknown }
    const normalized = normalizeProjectPath(typeof path === 'string' ? path : '')

    // Dedupe on the way IN as well: an older build (or a hand-edited value)
    // could hold aliases that only collapse once normalized.
    if (!normalized || seen.has(normalized)) {
      continue
    }

    seen.add(normalized)
    entries.push({
      openedAt: typeof openedAt === 'number' && Number.isFinite(openedAt) ? openedAt : 0,
      path: normalized
    })
  }

  return entries.sort((a, b) => b.openedAt - a.openedAt).slice(0, MAX_RECENT_PROJECTS)
}

export const $recentProjects = persistentAtom<RecentProject[]>(
  RECENT_PROJECTS_KEY,
  [],
  Codecs.json<RecentProject[]>(sanitizeRecents)
)

/**
 * Record a workspace as just-opened: newest first, de-duplicated on the
 * normalized path, capped at MAX_RECENT_PROJECTS.
 *
 * Re-opening an existing entry MOVES it to the front rather than adding a
 * second row, so the list stays an MRU and never accumulates aliases of one
 * directory.
 */
export function recordRecentProject(path: string, now: number = Date.now()): void {
  const normalized = normalizeProjectPath(path)

  if (!normalized) {
    return
  }

  const rest = $recentProjects.get().filter(entry => entry.path !== normalized)

  $recentProjects.set([{ openedAt: now, path: normalized }, ...rest].slice(0, MAX_RECENT_PROJECTS))
}

/** Drop one workspace from the list (its "Remove from recents" affordance). */
export function forgetRecentProject(path: string): void {
  const normalized = normalizeProjectPath(path)

  if (!normalized) {
    return
  }

  const next = $recentProjects.get().filter(entry => entry.path !== normalized)

  if (next.length !== $recentProjects.get().length) {
    $recentProjects.set(next)
  }
}

export function clearRecentProjects(): void {
  $recentProjects.set([])
}

// ── Missing-directory marking ───────────────────────────────────────────────
// A remembered folder can be renamed, unmounted, or deleted between launches.
// Existence is probed asynchronously by the switcher (the renderer can't stat
// synchronously), and the result is cached here so a row can render as
// unavailable instead of silently re-anchoring a session to a dead path.
// A plain (unpersisted) atom on purpose: a missing volume that comes back
// should recover on the next probe rather than staying poisoned across
// restarts, so this cache is scoped to the running window.
export const $missingProjectPaths = atom<string[]>([])

export function markProjectMissing(path: string, missing: boolean): void {
  const normalized = normalizeProjectPath(path)

  if (!normalized) {
    return
  }

  const current = $missingProjectPaths.get()
  const has = current.includes(normalized)

  if (missing && !has) {
    $missingProjectPaths.set([...current, normalized])
  } else if (!missing && has) {
    $missingProjectPaths.set(current.filter(entry => entry !== normalized))
  }
}

export const isProjectMissing = (path: string): boolean =>
  $missingProjectPaths.get().includes(normalizeProjectPath(path))

/** Recents with their probed availability folded in, for rendering. */
export const $recentProjectRows = computed(
  [$recentProjects, $missingProjectPaths],
  (recents, missing): Array<RecentProject & { missing: boolean }> =>
    recents.map(entry => ({ ...entry, missing: missing.includes(entry.path) }))
)
