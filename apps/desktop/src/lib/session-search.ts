import { normalize } from '@/lib/text'
import type { SessionInfo } from '@/types/hermes'

import { sessionTitle } from './chat-runtime'
import { bridgeMirrorStateSearchTerms, bridgeProviderSearchTerms, sessionSourceSearchTerms } from './session-source'

export function sessionMatchesSearch(session: SessionInfo, query: string): boolean {
  const needle = normalize(query)

  if (!needle) {
    return true
  }

  return [
    session.id,
    session._lineage_root_id ?? '',
    sessionTitle(session),
    session.preview ?? '',
    session.cwd ?? '',
    ...sessionSourceSearchTerms(session.source),
    ...bridgeProviderSearchTerms(session.bridge_provider),
    ...bridgeMirrorStateSearchTerms(session.bridge_mirror_state)
  ].some(value => value.toLowerCase().includes(needle))
}
