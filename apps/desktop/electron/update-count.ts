// Whether `git rev-list HEAD..origin/<branch> --count` produces a meaningful
// number worth computing. On a SHALLOW checkout (installer clones with
// --depth 1) the local history often shares no merge-base with the freshly
// fetched origin tip, so the count enumerates the entire remote ancestry and
// returns a bogus huge number (e.g. 12104) — see #51922. resolveBehindCount
// discards that bogus count in favour of a SHA compare, so the caller should
// SKIP the expensive rev-list entirely in that case rather than run it and
// throw the result away.
function shouldCountCommits({ isShallow, hasMergeBase }) {
  return !(isShallow && !hasMergeBase)
}

// Resolve how many commits the local checkout is behind origin for the desktop
// update indicator. When the count isn't meaningful (shallow + no merge-base)
// fall back to a binary up-to-date check by SHA, exactly like the official-SSH
// path in checkUpdates() and the CLI guard in hermes_cli/banner.py. Full clones
// (developers / Docker dev images) keep the exact count path unchanged.
function resolveBehindCount({ countStr, currentSha, targetSha, isShallow, hasMergeBase }) {
  if (!shouldCountCommits({ isShallow, hasMergeBase })) {
    if (currentSha && targetSha && currentSha === targetSha) {
      return 0
    }

    return 1 // behind by an unknown amount — show a generic "update available"
  }

  return Number.parseInt(countStr, 10) || 0
}

// Paths that can change without altering the Hermes runtime or Desktop app.
// Keep this allowlist deliberately narrow: unknown paths remain actionable so a
// future repository layout change cannot silently hide a real update.
function isDocumentationOnlyPath(filePath: unknown) {
  const normalized = String(filePath || '')
    .replaceAll('\\', '/')
    .replace(/^\.\//, '')

  return (
    normalized.startsWith('website/') ||
    normalized.startsWith('docs/') ||
    normalized === 'SECURITY.md' ||
    (normalized.startsWith('SECURITY.') && normalized.endsWith('.md'))
  )
}

// Suppress the update indicator only when we have a non-empty, successful
// changed-path result and every path is known documentation. Missing/empty data
// fails open and preserves the original behind count.
function resolveActionableBehindCount({
  behind,
  changedPaths
}: {
  behind: number
  changedPaths: string[] | null
}) {
  if (behind <= 0) {
    return 0
  }

  if (!Array.isArray(changedPaths) || changedPaths.length === 0) {
    return behind
  }

  return changedPaths.every(isDocumentationOnlyPath) ? 0 : behind
}

function buildChangedPathDiffArgs(baseRef: string, targetRef: string) {
  return ['diff', '--no-renames', '--name-only', `${baseRef}..${targetRef}`]
}

export {
  buildChangedPathDiffArgs,
  isDocumentationOnlyPath,
  resolveActionableBehindCount,
  resolveBehindCount,
  shouldCountCommits
}
