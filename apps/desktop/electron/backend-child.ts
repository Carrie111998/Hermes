/**
 * backend-child.ts
 *
 * Teardown for the desktop's managed backend child process.
 *
 * ONE RULE, and it is the whole point of this module after #89614: a mutator
 * may act only on authority it still holds. A retained `ChildProcess` handle
 * is such authority. A bare pid is NOT -- the OS may have reaped the process
 * and handed the number to somebody else.
 *
 * So `stopBackendChild` signals through the handle and nothing else. It no
 * longer tree-kills by pid on Windows, and no longer group-signals by pid on
 * POSIX, because both reconstruct authority from a number.
 *
 * The cost is real and accepted. `child.kill()` reaches only the direct child,
 * so on Windows a backend's grandchildren (a hermes REPL, a pty terminal, the
 * gateway, MCP servers) survive this call and can keep venv files locked.
 * That residue is contained downstream rather than escalated: the update
 * hand-off polls the venv shim and ABORTS if it stays locked, instead of
 * reaching for a pid kill. Leaving residue is acceptable; mutating a recycled
 * pid is not.
 *
 * Reaching a whole tree with handle-grade authority needs a kernel object that
 * owns the tree -- a Windows Job Object, or a process group we hold a handle
 * to -- not a pid. That is deliberately out of scope here.
 *
 * `stopBackendTreesForUpdate` still tree-kills, because no handle can reach a
 * grandchild, but only for a root whose handle PROVES it is still live.
 *
 * Extracted into its own dependency-free module (no electron import) so the
 * teardown contract can be asserted directly with fake child objects and spy
 * kill functions, instead of grepping main.ts source text for the function
 * body.
 */

export interface StopBackendTreesForUpdateDeps {
  /** Synchronous Windows taskkill /T /F implementation. */
  forceKillProcessTree: (pid: number) => void
  /** Clears and stops the desktop's pooled backends. */
  stopAllPoolBackends: () => void
}

export interface BackendProcessRoot {
  pid?: number | null
  /**
   * Node sets these the moment the child is reaped, and leaves `pid` in place
   * forever. They are therefore the ONLY reliable liveness signal on a
   * `ChildProcess` handle -- see `isLiveProcessRoot`.
   */
  exitCode?: null | number
  signalCode?: null | string
}

/**
 * True when this handle still refers to a RUNNING process.
 *
 * `child.pid` is not a liveness check. Node keeps the number after the child
 * is reaped -- `Number.isInteger(child.pid)` stays true, and `child.killed`
 * stays *false* for a child that exited on its own rather than being
 * signalled. So every `Number.isInteger(pid)` guard in this file used to pass
 * for a dead child, and the caller then force-killed that number.
 *
 * On Windows that is not a harmless no-op. PIDs are recycled aggressively, so
 * `taskkill /PID <recycled> /T /F` kills whatever process inherited the
 * number. When the winner is a protected system process the kernel raises
 * bugcheck 0xEF (CRITICAL_PROCESS_DIED) -- a blue screen, attributed by WER to
 * `taskkill.exe` (#89614).
 *
 * Fail-closed by design: liveness must be WITNESSED, never assumed. The
 * witness is the pair of lifecycle fields Node maintains on a `ChildProcess`.
 * A record carrying neither -- a bare `{ pid }` -- is a number with no
 * evidence of authority behind it, so it reads as NOT live. The strict
 * `=== null` on BOTH fields is what enforces that: `undefined` (field absent)
 * is not `null`, so an evidence-free record cannot be mistaken for a live one.
 * Node sets whichever field applies before emitting 'exit', so a handle we
 * still own answers truthfully.
 *
 * This NARROWS the stale-pid window; it does not close it, and no in-process
 * predicate can. Between this check and the moment `taskkill` resolves the
 * number inside the kernel, the process can exit and the pid be reassigned.
 * That residual race is why pid-based mutation is confined to the one caller
 * that cannot avoid it, and why the handle path below does not use this
 * function at all.
 */
export function isLiveProcessRoot(root: BackendProcessRoot | null | undefined): boolean {
  if (!root || !Number.isInteger(root.pid) || (root.pid as number) <= 0) {
    return false
  }

  return root.exitCode === null && root.signalCode === null
}

/**
 * The PIDs of every backend root that is still running, for a Windows
 * tree-kill sweep.
 *
 * The update hand-off re-collects and re-kills on every pass of its wait loop,
 * which is what made the stale-PID window so easy to hit: pass 1 kills the
 * children, and passes 2..N re-issue `taskkill` against the same numbers,
 * which by then may belong to unrelated processes. Filtering here means a
 * reaped root drops out of the sweep instead of being re-killed forever.
 */
export function collectLiveStragglerPids(
  primary: BackendProcessRoot | null | undefined,
  poolRoots: Iterable<BackendProcessRoot | null | undefined>
): number[] {
  const pids: number[] = []

  if (isLiveProcessRoot(primary)) {
    pids.push((primary as BackendProcessRoot).pid as number)
  }

  for (const root of poolRoots) {
    if (isLiveProcessRoot(root)) {
      pids.push(root!.pid as number)
    }
  }

  return pids
}

export interface KillableChild extends BackendProcessRoot {
  killed?: boolean
  kill: (signal: string) => void
}

/**
 * Stop a managed child process, using ONLY the authority we still hold.
 *
 * `child.kill()` acts through Node's retained handle, so it is safe by
 * construction: once the child is reaped Node has already dropped the
 * underlying handle, and the call issues no syscall against the number at all.
 * It therefore cannot reach a process that merely inherited the pid.
 *
 * No liveness precondition is applied here, deliberately. `isLiveProcessRoot`
 * guards pid-based mutators and this is not one; gating the handle path on it
 * would only stop us signalling a child whose pid never materialised (a failed
 * spawn), which is both harmless and worth attempting.
 *
 * What is gone, on purpose (#89614):
 *   - the Windows `forceKillProcessTree(child.pid)` branch, and
 *   - the POSIX `process.kill(-child.pid, ...)` process-group branch.
 *
 * Both mutated by number. See the module header for what removing them costs
 * and where the resulting residue is contained.
 */
export function stopBackendChild(child: KillableChild | null | undefined) {
  if (!child || child.killed) {
    return
  }

  try {
    child.kill('SIGTERM')
  } catch {
    // Already gone.
  }
}

/**
 * Stop every backend tree owned by a Windows Desktop update hand-off.
 *
 * Tree-kill the primary root while its PID is still live, then delegate pool
 * teardown to the existing routine that tree-kills each pooled root exactly
 * once before mutating its registry. In particular, do not signal the primary
 * first: if that root exits before taskkill /T runs, Windows can no longer
 * enumerate its MCP grandchildren and they survive with the venv locked.
 */
export function stopBackendTreesForUpdate(
  primary: BackendProcessRoot | null | undefined,
  deps: StopBackendTreesForUpdateDeps
): void {
  // Live-only: a reaped primary still carries its pid, and tree-killing that
  // number after Windows recycled it is how this path blue-screened (#89614).
  if (isLiveProcessRoot(primary)) {
    deps.forceKillProcessTree(primary!.pid as number)
  }

  deps.stopAllPoolBackends()
}
