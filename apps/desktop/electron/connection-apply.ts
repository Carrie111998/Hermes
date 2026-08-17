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

  await teardownPrimary()
  sendApplied()
}

function commitConnectionFailure(current, starting, commit) {
  if (current !== starting) {
    return false
  }

  commit()

  return true
}

async function resolveTerminalConnection(identity, getTarget, ensureBackend) {
  let target = getTarget(identity)

  if (target !== 'pending') {
    return target
  }

  await ensureBackend(identity)
  target = getTarget(identity)

  if (target === 'pending') {
    throw new Error('Remote connection is not ready yet. Try again in a moment.')
  }

  return target
}

function terminalConnectionId(value) {
  const connectionId = String(value || '').trim()

  return connectionId || null
}

/** Classify registry terminal transport without silently downgrading a stale
 * persisted connection to a local shell. */
function terminalRegistrySourceKind(connectionId, sources) {
  const source = sources.find(source => source.id === connectionId)

  if (!source) {
    return 'missing'
  }

  return source.kind === 'ssh' ? 'ssh' : 'local'
}

export {
  applyConnectionChange,
  commitConnectionFailure,
  resolveTerminalConnection,
  terminalConnectionId,
  terminalRegistrySourceKind
}
