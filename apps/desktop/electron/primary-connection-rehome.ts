export interface PrimaryConnectionRehomeOptions {
  clearLocalBootstrapFailure: () => void
  mode: string
  notifyConnectionApplied: () => void
  resumeFirstRunRemote: () => boolean
  teardownPrimaryBackend: (options: { soft: boolean }) => Promise<void>
}

function isNonLocalPrimaryMode(mode: string): boolean {
  return mode === 'remote' || mode === 'cloud' || mode === 'ssh'
}

// Production seam shared by the connection-config IPC handler and the
// first-run integration test. A remote apply that resumes the active setup
// gate must keep that connection attempt alive; ordinary mode changes tear the
// current backend down before the renderer is told to reconnect.
export async function rehomePrimaryConnection({
  clearLocalBootstrapFailure,
  mode,
  notifyConnectionApplied,
  resumeFirstRunRemote,
  teardownPrimaryBackend
}: PrimaryConnectionRehomeOptions): Promise<{ resumedFirstRunRemote: boolean }> {
  let resumedFirstRunRemote = false

  if (isNonLocalPrimaryMode(mode)) {
    resumedFirstRunRemote = resumeFirstRunRemote()
    clearLocalBootstrapFailure()
  }

  if (resumedFirstRunRemote) {
    return { resumedFirstRunRemote: true }
  }

  await teardownPrimaryBackend({ soft: true })
  notifyConnectionApplied()

  return { resumedFirstRunRemote: false }
}
