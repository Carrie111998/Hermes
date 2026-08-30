/**
 * before-pack.mjs — electron-builder beforePack hook.
 *
 * Two responsibilities:
 *
 * 1. Removes any stale unpacked app directory (`appOutDir`) before
 *    electron-builder stages the Electron binaries into it.
 *
 * WHY THIS EXISTS
 * ---------------
 * electron-builder's final packaging step copies the stock `electron`
 * binary into `release/<platform>-unpacked/` and then renames it to the
 * product name (`Hermes`). If a PREVIOUS `npm run pack` was interrupted
 * (Ctrl-C, OOM kill, crash, full disk) the unpacked directory is left in a
 * corrupted partial state: it keeps the already-renamed `LICENSE.electron.txt`
 * and the Chromium payload (.pak/.so/icudtl.dat/chrome-sandbox) but is MISSING
 * the `electron` binary itself.
 *
 * On the next run, electron-builder sees the destination directory already
 * populated, skips re-copying the binary it thinks is present, then tries to
 * rename a `electron` file that no longer exists. The build dies with:
 *
 *   ENOENT: no such file or directory, rename
 *   '.../release/linux-unpacked/electron' -> '.../release/linux-unpacked/Hermes'
 *
 * This is a hard failure with no obvious cause for the user — `hermes desktop`
 * just prints "Desktop GUI build failed" and the only fix is to manually
 * `rm -rf` the release directory, which a normal user has no way to know.
 *
 * The packaging step is not idempotent across an interrupted run, so we make
 * it idempotent ourselves: wipe the target unpacked directory up front so
 * electron-builder always stages into a clean tree. This is safe — the
 * directory is a pure build artifact that electron-builder fully recreates
 * on every pack; nothing else depends on its prior contents.
 *
 * Cross-platform: the same partial-state trap exists on macOS
 * (the mac-unpacked Hermes.app bundle) and Windows (win-unpacked), so we
 * clean whatever `appOutDir` electron-builder hands us regardless of platform.
 *
 * Best-effort: a cleanup failure must never mask the real build. We log and
 * resolve rather than throw — worst case electron-builder hits the original
 * ENOENT, which is no worse than not having this hook at all.
 *
 * 2. Re-stages node-pty's native files for the ACTUAL target platform/arch
 *    of this pack. `npm run build` already staged node-pty once for the
 *    host machine (see scripts/stage-native-deps.mjs), which is correct for
 *    single-arch builds matching the host. But electron-builder can target
 *    a different arch than the host (cross-build), or pack multiple archs
 *    from one `npm run build` (e.g. `dist:mac` => x64 + arm64). Only this
 *    hook knows the real per-target arch, via `context.arch` /
 *    `context.electronPlatformName` — so it re-stages on top of whatever
 *    `npm run build` left behind, per target, right before files are read
 *    for packing.
 *
 * electron-builder passes a context with:
 *   - appOutDir:            the unpacked app directory about to be staged
 *   - electronPlatformName: 'win32' | 'darwin' | 'linux'
 *   - arch:                 Arch enum (0=ia32, 1=x64, 2=armv7l, 3=arm64, 4=universal)
 */
import { existsSync, rmSync, renameSync, execFileSync } from 'node:fs'
import { execSync } from 'node:child_process'
import path from 'node:path'
import { Arch } from 'electron-builder'
import { stageNodePty, stageGetWindows } from './stage-native-deps.mjs'

// ---------------------------------------------------------------------------
// chrome-sandbox helper — fixes Electron's SUID sandbox permissions
//
// Electron requires chrome-sandbox to be owned by root with mode 4755
// (setuid). electron-builder sets this during pack, but the fix can fail
// when:
//   • the process runs without sudo (e.g. user without NOPASSWD)
//   • the chown target is wrong (e.g. uid changes across reinstalls)
//   • electron-builder's auto-fix runs in a different user namespace
//
// If permissions are wrong, Electron refuses to start the sandboxed
// renderer process and the desktop app hangs or exits immediately.
//
// This function fixes it in-place, best-effort. A failure is non-fatal
// (the app can still start with --no-sandbox).
// ---------------------------------------------------------------------------

function _fix_chrome_sandbox(appOutDir) {
  const sandbox = path.join(appOutDir, 'chrome-sandbox')
  if (!existsSync(sandbox)) {
    return false
  }

  // Fast path: check lstat() first so we don't follow a stale symlink.
  const sb = execFileSync('stat', ['-c', '%u:%g:%a', sandbox], { encoding: 'utf8' }).trim()
  const parts = sb.split(':')
  const uid = parseInt(parts[0], 10)
  const gid = parseInt(parts[1], 10)
  const mode = parseInt(parts[2], 8)

  // Need root ownership and setuid bit set (mode >= 4000).
  if (uid === 0 && (mode & 0o4000)) {
    return true
  }

  try {
    execSync(`sudo chown root:root "${sandbox}"`)
    execSync(`sudo chmod 4755 "${sandbox}"`)
    console.log('[before-pack] ✓ Fixed chrome-sandbox permissions')
    return true
  } catch (err) {
    console.warn(`[before-pack] ⚠ chrome-sandbox fix warning: ${err.message}`)
    return false
  }
}

