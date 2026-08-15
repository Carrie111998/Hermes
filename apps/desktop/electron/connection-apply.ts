// The in-flight soft re-home promise, if any. Two connection-config:apply
// calls for the global/primary scope can arrive back-to-back (e.g. the
// Settings "Apply" button and the cloud-agent "Connect" button are two
// independent UI triggers with independent pending-state guards, so nothing
// stops both firing close together). Without this, a second call would start
// its own teardownPrimary() while the first is still waiting on the real
// process exit, race the "backend stopped" toast suppression back off early
// (surfacing a spurious crash toast for the first's still-pending teardown),
// and fire a second hermes:connection:applied the renderer has no guard
// against. Concurrent callers instead await the one in-flight re-home.
let primaryRehomeInFlight = null

async function applyConnectionChange({
  cancelAndWait,
  isPrimary,
  rehomePrimary = null,
  scope,
  sendApplied,
  stopPool,
  teardownPrimary,
  teardownSsh
}) {
  await cancelAndWait(scope)
  await teardownSsh(scope)

  if (!isPrimary) {
    stopPool(scope)

    return
  }

  if (rehomePrimary) {
    await rehomePrimary()

    return
  }

  // A second call arriving while one is already in flight awaits the same
  // re-home instead of racing its own teardown + notify.
  if (!primaryRehomeInFlight) {
    primaryRehomeInFlight = (async () => {
      try {
        await teardownPrimary()
        sendApplied()
      } finally {
        primaryRehomeInFlight = null
      }
    })()
  }

  await primaryRehomeInFlight
}

function commitConnectionFailure(current, starting, commit) {
  if (current !== starting) {
    return false
  }

  commit()

  return true
}

async function resolveTerminalConnection(getTarget, ensureBackend) {
  let target = getTarget()

  if (target !== 'pending') {
    return target
  }

  await ensureBackend()
  target = getTarget()

  if (target === 'pending') {
    throw new Error('Remote connection is not ready yet. Try again in a moment.')
  }

  return target
}

export { applyConnectionChange, commitConnectionFailure, resolveTerminalConnection }
