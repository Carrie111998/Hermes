export type LocalBackendRestartResult =
  { ok: true; mode: 'local' } | { ok: false; reason: 'restart-failed'; message: string }

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

export async function restartLocalBackend({
  teardown,
  start,
  notifyApplied
}: {
  teardown: () => Promise<void>
  start: () => Promise<unknown>
  notifyApplied: () => void
}): Promise<LocalBackendRestartResult> {
  try {
    await teardown()
    await start()
    notifyApplied()

    return { ok: true, mode: 'local' }
  } catch (error) {
    notifyApplied()

    return { ok: false, reason: 'restart-failed', message: errorMessage(error) }
  }
}
