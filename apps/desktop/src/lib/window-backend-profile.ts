export function resolveWindowBackendProfile(
  connectionProfile: null | string | undefined,
  preferredProfile: null | string | undefined
): string {
  return connectionProfile?.trim() || preferredProfile?.trim() || 'default'
}
