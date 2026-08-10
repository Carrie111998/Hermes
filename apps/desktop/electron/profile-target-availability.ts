import fs from 'node:fs'
import path from 'node:path'

import { isValidProfileName } from './profile-name'

/** True when a target names the root profile or an existing HOME-anchored profile. */
export function isProfileTargetAvailable(
  profile: string,
  homeDir: string,
  exists: (candidate: string) => boolean = fs.existsSync
): boolean {
  if (profile === 'default') {
    return true
  }

  if (!isValidProfileName(profile)) {
    return false
  }

  return exists(path.join(homeDir, '.hermes', 'profiles', profile))
}
