import { createContext, useContext } from 'react'

import { useSessionView } from '@/app/chat/session-view'

export interface TranscriptIdentity {
  cwd: string
  runtimeId: null | string
}

const TranscriptIdentityContext = createContext<TranscriptIdentity | null>(null)

export const TranscriptIdentityProvider = TranscriptIdentityContext.Provider

export function useTranscriptIdentity(): TranscriptIdentity {
  const identity = useContext(TranscriptIdentityContext)
  const view = useSessionView()

  return identity ?? { cwd: view.$cwd.get() || '', runtimeId: view.$runtimeId.get() }
}
