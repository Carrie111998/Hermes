import { backendPoolTouchKeys, resolveRemoteBackendRail } from './connection-config'

export function touchPooledBackendEntries(backendPool, profile, touchedAt = Date.now()) {
  for (const key of backendPoolTouchKeys(profile)) {
    const entry = backendPool.get(key)

    if (entry) {
      entry.lastActiveAt = touchedAt
    }
  }
}

export function resolveExplicitBackendRail(config, options: any = {}) {
  return resolveRemoteBackendRail(config, options)
}
