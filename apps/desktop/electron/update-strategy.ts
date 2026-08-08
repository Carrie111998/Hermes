/**
 * Update strategy ladder: which self-update mechanism applies to this install.
 *
 * Hermes Desktop historically has exactly ONE update path — the source
 * install. `applyUpdates` runs `hermes update` + `hermes desktop --build-only`
 * and swaps the .app bundle, which requires a git checkout, the `hermes` CLI,
 * and a Node toolchain on the machine running the app. That assumption breaks
 * for the remote-client topology (always-on backend host + packaged client
 * with no repo/CLI/build tools): those clients have no source tree and so no
 * update path at all.
 *
 * This module turns the single path into an ORDERED LADDER of candidates —
 * the pattern apps/desktop/AGENTS.md mandates ("cross everything as an
 * observable ladder … a candidate is trusted only after it is validated at
 * the right boundary"). The decision is pure and bootable WITHOUT Electron so
 * it is unit-testable in isolation; main.ts only reads the verdict.
 *
 * Precedence (highest first):
 *
 *   1. electron-updater  — packaged app AND an update feed is configured.
 *      The app downloads a signed release binary (dmg/exe/AppImage) and
 *      quit-and-installs. This is the remote-client path: no repo, no CLI,
 *      no build tools needed on the device.
 *   2. staged hermes-setup (Windows) — the Tauri installer self-copied a
 *      setup binary; the quit→hand-off→rebuild dance handles the venv-shim
 *      file lock. Unchanged existing behavior.
 *   3. POSIX in-app rebuild — macOS/Linux source install with a working
 *      `hermes` CLI. Unchanged existing behavior.
 *   4. manual — nothing else applies: surface the exact `hermes update`
 *      command for the user to run. Unchanged existing fallback.
 *
 * Crucially, electron-updater only ever activates for a PACKAGED app with a
 * configured feed. Every existing install (source checkout, or packaged with
 * no feed) falls through to the exact path it uses today — no behavior
 * change. That gate is what makes this safe to merge.
 */

export type UpdateStrategy = 'electron-updater' | 'staged-setup' | 'posix-in-app' | 'manual'

export interface UpdateStrategyInput {
  /** app.isPackaged — false for a dev/source checkout (`electron .`). */
  isPackaged: boolean
  /** Resolved update feed URL (from updates.json `feed_url`), or '' if unset. */
  feedUrl: string
  /** Whether a staged hermes-setup updater binary was resolved (Windows). */
  hasStagedUpdater: boolean
  /** process.platform === 'win32'. */
  isWindows: boolean
}

/**
 * Decide the update strategy. Pure: no fs, no electron, no network. The
 * caller validates each input at the boundary (isPackaged from Electron, the
 * feed URL from updates.json, the staged-updater path from the resolver).
 */
export function resolveUpdateStrategy(input: UpdateStrategyInput): UpdateStrategy {
  const feedUrl = (input.feedUrl || '').trim()

  // Rung 1 — self-updating packaged client. A packaged app with a configured
  // feed never needs a repo/CLI/toolchain, so it can update from signed
  // release binaries regardless of platform.
  if (input.isPackaged && feedUrl) {
    return 'electron-updater'
  }

  // Rung 2 — Windows staged installer. Only reachable when electron-updater
  // did not claim the install (no feed configured), preserving the existing
  // quit→hand-off→rebuild path for CLI-installed Windows users.
  if (input.hasStagedUpdater && input.isWindows) {
    return 'staged-setup'
  }

  // Rung 3 — POSIX source install (macOS/Linux with a working hermes CLI).
  // Also reached by a packaged app WITHOUT a feed: it cannot self-update from
  // a binary, so it keeps the existing hermes-update+rebuild behavior.
  if (!input.isWindows) {
    return 'posix-in-app'
  }

  // Rung 4 — Windows with neither a feed nor a staged updater: manual.
  return 'manual'
}

/**
 * Whether the electron-updater rung applies. Exposed separately so the
 * update-detection path (`checkUpdates`) can ask the same question as the
 * apply path without duplicating the gate.
 */
export function usesElectronUpdater(input: UpdateStrategyInput): boolean {
  return resolveUpdateStrategy(input) === 'electron-updater'
}

/**
 * Resolve the electron-updater publish provider from a feed URL. Kept in this
 * dependency-free module (NOT in electron-updater-controller.ts, which imports
 * the electron-updater package) so it is unit-testable without installing the
 * dependency.
 *
 * A github.com/<owner>/<repo>(.git)(/releases…) URL uses the GitHub Releases
 * provider; anything else is a generic HTTPS feed (S3 / own host / per-tenant
 * server) serving the latest-*.yml manifests. This is what makes per-tenant /
 * self-hosted feeds work without code changes — the tenant just points
 * feed_url at their host.
 */
export type FeedConfiguration =
  | { provider: 'github'; owner: string; repo: string }
  | { provider: 'generic'; url: string }

export function feedConfiguration(feedUrl: string): FeedConfiguration {
  const match = feedUrl.trim().match(/github\.com[/:]([^/]+)\/([^/.]+)/i)
  if (match) {
    return { provider: 'github', owner: match[1], repo: match[2] }
  }
  return { provider: 'generic', url: feedUrl.trim() }
}
