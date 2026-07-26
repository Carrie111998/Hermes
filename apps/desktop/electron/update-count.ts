// Whether `git rev-list HEAD..origin/<branch> --count` produces a meaningful
// number worth computing. On a SHALLOW checkout (the installer clones with
// --depth 1) the local history is truncated at a graft boundary, so git cannot
// walk HEAD's ancestry to exclude it from the range. `rev-list --count` then
// enumerates nearly the entire remote ancestry and returns a bogus huge number
// — e.g. "v0.19.0 (+17570)" on a repo whose real gap was 143 commits (#51922).
//
// The tell is arithmetic: the reported count equals
// `rev-list --count origin/<branch>` minus `rev-list --count HEAD`. It is not
// finding new commits, it is failing to subtract the old ones.
//
// A merge-base probe cannot distinguish the safe case from the broken one. A
// shallow clone whose grafted HEAD is an ancestor of the fetched tip reports
// `merge-base HEAD origin/<branch>` == HEAD — a merge-base exists, yet the
// count is still bogus because the ancestry behind the graft is still missing.
// Shallowness alone is the reliable signal, which is what hermes_cli/banner.py
// (`_check_via_local_git`) and hermes_cli/main.py (`cmd_check_update`) already
// key on. resolveBehindCount discards the count for a shallow repo in favour of
// a SHA compare, so the caller should SKIP the expensive rev-list entirely
// rather than run it and throw the result away.
function shouldCountCommits({ isShallow }) {
  return !isShallow
}

// Resolve how many commits the local checkout is behind origin for the desktop
// update indicator. When the count isn't meaningful (a shallow checkout) fall
// back to a binary up-to-date check by SHA, exactly like the official-SSH path
// in checkUpdates() and the CLI guard in hermes_cli/banner.py. Full clones
// (developers / Docker dev images) keep the exact count path unchanged.
function resolveBehindCount({ countStr, currentSha, targetSha, isShallow }) {
  if (!shouldCountCommits({ isShallow })) {
    if (currentSha && targetSha && currentSha === targetSha) {
      return 0
    }

    return 1 // behind by an unknown amount — show a generic "update available"
  }

  return Number.parseInt(countStr, 10) || 0
}

export { resolveBehindCount, shouldCountCommits }
