// Pure path helpers for the recent-projects list. Kept here (not in the store)
// so the normalization contract is testable without touching localStorage.
//
// Scope note: the renderer canNOT resolve symlinks or expand `~` — both need
// the filesystem, and `~` expansion in particular is a main-process concern
// (`electron/hardening.ts` joins it against `os.homedir()`). So this module
// does the normalization that is provably correct off-disk (separator, dot
// segments, trailing slashes, case-sensitive duplicates) and defers the rest to
// the backend: `changeSessionCwd` adopts the cwd the gateway echoes back, and
// recording THAT value is what collapses `~`/symlink aliases in practice.

/** Longest form we'll ever store, so a pathological value can't bloat storage. */
const MAX_PATH_LENGTH = 4096

/**
 * Canonical comparison form of a workspace path.
 *
 * Collapses `\` -> `/`, redundant separators, `.` segments, resolvable `..`
 * segments, and trailing slashes, so `/a/b`, `/a/b/`, `/a//b`, and `/a/./b` are
 * one entry. Returns '' for anything unusable, which callers treat as "don't
 * record".
 */
export function normalizeProjectPath(raw: null | string | undefined): string {
  const trimmed = (raw ?? '').trim()

  if (!trimmed || trimmed.length > MAX_PATH_LENGTH) {
    return ''
  }

  const slashed = trimmed.replace(/\\/g, '/')
  // Windows drive letters are case-insensitive; normalize so `C:/x` == `c:/x`.
  const driveNormalized = slashed.replace(/^([a-zA-Z]):\//, (_m, letter: string) => `${letter.toUpperCase()}:/`)
  const isAbsolute = driveNormalized.startsWith('/')
  const segments: string[] = []

  for (const segment of driveNormalized.split('/')) {
    if (!segment || segment === '.') {
      continue
    }

    // Only collapse `..` against a real parent we've already accepted; a
    // leading `..` on a relative path has to survive or we'd change its meaning.
    if (segment === '..' && segments.length > 0 && segments[segments.length - 1] !== '..') {
      segments.pop()

      continue
    }

    segments.push(segment)
  }

  const joined = segments.join('/')

  if (isAbsolute) {
    return `/${joined}`
  }

  return joined
}

/** True when two workspace paths denote the same directory, post-normalization. */
export const isSameProjectPath = (a: null | string | undefined, b: null | string | undefined): boolean => {
  const left = normalizeProjectPath(a)

  return Boolean(left) && left === normalizeProjectPath(b)
}

/**
 * Display label for a workspace: its final path segment (`/src/hermes-agent`
 * -> `hermes-agent`), falling back to the whole path for a root-ish value.
 */
export function projectPathLabel(raw: null | string | undefined): string {
  const normalized = normalizeProjectPath(raw)

  if (!normalized) {
    return ''
  }

  return normalized.split('/').filter(Boolean).pop() || normalized
}
