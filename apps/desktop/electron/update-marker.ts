/**
 * In-app update mutual-exclusion marker (#50238).
 *
 * The Tauri updater writes HERMES_HOME/.hermes-update-in-progress for the whole
 * duration of an `--update` run (see apps/bootstrap-installer/src-tauri/src/
 * update.rs `UpdateMarkerGuard`). The marker body is two lines: the updater's
 * pid and a unix-seconds lease timestamp refreshed by the owner.
 *
 * Why: if the user relaunches the desktop mid-update — the window vanished with
 * no progress and looks crashed — a fresh instance must NOT spawn its own local
 * backend. That backend re-locks the venv shim, the updater's straggler cleanup
 * (`force_kill_other_hermes`, taskkill /IM hermes.exe) kills it, the launch
 * fails with the 45s "backend didn't come up" timeout, and the user relaunches
 * into the same trap — an infinite respawn/kill loop. The desktop gates local
 * backend startup on this marker and parks until the update finishes.
 *
 * This module holds the PURE, side-effect-light logic (path, pid liveness,
 * parse + staleness) so it is unit-testable without booting Electron. The
 * polling/boot-progress wrapper lives in main.ts where the boot-progress and
 * log sinks are.
 */

import fs from 'fs'
import { randomUUID } from 'node:crypto'
import path from 'path'

// Owners heartbeat the marker's second line well before this nominal ceiling.
// A live PID remains authoritative beyond it: suspend or heartbeat trouble
// must never let the desktop start a backend into a mutating checkout.
export const UPDATE_MARKER_MAX_AGE_MS = 20 * 60 * 1000
const MAX_U32 = 0xffffffff
const FILE_FLAG_OPEN_REPARSE_POINT = 0x00200000
const O_NOFOLLOW = fs.constants.O_NOFOLLOW ?? 0

/**
 * Open a marker through a stable file handle before doing any I/O.
 *
 * POSIX has a native O_NOFOLLOW flag. Node/libuv does not expose the Windows
 * FILE_FLAG_OPEN_REPARSE_POINT bit through fs.openSync, so Windows uses the
 * equivalent handle boundary: open first, immediately re-check the path
 * topology, and read/write only through the already-open descriptor. A handle
 * cannot be redirected after it is opened; the post-open check rejects a
 * reparse-point race that happened while opening. Keep the Windows constant
 * next to this helper as the contract we mirror (and to make the boundary
 * explicit for native-backed Electron builds).
 */
function openMarkerNoFollow(filePath: string, flags: number, mode?: number) {
  const openFlags =
    process.platform === 'win32' ? flags | FILE_FLAG_OPEN_REPARSE_POINT : flags | O_NOFOLLOW
  const fd = fs.openSync(filePath, openFlags, mode)
  try {
    assertNoReparseTopology(filePath, { allowMissing: false })
    return fd
  } catch (err) {
    fs.closeSync(fd)
    throw err
  }
}

export function markerPath(hermesHome) {
  // Named profiles have distinct HERMES_HOME values but mutate the same
  // <root>/hermes-agent checkout. Keep the desktop reader on the same
  // install-wide marker as Python and Rust.
  const profileParent = path.dirname(hermesHome)
  const installRoot = path.basename(profileParent) === 'profiles' ? path.dirname(profileParent) : hermesHome

  return path.join(installRoot, '.hermes-update-in-progress')
}

/**
 * Reject links/reparse points in the marker path topology before any marker
 * open/read/write. `lstatSync` is the Node equivalent of Python's lstat walk;
 * the realpath comparison additionally catches Windows junctions on Node
 * versions that expose them as ordinary directories rather than symlinks.
 */
function assertNoReparseTopology(filePath: string, { allowMissing = true } = {}) {
  const absolute = path.resolve(filePath)
  let current = absolute
  let first = true

  while (true) {
    let metadata
    try {
      metadata = fs.lstatSync(current)
    } catch (err) {
      if (err && err.code === 'ENOENT') {
        if (first && !allowMissing) {
          throw new Error(`Marker path was not found: ${filePath}`)
        }
        const parent = path.dirname(current)
        if (parent === current) {
          break
        }
        current = parent
        first = false
        continue
      }
      throw new Error(`Could not inspect marker path ${current}: ${String(err)}`)
    }

    const normalizedCurrent = absolutePathForComparison(current)
    let isReparse = metadata.isSymbolicLink()
    if (!isReparse && process.platform === 'win32') {
      try {
        isReparse = absolutePathForComparison(fs.realpathSync.native(current)) !== normalizedCurrent
      } catch {
        // The lstat result is still authoritative if realpath is unavailable.
      }
    }
    if (isReparse) {
      throw new Error(`Marker path contains a link or reparse point: ${current}`)
    }

    const parent = path.dirname(current)
    if (parent === current) {
      break
    }
    current = parent
    first = false
  }
}