export function cleanStaleAppOutDir(appOutDir) {
  if (!appOutDir || typeof appOutDir !== 'string') {
    return false
  }
  if (!existsSync(appOutDir)) {
    return false
  }
  // Recursive + force so a half-written tree (read-only bits, partial files)
  // can't block the wipe. retry/maxRetries rides out transient EBUSY on
  // Windows where an AV/indexer may briefly hold a handle.
  rmSync(appOutDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
  return true
}

/**
 * Windows rollback material (#69179): before wiping the previous unpacked
 * tree, preserve it as `<appOutDir>.bak` — but ONLY when it holds the product
 * exe (i.e. it is a previously-working build, not the corrupted partial state
 * cleanStaleAppOutDir exists to remove). If the fresh pack then produces a
 * Hermes.exe that Windows can't load (truncated PE from a corrupt cached
 * Electron zip, wrong arch), the updater's integrity gate in
 * `hermes desktop --build-only` (hermes_cli/main.py
 * `_ensure_desktop_exe_launchable`) restores this .bak instead of leaving the
 * user with "This app can't run on your computer".
 *
 * Returns true when the tree was preserved (appOutDir no longer exists), false
 * when there was nothing worth preserving (caller falls through to the wipe).
 * A rename failure (AV holding a handle) also returns false — the wipe is the
 * safe fallback and matches pre-#69179 behavior exactly.
 */
export function preserveRollbackBackup(appOutDir, productExeName = 'Hermes.exe') {
  if (!appOutDir || typeof appOutDir !== 'string' || !existsSync(appOutDir)) {
    return false
  }
  if (!existsSync(path.join(appOutDir, productExeName))) {
    // Partial/corrupt tree (interrupted prior pack) — not rollback material.
    return false
  }
  const backupDir = `${appOutDir}.bak`
  try {
    rmSync(backupDir, { recursive: true, force: true, maxRetries: 5, retryDelay: 100 })
    renameSync(appOutDir, backupDir)
    return true
  } catch {
    return false
  }
}

export default async function beforePack(context) {
  const appOutDir = context && context.appOutDir
  const platformName = context && context.electronPlatformName
  try {
    // Windows: keep the previous working build as rollback material for the
    // post-build integrity gate (#69179) instead of destroying it. Falls
    // through to the plain wipe when the old tree is partial/corrupt or the
    // rename fails.
    const productExe = `${(context && context.packager?.appInfo?.productFilename) || 'Hermes'}.exe`
    if (platformName === 'win32' && preserveRollbackBackup(appOutDir, productExe)) {
      console.log(`[before-pack] preserved previous unpacked dir for rollback: ${appOutDir}.bak`)
    } else if (cleanStaleAppOutDir(appOutDir)) {
      console.log(`[before-pack] removed stale unpacked dir before staging: ${appOutDir}`)
    }

    // Fix chrome-sandbox permissions AFTER cleaning (before electron-builder
    // stages the fresh tree). This is the critical fix for the #3 bug:
    // if electron-builder's auto-fix failed on a previous pack, the
    // permissions are wrong and the desktop app won't start.
    _fix_chrome_sandbox(appOutDir)
  } catch (err) {
    // Never fail the build over cleanup; surface why so a genuinely stuck
    // directory (permissions, mount) is still diagnosable.
    console.warn(`[before-pack] could not clean ${appOutDir} (${err.message}); continuing`)
  }

  try {
    const platform = context && context.electronPlatformName
    const archName = context && typeof context.arch === 'number' ? Arch[context.arch] : undefined
    if (platform && archName) {
      if (archName === 'universal') {
        console.warn(
          '[before-pack] target arch is "universal" — node-pty has no universal prebuild; ' +
            'staged binary will be whichever single-arch copy npm run build left behind. ' +
            'lipo-merge x64/arm64 .node files manually if you need a true universal build.'
        )
      } else {
        await stageNodePty({ platform, arch: archName })
        console.log(`[before-pack] re-staged node-pty for target ${platform}-${archName}`)
      }
      // The macOS helper is universal, while Windows bindings are arch-specific.
      // Pass the target arch so an ARM64 package never stages an x64 binding.
      stageGetWindows({ platform, arch: archName })
      console.log(`[before-pack] re-staged get-windows for target ${platform}-${archName}`)
    }
  } catch (err) {
    // This one SHOULD fail the build — a missing/wrong native binary for the
    // target arch means a broken package shipped to users, which is worse
    // than a build that fails loudly here.
    throw new Error(`[before-pack] failed to stage native deps for this target: ${err.message}`)
  }
}