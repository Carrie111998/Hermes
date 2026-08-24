import crypto from 'node:crypto'
import fs from 'node:fs/promises'

const defaultOperations = fs

async function publishBackendOwnershipLock(lockPath: string, contents: string, operations = defaultOperations) {
  const temporaryLockPath = `${lockPath}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tmp`

  try {
    await operations.writeFile(temporaryLockPath, `${contents}\n`, {
      encoding: 'utf8',
      mode: 0o600,
      flag: 'wx'
    })
    // Publish with a non-overwriting hard link. A concurrent owner keeps the
    // public pathname and makes this link fail with EEXIST instead of being
    // replaced by a delayed creator.
    await operations.link(temporaryLockPath, lockPath)
    await operations.unlink(temporaryLockPath)
  } finally {
    await operations.rm(temporaryLockPath, { force: true }).catch(() => {})
  }
}

// A pathname-only unlink is not a compare-and-delete: another interpreter can
// replace the lock after the read and before the unlink. Rename the exact
// pathname to a private tombstone first, compare the bytes there, and delete
// only that tombstone. If the bytes changed, restore the tombstone with a
// hard-link without ever overwriting a replacement lock at the public path.
async function compareAndDeleteBackendOwnershipLock(
  lockPath: string,
  expectedContents: string,
  operations = defaultOperations
) {
  const tombstonePath = `${lockPath}.${process.pid}.${crypto.randomBytes(8).toString('hex')}.tombstone`

  try {
    await operations.rename(lockPath, tombstonePath)
  } catch (error: any) {
    if (error?.code === 'ENOENT') {
      return false
    }

    throw error
  }

  const restoreTombstone = async () => {
    try {
      await operations.link(tombstonePath, lockPath)
    } catch (error: any) {
      if (error?.code !== 'EEXIST') {
        throw error
      }

      // A replacement already owns the public pathname. The hard link failed
      // without touching that winner, so discard our private tombstone too.
      await operations.unlink(tombstonePath).catch((cleanupError: any) => {
        if (cleanupError?.code !== 'ENOENT') {
          throw cleanupError
        }
      })

      return
    }

    await operations.unlink(tombstonePath)
  }

  let currentContents: string

  try {
    currentContents = await operations.readFile(tombstonePath, 'utf8')
  } catch {
    await restoreTombstone()

    return false
  }

  if (currentContents !== expectedContents) {
    await restoreTombstone()

    return false
  }

  try {
    await operations.unlink(tombstonePath)

    return true
  } catch (error: any) {
    if (error?.code === 'ENOENT') {
      return false
    }

    throw error
  }
}

export { compareAndDeleteBackendOwnershipLock, publishBackendOwnershipLock }
