import fs from 'node:fs'
import path from 'node:path'

import { isValidProfileName } from './profile-name'

/**
 * Named profiles on a shared global remote (SSH/URL) live on the remote host.
 * They must be openable without a local ~/.hermes/profiles/<name> directory.
 */
export function configuredProfileExistsOnSharedRemote(profile: string, globalRemote: boolean): boolean {
  return Boolean(globalRemote) && isValidProfileName(profile)
}

/** True when a target names the root profile or an existing profile under HERMES_HOME. */
export function isProfileTargetAvailable(
  profile: string,
  hermesHome: string,
  exists: (candidate: string) => boolean = fs.existsSync
): boolean {
  if (profile === 'default') {
    return true
  }

  if (!isValidProfileName(profile)) {
    return false
  }

  return exists(path.join(hermesHome, 'profiles', profile))
}
