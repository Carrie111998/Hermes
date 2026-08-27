/** Canonical profile identity: trimmed, with empty values mapped to the default profile. */
export function normalizeProfileKey(name: null | string | undefined): string {
  const value = (name ?? '').trim()

  return value || 'default'
}
