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

async function applyPrimaryProfileChange({
  cancelAndWait,
  nextProfile,
  previousSshScope,
  reload,
  resetPreviewReach,
  teardownPrimary,
  teardownSsh,
  writeProfile
}) {
  const next = writeProfile(nextProfile)

  if (previousSshScope !== undefined) {
    await cancelAndWait(previousSshScope || '')
    await resetPreviewReach()
    await teardownSsh(previousSshScope)
  }

  await teardownPrimary()
  reload()

  return next
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

async function resolveTerminalConnectionForSender(webContentsId, getTarget, ensureBackend) {
  return resolveTerminalConnection(
    () => getTarget(webContentsId),
    () => ensureBackend(webContentsId)
  )
}

export {
  applyConnectionChange,
  applyPrimaryProfileChange,
  commitConnectionFailure,
  resolveTerminalConnection,
  resolveTerminalConnectionForSender
}
