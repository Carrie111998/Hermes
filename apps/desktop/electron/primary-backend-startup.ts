import type { FirstRunSetupDecision } from './first-run-setup-gate'

export interface PrimaryBackendStartupOptions<Backend, RuntimeBackend, Remote, Connection> {
  connectRemote: (remote: Remote) => Promise<Connection>
  ensureLocalRuntime: (backend: Backend) => Promise<RuntimeBackend>
  ensureCurrent: () => void
  prepareLocalBackend: () => Backend | Promise<Backend>
  resolveRemote: () => Promise<Remote | null>
  waitForDecision: (backend: Backend) => Promise<FirstRunSetupDecision>
  waitForLocalStart: () => Promise<unknown>
}

export type PrimaryBackendStartupResult<RuntimeBackend, Connection> =
  { kind: 'local'; backend: RuntimeBackend } | { kind: 'remote'; connection: Connection }

export class FirstRunSetupResetError extends Error {
  readonly firstRunSetupReset = true

  constructor() {
    super('First-run setup was reset before a choice completed.')
    this.name = 'FirstRunSetupResetError'
  }
}

// Owns the production startHermes path up to the local process spawn. Keeping
// the full ordering here makes the first-run remote boundary executable in a
// test: an already-saved remote wins immediately; otherwise update exclusion
// and local backend resolution happen before the setup gate, and a remote Apply
// re-resolves persisted config without ever entering ensureRuntime/bootstrap.
export async function runPrimaryBackendStartup<Backend, RuntimeBackend, Remote, Connection>({
  connectRemote,
  ensureLocalRuntime,
  ensureCurrent,
  prepareLocalBackend,
  resolveRemote,
  waitForDecision,
  waitForLocalStart
}: PrimaryBackendStartupOptions<Backend, RuntimeBackend, Remote, Connection>): Promise<
  PrimaryBackendStartupResult<RuntimeBackend, Connection>
> {
  const savedRemote = await resolveRemote()
  ensureCurrent()

  if (savedRemote) {
    const connection = await connectRemote(savedRemote)
    ensureCurrent()

    return { kind: 'remote', connection }
  }

  await waitForLocalStart()
  ensureCurrent()

  const backend = await prepareLocalBackend()
  ensureCurrent()
  const decision = await waitForDecision(backend)
  ensureCurrent()

  if (decision === 'remote-applied') {
    const appliedRemote = await resolveRemote()
    ensureCurrent()

    if (!appliedRemote) {
      throw new Error('First-run remote setup completed without a saved remote backend.')
    }

    const connection = await connectRemote(appliedRemote)
    ensureCurrent()

    return { kind: 'remote', connection }
  }

  if (decision === 'reset') {
    throw new FirstRunSetupResetError()
  }

  const runtimeBackend = await ensureLocalRuntime(backend)
  ensureCurrent()

  return { kind: 'local', backend: runtimeBackend }
}
