/**
 * Swapped-bundle detection.
 *
 * The detached updater (scripts/desktop-update/posix.sh / windows.ps1)
 * rebuilds and swaps the packaged app on disk AFTER `hermes update` exits. An
 * instance that was reopened mid-update parks on the boot gate and — having
 * been launched from the pre-swap bundle — would otherwise proceed to run the
 * NEW runtime under the OLD renderer. That is the stale-renderer tail of the
 * #50238 mid-update relaunch: the "App build out of date" banner right after
 * a fully successful update, while the Updates card says "You're on the
 * latest version" and so offers nothing that would clear it.
 *
 * Detection: compare the install stamp this process loaded at boot with the
 * one on disk now. A different commit — or a different builtAt at the same
 * commit (a dirty-tree or content-hash rebuild) — means the bundle under our
 * feet is not the one we are running, and a plain relaunch loads it.
 *
 * Fail-quiet like bundle-skew: a missing stamp on either side (dev runs,
 * unreadable resources) or a fallback all-zero commit reports "not swapped".
 * This must never false-positive — a positive triggers an automatic relaunch.
 *
 * Pure so it is testable without booting Electron.
 */

import { isFallbackCommit } from './bundle-skew'

export interface BundleSwapStamp {
  /** write-build-stamp.mjs build timestamp — differs on every rebuild. */
  builtAt?: null | string
  commit: string
  /** write-build-stamp.mjs source tag — 'fallback' means the commit is fake. */
  source?: null | string
}

/** True only on positive proof that the bundle on disk is not the running one. */
export function detectBundleSwap(
  running: BundleSwapStamp | null,
  onDisk: BundleSwapStamp | null
): boolean {
  if (!running?.commit || !onDisk?.commit) {
    return false
  }

  if (running.source === 'fallback' || isFallbackCommit(running.commit)) {
    return false
  }

  if (onDisk.source === 'fallback' || isFallbackCommit(onDisk.commit)) {
    return false
  }

  if (running.commit !== onDisk.commit) {
    return true
  }

  // Same commit: only a builtAt PRESENT ON BOTH sides can prove a rebuild —
  // a missing timestamp (older stamp schema) proves nothing.
  return Boolean(running.builtAt && onDisk.builtAt && running.builtAt !== onDisk.builtAt)
}