function absolutePathForComparison(value: string) {
  return path.resolve(value).toLowerCase()
}

function unavailableMarker(reason: unknown) {
  return {
    pid: null,
    ageMs: Infinity,
    leaseExpired: true,
    unavailable: true,
    reason: String(reason)
  }
}

// True only if a host process with this pid is currently alive. Signal 0 does
// not deliver a signal — it just probes existence/permission. ESRCH => dead;
// EPERM => alive but owned by another user (still "alive" for our purposes).
// Injectable `kill` keeps it unit-testable.
export function isPidAlive(pid, kill: typeof process.kill = process.kill.bind(process)) {
  if (!Number.isSafeInteger(pid) || pid <= 0 || pid > MAX_U32) {
    return false
  }

  try {
    kill(pid, 0)

    return true
  } catch (err) {
    // Only ESRCH proves that the process is gone. Permission denied and
    // indeterminate host errors fail closed as alive so we never discard a
    // possibly-live updater claim.
    return !(err && err.code === 'ESRCH')
  }
}

/**
 * Read + interpret the marker.
 *
 * Returns a live `{ pid, ageMs }` claim for a parseable, alive owner, an
 * `unavailable` sentinel for unreadable/malformed state, and `null` only for
 * absence or a well-formed confirmed-dead PID. This read-only desktop observer
 * never deletes: Python/Rust owners perform mutex-protected dead cleanup,
 * avoiding a read→unlink race with an atomic heartbeat or ownership handoff.
 *
 * Pure-ish: file I/O against the given path, plus an injectable pid probe and
 * clock for tests.
 */
export function readLiveUpdateMarker(
  hermesHome,
  {
    kill,
    now = Date.now,
    maxAgeMs = UPDATE_MARKER_MAX_AGE_MS
  }: {
    now?: () => number
    maxAgeMs?: number
    kill?: typeof process.kill
  } = {}
) {
  const file = markerPath(hermesHome)
  let raw

  try {
    assertNoReparseTopology(file, { allowMissing: true })
    const fd = openMarkerNoFollow(file, fs.constants.O_RDONLY)
    try {
      raw = fs.readFileSync(fd, 'utf8')
    } finally {
      fs.closeSync(fd)
    }
  } catch (err) {
    if (err && err.code === 'ENOENT') {
      return null
    }

    return unavailableMarker(`Update marker is unreadable: ${String(err)}`)
  }

  const wire = /^([1-9][0-9]*)\r?\n([0-9]+)(?:\r?\n)?$/.exec(String(raw))
  const pid = wire ? Number(wire[1]) : Number.NaN
  const startedAt = wire ? Number(wire[2]) : Number.NaN
  const validPid = Number.isSafeInteger(pid) && pid > 0 && pid <= MAX_U32
  const validLease = Number.isSafeInteger(startedAt) && startedAt >= 0
  const ageMs = validLease ? now() - startedAt * 1000 : Infinity
  const alive = validPid && isPidAlive(pid, kill)

  if (!validPid || !validLease) {
    return {
      pid: null,
      ageMs: Infinity,
      leaseExpired: true,
      unavailable: true,
      reason: 'Update marker is malformed'
    }
  }

  if (!alive) {
    return null
  }

  return { pid, ageMs, leaseExpired: ageMs > maxAgeMs, unavailable: false, reason: null }
}

/**
 * Write the update-in-progress marker *from the desktop* before handing off
 * to the detached updater.
 *
 * The Tauri-based hermes-setup.exe takes several seconds to initialise its
 * window and reach the Rust `run_update` entry point where it writes the
 * marker itself. During that gap the desktop's `app.quit()` teardown kills
 * the backend child, the renderer's WebSocket drops, and the renderer
 * immediately calls `ensureBackend()` → `waitForUpdateToFinish()`. Because
 * the updater hasn't written the marker yet, the gate sees no live update
 * and spawns a *new* backend — which re-locks `.pyd` files in the venv.
 * When the updater finally reaches the venv-rebuild stage it finds those
 * files locked and the update bricks.
 *
 * Fix: the desktop writes the marker itself, using the spawned updater's
 * PID, immediately after `spawn()`. The complete two-line payload is staged
 * and then hard-linked into place, an atomic no-clobber publish shared by the
 * Python and Rust claimers. If another updater wins the race its marker stays
 * untouched; `UpdateMarkerGuard` later adopts this marker when our child won.
 * Confirmed-dead markers are cleaned under the Python/Rust shared mutex;
 * malformed state remains a conservative blocker. Python and Rust mutation
 * paths open the marker/mutex with no-follow/reparse-point checks and publish
 * complete staged payloads atomically. Electron is intentionally a read-only
 * observer except for this initial no-clobber hard-link claim; it leaves any
 * existing winner untouched and the Python/Rust owner performs replacement or
 * cleanup under the mutex.
 */
