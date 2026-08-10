import fs from 'node:fs'
import path from 'node:path'

import { normalizeDesktopProfile } from './profile-name'

interface EffectivePrimaryProfileOptions {
  desktopProfile: unknown
  hermesHome: string
  readFile?: (file: string, encoding: BufferEncoding) => string
}

/** Keep the resolved owner stable until the primary backend is torn down. */
export function createPrimaryProfileOwner(resolve: () => string) {
  let current: null | string = null

  return {
    get() {
      current ??= resolve()

      return current
    },
    reset() {
      current = null
    }
  }
}

/** Mirror the CLI's profile precedence for an unpinned Desktop backend launch. */
export function resolveEffectivePrimaryProfile({
  desktopProfile,
  hermesHome,
  readFile = (file, encoding) => fs.readFileSync(file, encoding)
}: EffectivePrimaryProfileOptions): string {
  const explicit = normalizeDesktopProfile(desktopProfile)

  if (explicit) {
    return explicit
  }

  const resolvedHome = path.resolve(hermesHome)

  if (path.basename(path.dirname(resolvedHome)) === 'profiles') {
    return normalizeDesktopProfile(path.basename(resolvedHome)) ?? 'default'
  }

  try {
    return normalizeDesktopProfile(readFile(path.join(resolvedHome, 'active_profile'), 'utf8')) ?? 'default'
  } catch {
    return 'default'
  }
}
