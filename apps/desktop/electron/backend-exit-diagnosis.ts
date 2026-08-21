'use strict'

/**
 * backend-exit-diagnosis.ts
 *
 * Recognizes known-cause signatures in the backend's stdout/stderr tail when
 * it exits before becoming ready, and returns an actionable hint to append to
 * the boot-failure message. Dependency-free pure function so it is testable
 * without booting Electron.
 */

// Windows-only CPython error string for a blocked/unloadable native extension
// (.pyd). Seen when Smart App Control, antivirus, or a corrupted venv blocks
// one of Hermes's bundled Python DLLs (e.g. `_sqlite3.pyd`) — the backend
// aborts on import before it can announce its port, so the generic "exited
// before it became ready" message otherwise gives the user no lead at all.
const DLL_LOAD_BLOCKED_PATTERN = /DLL load failed while importing/i

export function describeBackendExitHint(logTail: string): string | null {
  if (DLL_LOAD_BLOCKED_PATTERN.test(logTail)) {
    return (
      'This looks like Windows blocking one of the bundled Python DLLs ' +
      '(Smart App Control, antivirus, or a corrupted install can all do this). ' +
      'See "Common pitfalls" in the Windows guide: ' +
      'https://hermes-agent.nousresearch.com/docs/user-guide/windows-native#common-pitfalls'
    )
  }

  return null
}