export function writeUpdateMarker(
  hermesHome,
  pid,
  options: {
    now?: () => number
    maxAgeMs?: number
    kill?: typeof process.kill
    startedAt?: number
  } = {}
) {
  if (!Number.isInteger(pid) || pid <= 0) {
    return
  }
  const file = markerPath(hermesHome)
  const { now = Date.now, startedAt } = options
  const nowMs = now()
  const acquiredAt =
    typeof startedAt === 'number' && Number.isInteger(startedAt) && startedAt >= 0
      ? startedAt
      : Math.floor(nowMs / 1000)
  if (!Number.isSafeInteger(acquiredAt) || acquiredAt < 0) {
    return
  }
  const temp = path.join(path.dirname(file), `.${path.basename(file)}.${process.pid}.${randomUUID()}.claim`)
  let tempFd

  try {
    // Validate the destination topology before creating a staging file. The
    // second validation immediately before linkSync closes the replacement
    // race if a parent or marker is swapped while the payload is staged.
    assertNoReparseTopology(file, { allowMissing: true })
    assertNoReparseTopology(temp, { allowMissing: true })
    tempFd = fs.openSync(temp, 'wx', 0o600)
    fs.writeFileSync(tempFd, `${pid}\n${acquiredAt}\n`, 'utf8')
    fs.fsyncSync(tempFd)
    fs.closeSync(tempFd)
    tempFd = undefined

    assertNoReparseTopology(file, { allowMissing: true })
    assertNoReparseTopology(temp, { allowMissing: false })
    // Keep a no-follow handle on the complete staged inode through the
    // no-clobber publish. Readers never consume a path that was reparse-swapped
    // after the topology check.
    const tempHandle = openMarkerNoFollow(temp, fs.constants.O_RDONLY)
    try {
      // linkSync is atomic and refuses an existing destination. Readers can
      // observe only the complete staged inode, never an empty/truncated body.
      fs.linkSync(temp, file)
    } finally {
      fs.closeSync(tempHandle)
    }
  } catch {
    // Best-effort: an existing winner is intentionally left untouched. If
    // publication itself is unavailable, Rust still fails closed when it
    // reaches UpdateMarkerGuard::acquire.
  } finally {
    if (tempFd !== undefined) {
      try {
        fs.closeSync(tempFd)
      } catch {
        // Best-effort cleanup.
      }
    }
    try {
      assertNoReparseTopology(temp, { allowMissing: true })
      fs.unlinkSync(temp)
    } catch {
      // The temp may never have been created, is already gone, or its parent
      // became a reparse point; never follow an unvalidated cleanup path.
    }
  }
}

/**
 * Whether a NEW updater hand-off must be refused because a different,
 * already-alive updater currently owns the marker (#75778).
 *
 * A user who clicks "Update" again while a prior updater is still parked
 * mid-run (e.g. "waiting for Hermes to exit…") must not spawn a second
 * checkout mutator. `writeUpdateMarker` also publishes no-clobber as a final
 * race barrier between this check and the detached child starting.
 *
 * Returns the live foreign owner (with a ready-to-show message) when the
 * hand-off must be refused, or `null` when no confirmed-live owner exists.
 */
export function updateHandoffConflict(
  hermesHome,
  opts: {
    now?: () => number
    maxAgeMs?: number
    kill?: typeof process.kill
  } = {}
) {
  const owner = readLiveUpdateMarker(hermesHome, opts)

  if (!owner) {
    return null
  }

  if (owner.unavailable || owner.pid === null) {
    return {
      pid: null,
      ageMs: owner.ageMs,
      message:
        'Hermes cannot verify the current update marker. Backend startup and new updates are paused to protect the install; check the Hermes home permissions or restart after the active updater exits.'
    }
  }

  const mins = Math.floor(owner.ageMs / 60_000)
  const secs = Math.floor((owner.ageMs % 60_000) / 1000)
  const elapsed = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`

  return {
    pid: owner.pid,
    ageMs: owner.ageMs,
    message: `An update is already running (PID ${owner.pid}, last active ${elapsed} ago). Wait for it to finish, then try again.`
  }
}
