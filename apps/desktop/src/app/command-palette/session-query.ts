import type { QueryFunctionContext } from '@tanstack/react-query'

import { listAllProfileSessions } from '@/api/sessions'

export function commandPaletteSessionsQueryFn(
  { signal }: Pick<QueryFunctionContext, 'signal'>,
  archived: boolean
) {
  return listAllProfileSessions(200, archived ? 0 : 1, archived ? 'only' : 'exclude', 'recent', 'all', {}, signal)
}
