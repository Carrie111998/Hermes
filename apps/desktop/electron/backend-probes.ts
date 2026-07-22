/**
 * backend-probes.ts
 *
 * Cheap "does this candidate backend actually work" checks used by
 * resolveHermesBackend (main.ts). The resolver walks a ladder of
 * candidates -- bootstrap marker, `hermes` on PATH, system Python with
 * hermes_cli installed -- and historically returned the first candidate
 * whose binary existed on disk. That assumption breaks when a user has
 * a pre-installed Python 3.11-3.13 (so findSystemPython() returns a
 * path) but no hermes_cli in its site-packages: the resolver hands back
 * a backend the spawn step can't actually run, and the user gets a
 * dead-on-arrival "ModuleNotFoundError: No module named 'hermes_cli'"
 * instead of the first-launch installer.
 *
 * These probes give the resolver a way to verify a candidate before
 * trusting it. Failure (non-zero exit, exception, timeout) means "skip
 * this rung, try the next one"; success means "spawn this for real."
 * Falling off the bottom of the ladder lands on the bootstrap-needed
 * sentinel, which is exactly what we want when nothing pre-existing
 * actually works.
 *
 * Both probes are deliberately forgiving:
 *   - 15s timeout with one retry on timeout only. `hermes --version`
 *     measures 2.4-2.9s warm and 4.5s+ under boot-time CPU storms
 *     (login-storm boots with a dozen startup tasks competing), so the
 *     old 5s deadline flaked the PATH rung on perfectly healthy
 *     installs and dumped users into the bootstrap installer. A
 *     genuinely broken shim exits non-zero quickly and still fails the
 *     rung on the first attempt -- only ETIMEDOUT earns the retry.
 *   - stdio ignored (we only care about exit code; stdout/stderr are
 *     not surfaced to the user, just to recentHermesLog for forensics
 *     via the caller's catch block if it chooses)
 *   - any throw -> false (never propagate -- resolver wants a boolean)
 *
 * Kept in a standalone ts module so it can be unit-tested with
 * `node --test` without dragging in the electron runtime (same pattern
 * as bootstrap-platform.ts and hardening.ts).
 */

import { execFileSync } from 'node:child_process'

const PROBE_TIMEOUT_MS = 15000
const PROBE_TIMEOUT_RETRIES = 1

// Narrower shape than `typeof execFileSync` (which is a heavily overloaded
// signature TS won't match a plain 3-arg test double against). Both real
// callers and the test seam only ever call it the one way: (command, args,
// options) -> ignored return value, throws on failure.
type ExecFileSyncLike = (command: string, args: readonly string[], options?: unknown) => unknown

// spawnSync surfaces a timeout kill as an error with code ETIMEDOUT (the
// child never got to exit on its own); a real exit lands in err.status and
// a missing binary in code ENOENT. Only the timeout is ambiguous about the
// candidate's health, so only the timeout is worth a second attempt.
function isProbeTimeout(err): boolean {
  return Boolean(err) && (err.code === 'ETIMEDOUT' || err.errno === 'ETIMEDOUT')
}

function runProbe(exec: ExecFileSyncLike, command: string, args: string[], options: unknown): boolean {
  for (let attempt = 0; ; attempt++) {
    try {
      exec(command, args, options)

      return true
    } catch (err) {
      if (isProbeTimeout(err) && attempt < PROBE_TIMEOUT_RETRIES) {
        continue
      }

      return false
    }
  }
}

/**
 * Return the Python snippet used to verify Hermes can import far enough to
 * launch the CLI. Kept exported for tests so dependency regressions are
 * caught without needing a real broken venv fixture.
 *
 * @returns {string}
 */
function hermesRuntimeImportProbe() {
  return 'import yaml; import dotenv; import hermes_cli.config'
}

/**
 * Return true iff the Hermes runtime import probe exits 0.
 *
 * Used to gate the "fallback to system Python with hermes_cli installed"
 * rung of resolveHermesBackend. Without this, a system Python 3.11-3.13
 * registered in PEP 514 makes findSystemPython() succeed regardless of
 * whether hermes_cli has actually been pip-installed into its
 * site-packages -- and the resolver returns a backend that immediately
 * dies on spawn.
 *
 * The probe intentionally imports hermes_cli.config, not just the top-level
 * package: a broken/empty Windows launcher venv can still see the source tree
 * through PYTHONPATH but lack PyYAML, then die on the first real CLI import.
 *
 * @param {string} pythonPath - Absolute path to a python.exe / python.
 * @param {object} [opts.env] - Additional environment for the probe.
 * @param {Function} [opts._execFileSync] - Test seam.
 * @returns {boolean}
 */
function canImportHermesCli(
  pythonPath: string,
  opts: { env?: Record<string, string>; _execFileSync?: ExecFileSyncLike } = {}
) {
  if (!pythonPath) {
    return false
  }

  return runProbe(opts._execFileSync || execFileSync, pythonPath, ['-c', hermesRuntimeImportProbe()], {
    env: { ...process.env, ...(opts.env || {}) },
    stdio: 'ignore',
    timeout: PROBE_TIMEOUT_MS,
    windowsHide: true
  })
}

/**
 * Return true iff `<hermesCommand> --version` exits 0.
 *
 * Used to gate the "existing `hermes` on PATH" rung. Without this, a
 * stale hermes.cmd shim left behind by an uninstalled pip install (or
 * a half-built venv whose `hermes` entry-point points at a deleted
 * Python) survives findOnPath() and gets selected as the backend.
 *
 * We intentionally avoid invoking the command with the dashboard args
 * here -- `--version` is the cheapest "is this binary alive" smoke
 * test that every hermes_cli entry-point has supported since 0.1.
 *
 * @param {string} hermesCommand - Resolved absolute path to a hermes
 *   executable (or an interpreter+script wrapper).
 * @param {boolean} [opts.shell] - Whether to run through a shell. For
 *   .cmd/.bat shims on Windows execFileSync needs shell:true to find
 *   the cmd interpreter; mirrors the same flag isCommandScript() drives
 *   in resolveHermesBackend.
 * @param {Function} [opts._execFileSync] - Test seam.
 * @returns {boolean}
 */
function verifyHermesCli(hermesCommand: string, opts?: { shell?: boolean; _execFileSync?: ExecFileSyncLike }) {
  if (!hermesCommand) {
    return false
  }

  return runProbe(opts?._execFileSync || execFileSync, hermesCommand, ['--version'], {
    stdio: 'ignore',
    timeout: PROBE_TIMEOUT_MS,
    shell: Boolean(opts?.shell),
    windowsHide: true
  })
}

export { canImportHermesCli, hermesRuntimeImportProbe, PROBE_TIMEOUT_MS, verifyHermesCli }
