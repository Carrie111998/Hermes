'use strict'

const fs = require('node:fs')

// isSpawnablePythonExe — reject Python interpreter candidates that cannot
// actually be handed to child_process.spawn().
//
// The motivating failure (desktop boot EPERM, 2026-07-09): HERMES_DESKTOP_PYTHON
// had been pinned to a Microsoft Store "App Execution Alias" —
//   %LOCALAPPDATA%\Microsoft\WindowsApps\PythonSoftwareFoundation.Python.3.11_*\python.exe
// These aliases are IO_REPARSE_TAG_APPEXECLINK reparse-point stubs with no real
// PE payload; libuv surfaces them through lstat as symbolic links whose target
// resolves into %ProgramFiles%\WindowsApps and cannot be followed by a normal
// process (statSync → EACCES/EPERM). In the desktop's detached, windowless
// launch context, spawn(alias, { shell: false }) throws EPERM. The resolver is
// supposed to fall through to the next rung (findSystemPython → hermes.exe), so
// we must refuse such a candidate rather than return it.
//
// A candidate is treated as NON-spawnable when either:
//   * it is a reparse point / symlink (lstat) that does NOT resolve (statSync)
//     to a real regular file — the app-execution-alias case; or
//   * it is a 0-byte regular file — no interpreter is ever an empty PE.
//
// A genuine venv symlink (common on POSIX) resolves to a real file and is kept.
//
// `options.fs` / `options.log` are injectable for unit tests; production callers
// pass `{ log: rememberLog }` so the skip is visible in the desktop log buffer.
function isSpawnablePythonExe(candidatePath, options = {}) {
  const fsImpl = options.fs || fs
  const log = typeof options.log === 'function' ? options.log : () => {}

  if (!candidatePath || typeof candidatePath !== 'string') {
    return false
  }

  let linkStat
  try {
    linkStat = fsImpl.lstatSync(candidatePath)
  } catch {
    // Can't even lstat it — treat as absent; the caller falls through.
    return false
  }

  if (linkStat.isSymbolicLink()) {
    // Follow the reparse point. A real symlinked interpreter resolves to a
    // regular file; an App Execution Alias throws (EACCES/EPERM) or fails to
    // land on a file.
    let followsToRealFile = false
    try {
      followsToRealFile = fsImpl.statSync(candidatePath).isFile()
    } catch {
      followsToRealFile = false
    }
    if (!followsToRealFile) {
      log(`Ignoring non-spawnable Python (app-execution-alias reparse stub): ${candidatePath}`)
      return false
    }
    return true
  }

  // Not a reparse point: a real python.exe is a multi-KB PE, never empty.
  if (linkStat.size === 0) {
    log(`Ignoring non-spawnable Python (empty stub): ${candidatePath}`)
    return false
  }

  return true
}

module.exports = { isSpawnablePythonExe }
