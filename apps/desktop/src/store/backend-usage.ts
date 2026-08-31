import { normalizeProfileKey } from '@/store/profile'

export function collectKeptProfileKeys(
  sessions: Array<{ id: string; profile?: null | string }>,
  workingIds: readonly string[],
  attentionIds: readonly string[],
  unknownOwnerProfile = 'default'
): string[] {
  const live = new Set([...workingIds, ...attentionIds])
  const known = new Set(sessions.map(session => session.id))
  const keep = new Set<string>()

  for (const session of sessions) {
    if (live.has(session.id)) {
      keep.add(normalizeProfileKey(session.profile))
    }
  }

  for (const id of live) {
    if (!known.has(id)) {
      keep.add(normalizeProfileKey(unknownOwnerProfile))
    }
  }

  return [...keep]
}
