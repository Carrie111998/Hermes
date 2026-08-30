/**
 * ssh-process-teardown.ts
 *
 * Issue #94959: bot/profile deletion left the local SSH child process alive
 * after `state.ssh.close()` returned, holding the remote python grandchildren
 * alive through the SSH forwarding. The user-visible symptom: orphaned
 * python.exe (2) entries in Windows Task Manager, one per deleted bot.
 *
 * `state.ssh.close()` closes the SSH channel but, depending on the SSH
 * client's transport, may NOT reap the local `ssh` binary or its
 * forwarded ports. Tree-kill the local PID BEFORE closing the tunnel so
 * every descendant (the local ssh, its port-forward listeners, and any
 * python that the remote dashboard has tunneled through it) dies first.
 *
 * Extracted into its own dependency-free module so the teardown order and
 * pid-vs-tunnel ordering is asserted directly with a fake `ssh` and a
 * spy `forceKillProcessTree`, instead of grepping main.ts source text for
 * the function body.
 */

export interface SshConnectionStateLike {
  pid?: number | null
  localPort?: number | null
  remotePort?: number | null
  ssh: {
    cancelForward?: (localPort: number, remotePort: number) => Promise<void>
    close: () => Promise<void>
  }
}

export interface TeardownSshProcessDeps {
  /**
   * Windows tree-kill implementation (real: taskkill /T /F via execFileSync).
   * On POSIX the local ssh child is killed via `process.kill(pid, 'SIGKILL')`
   * because the local binary is a normal child of the desktop process group,
   * not a session leader.
   */
  forceKillProcessTree: (pid: number) => void
  /** Injectable so tests can detect silent no-ops (no PID recorded). */
  isWindows?: boolean
}

/**
 * Tear down an SSH connection: tree-kill the local ssh child FIRST (so the
 * remote dashboard's python children cannot outlive the tunnel), then close
 * the SSH channel + cancel the forwarded port.
 *
 * Returns the local PID that was tree-killed, or `null` when the state
 * carries no usable PID — callers should treat a null return as a signal
 * that no process kill was performed and rely solely on the channel close
 * for the connection cleanup.
 */
export function teardownSshConnectionProcessTree(
  state: SshConnectionStateLike | null | undefined,
  deps: TeardownSshProcessDeps
): number | null {
  if (!state) {
    return null
  }

  const pid = Number.isInteger(state.pid) ? (state.pid as number) : null

  if (pid !== null && pid > 0) {
    try {
      deps.forceKillProcessTree(pid)
    } catch {
      // Best-effort: the channel close below is the second-line cleanup.
    }

    return pid
  }

  return null
}

/**
 * Full teardown sequence. Tree-kill first, then cancel the forwarded port,
 * then close the channel. The pid kill is fire-and-forget (taskkill /T /F
 * is synchronous on Windows and SIGKILL is synchronous on POSIX); the
 * awaited close() lets the ssh client drain its control channel.
 *
 * The returned pid is the one that was tree-killed, or `null` if the state
 * had no usable pid (legacy or already-cleaned-up connection).
 */
export async function teardownSshConnectionWithProcessTree(
  state: SshConnectionStateLike | null | undefined,
  deps: TeardownSshProcessDeps
): Promise<number | null> {
  const killed = teardownSshConnectionProcessTree(state, deps)

  if (!state) {
    return killed
  }

  try {
    if (state.localPort && state.remotePort && state.ssh.cancelForward) {
      await state.ssh.cancelForward(state.localPort, state.remotePort)
    }
  } catch {
    // best effort
  }

  try {
    await state.ssh.close()
  } catch {
    // best effort
  }

  return killed
}
