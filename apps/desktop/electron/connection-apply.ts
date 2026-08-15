async function applyConnectionChange({
  cancelAndWait,
  isPrimary,
  rehomePrimary = null,
  scope,
  sendApplied,
  stopProfilePool,
  teardownPrimary,
  teardownSsh
}) {
  await cancelAndWait(scope)
  await teardownSsh(scope)

  if (!isPrimary) {
    await stopProfilePool(scope)

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

async function applySshConnectionTeardown({
  closePublishedConnection,
  retireAndRun,
  scope,
  teardownRelatedState = async () => {}
}) {
  await retireAndRun(scope, () =>
    Promise.all([closePublishedConnection(scope), teardownRelatedState()])
  )
}

async function replacePublishedSshConnection({
  closePublishedConnection,
  existingFingerprint,
  fingerprint,
  scope
}) {
  if (existingFingerprint && existingFingerprint !== fingerprint) {
    await closePublishedConnection(scope)
  }
}

function terminalProfileForTarget(target, primaryProfile) {
  if (target.kind === 'forced-local-profile') {
    return null
  }

  return target.kind === 'primary' ? primaryProfile : target.profile
}

export {
  applyConnectionChange,
  applySshConnectionTeardown,
  commitConnectionFailure,
  replacePublishedSshConnection,
  resolveTerminalConnection,
  terminalProfileForTarget
}
