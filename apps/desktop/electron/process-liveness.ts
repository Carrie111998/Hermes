// Cheap native liveness check used to short-circuit dead-PID backend probes.
//
// On Windows, probing a dead PID with a cold PowerShell spawn (Get-Process /
// Get-CimInstance) exits non-zero, which execText surfaces as a generic Error
// whose code is the exit code (e.g. '1') rather than 'ESRCH'/'ENOENT'. The
// probe catch blocks only map those two codes to `false` (gone), so a dead
// Windows backend is never reaped and gets re-probed on every launch — N cold
// PowerShell spawns per boot. Node's process.kill(pid, 0) is a native, sub-penny
// existence test that works on Windows and POSIX: it throws 'ESRCH' when the
// process does not exist and throws 'EPERM' (or succeeds) when it exists but is
// not inspectable. We treat only 'ESRCH' as "definitely dead"; everything else
// (including 'EPERM') means the process may still exist and must fall through to
// the normal platform-specific probe so start-marker/PID-reuse verification is
// never skipped for live processes.

export function isPidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0)
    return true
  } catch (error) {
    return (error as NodeJS.ErrnoException)?.code !== 'ESRCH'
  }
}

// Error with code === 'ESRCH', matching the code the probe catch blocks
// (processIdentityMatches / backendParentMatches) already map to `false`, which
// reapOrphans interprets as "reap this dead backend".
export function deadPidError(): Error {
  return Object.assign(new Error('PID no longer exists'), { code: 'ESRCH' })
}
