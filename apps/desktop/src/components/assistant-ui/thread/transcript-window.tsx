import { createContext, type ReactNode, useContext } from 'react'

export interface TranscriptWindowValue {
  /** Store still has older messages that the runtime window has not materialized. */
  olderAvailable: boolean
  /** At the soft max: older store history exists but cannot be loaded into the UI. */
  historyTruncated: boolean
  /** Expand the assistant-ui window by one page from the session store. */
  expandWindow: () => void
}

const TranscriptWindowContext = createContext<TranscriptWindowValue>({
  olderAvailable: false,
  historyTruncated: false,
  expandWindow: () => {}
})

export function TranscriptWindowProvider({
  children,
  value
}: {
  children: ReactNode
  value: TranscriptWindowValue
}) {
  return <TranscriptWindowContext.Provider value={value}>{children}</TranscriptWindowContext.Provider>
}

export function useTranscriptWindow(): TranscriptWindowValue {
  return useContext(TranscriptWindowContext)
}

/** Decide whether Show earlier pages DOM budget or expands the store window. */
export function resolveShowEarlierAction(
  hiddenCount: number,
  olderAvailable: boolean
): 'dom' | 'window' | null {
  if (hiddenCount > 0) {
    return 'dom'
  }

  if (olderAvailable) {
    return 'window'
  }

  return null
}
