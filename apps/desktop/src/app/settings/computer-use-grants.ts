import { profileScopeCacheKey, type ProfileScope } from '@/api/client'

const liveGrants = new Map<string, string>()

function key(profile?: ProfileScope): string {
  return profileScopeCacheKey(profile)
}

export function rememberComputerUseGrant(profile: ProfileScope | undefined, name: string): void {
  liveGrants.set(key(profile), name)
}

export function peekComputerUseGrant(profile?: ProfileScope): string | undefined {
  return liveGrants.get(key(profile))
}

export function forgetComputerUseGrant(profile?: ProfileScope): void {
  liveGrants.delete(key(profile))
}

export function resetComputerUseGrantLedger(): void {
  liveGrants.clear()
}
