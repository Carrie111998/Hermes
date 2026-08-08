import { AssistantRuntimeProvider, type ThreadMessage } from '@assistant-ui/react'
import { type ReactNode, useEffect, useMemo, useState } from 'react'

import { useRuntimeMessageRepository } from '@/app/chat/runtime-repository'
import { Thread } from '@/components/assistant-ui/thread'
import { Button } from '@/components/ui/button'
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet'
import {
  type CompressionLineage,
  getCompressionLineage,
  getCompressionSegmentMessages,
  type SessionInfo,
  type SessionMessage
} from '@/hermes'
import { useI18n } from '@/i18n'
import { toChatMessages } from '@/lib/chat-messages'
import { useIncrementalExternalStoreRuntime } from '@/lib/incremental-external-store-runtime'
import { cn } from '@/lib/utils'

interface ContextLineageProps {
  /** Backend-owned compression projection for the selected sidebar session. */
  lineage: CompressionLineage
  /** Non-virtual lists can expose the compact three-segment expansion. */
  compact?: boolean
  onBranch?: (sessionId: string, profile?: string, lineageSessionId?: string) => void
  profile?: string
  /** Places the trigger in the session row while keeping Sheet state here. */
  renderContent?: (control: ReactNode, compactContent: ReactNode) => ReactNode
}

const NOOP_SET_MESSAGES = (_messages: readonly ThreadMessage[]) => {}

const NOOP_NEW_MESSAGE = async () => {}

function SegmentTranscript({ messages, sessionId }: { messages: SessionMessage[]; sessionId: string }) {
  const chatMessages = useMemo(() => toChatMessages(messages), [messages])

  const messageRepository = useRuntimeMessageRepository(chatMessages)

  const adapter = useMemo(
    () => ({
      isRunning: false,
      messageRepository,
      onNew: NOOP_NEW_MESSAGE,
      setMessages: NOOP_SET_MESSAGES
    }),
    [messageRepository]
  )

  const runtime = useIncrementalExternalStoreRuntime<ThreadMessage>(adapter)

  return (
    <div className="min-h-0 flex-1" data-testid="lineage-transcript">
      <AssistantRuntimeProvider runtime={runtime}>
        <Thread readOnly sessionId={sessionId} />
      </AssistantRuntimeProvider>
    </div>
  )
}

export function ContextLineage(props: ContextLineageProps) {
  const identity = `${props.profile ?? 'default'}\u0000${props.lineage.root_session_id}\u0000${props.lineage.tip_session_id}`

  return <ContextLineageState {...props} key={identity} />
}

function ContextLineageState({ lineage, compact = true, onBranch, profile, renderContent }: ContextLineageProps) {
  const { t } = useI18n()
  const copy = t.sidebar.lineage
  const [open, setOpen] = useState(false)
  const [selectedSegmentId, setSelectedSegmentId] = useState(lineage.tip_session_id)
  const [messagesBySegment, setMessagesBySegment] = useState<Record<string, SessionMessage[]>>({})
  const [errorsBySegment, setErrorsBySegment] = useState<Record<string, true>>({})
  const [loadingSegmentId, setLoadingSegmentId] = useState<string | null>(null)

  const selectedSegment = useMemo(
    () => lineage.segments.find(segment => segment.id === selectedSegmentId) ?? lineage.segments.at(-1),
    [lineage.segments, selectedSegmentId]
  )

  const newestFirst = useMemo(() => [...lineage.segments].reverse(), [lineage.segments])
  const compactSegments = useMemo(() => newestFirst.slice(0, 3), [newestFirst])

  useEffect(() => {
    setSelectedSegmentId(lineage.tip_session_id)
    setMessagesBySegment({})
    setErrorsBySegment({})
    setLoadingSegmentId(null)
  }, [lineage.tip_session_id])

  useEffect(() => {
    if (
      !open ||
      !selectedSegment ||
      selectedSegment.is_tip ||
      Object.hasOwn(messagesBySegment, selectedSegment.id) ||
      errorsBySegment[selectedSegment.id]
    ) {
      return
    }

    let active = true
    const segmentId = selectedSegment.id

    setLoadingSegmentId(segmentId)
    void getCompressionSegmentMessages(lineage.tip_session_id, segmentId, profile)
      .then(result => {
        if (active) {
          setMessagesBySegment(current => ({ ...current, [segmentId]: result.messages }))
        }
      })
      .catch(() => {
        if (active) {
          setErrorsBySegment(current => ({ ...current, [segmentId]: true }))
        }
      })
      .finally(() => {
        if (active) {
          setLoadingSegmentId(current => (current === segmentId ? null : current))
        }
      })

    return () => {
      active = false
    }
  }, [errorsBySegment, lineage.tip_session_id, messagesBySegment, open, profile, selectedSegment])

  const retrySelectedSegment = () => {
    if (!selectedSegment) {
      return
    }

    setErrorsBySegment(current => {
      const next = { ...current }

      delete next[selectedSegment.id]

      return next
    })
  }

  const selectSegment = (segmentId: string) => {
    setSelectedSegmentId(segmentId)
    setOpen(true)
  }

  if (lineage.segments.length < 2) {
    return renderContent ? renderContent(null, null) : null
  }

  const control = (
    <Button
      aria-expanded={open}
      aria-haspopup="dialog"
      aria-label={copy.segments(lineage.segments.length)}
      className="h-5 shrink-0 rounded px-1.5 text-[0.625rem]"
      onClick={() => setOpen(true)}
      size="inline"
      variant="secondary"
    >
      {copy.segments(lineage.segments.length)}
    </Button>
  )

  const compactContent = compact ? (
    <div className="ml-4 grid gap-px border-l border-(--ui-stroke-tertiary) pb-1 pl-2" data-testid="lineage-compact">
      {compactSegments.map(segment => (
        <Button
          aria-current={segment.is_tip ? 'step' : undefined}
          className="justify-start text-left"
          key={segment.id}
          onClick={() => selectSegment(segment.id)}
          size="inline"
          variant="text"
        >
          {segment.is_tip ? copy.current : copy.segment(segment.index)}
        </Button>
      ))}
      <Button className="justify-start" onClick={() => setOpen(true)} size="inline" variant="text">
        {copy.all}
      </Button>
    </div>
  ) : null

  return (
    <>
      {renderContent ? (
        renderContent(control, compactContent)
      ) : (
        <>
          {control}
          {compactContent}
        </>
      )}
      <Sheet onOpenChange={setOpen} open={open}>
        <SheetContent className="w-full sm:max-w-[min(52rem,90vw)]" side="right">
          <SheetHeader>
            <SheetTitle>{copy.title}</SheetTitle>
            <SheetDescription>{copy.segments(lineage.segments.length)}</SheetDescription>
          </SheetHeader>
          <div className="grid min-h-0 flex-1 grid-rows-[auto_minmax(0,1fr)] border-y border-(--ui-stroke-tertiary) sm:grid-cols-[10rem_minmax(0,1fr)] sm:grid-rows-1">
            <div className="flex max-h-32 overflow-auto py-1 sm:max-h-none sm:min-h-0 sm:flex-col sm:overflow-y-auto">
              {newestFirst.map(segment => (
                <Button
                  aria-current={selectedSegment?.id === segment.id ? 'step' : undefined}
                  className={cn(
                    'shrink-0 justify-start text-left',
                    selectedSegment?.id === segment.id && 'bg-(--ui-bg-quaternary)'
                  )}
                  key={segment.id}
                  onClick={() => setSelectedSegmentId(segment.id)}
                  size="sm"
                  variant="ghost"
                >
                  {segment.is_tip ? copy.current : copy.segment(segment.index)}
                </Button>
              ))}
            </div>
            <div className="flex min-h-0 flex-col border-t border-(--ui-stroke-tertiary) sm:border-l sm:border-t-0">
              {selectedSegment?.is_tip ? (
                <p className="p-3 text-sm text-(--ui-text-tertiary)">{copy.current}</p>
              ) : loadingSegmentId === selectedSegment?.id ? (
                <p className="p-3 text-sm text-(--ui-text-tertiary)" role="status">
                  {copy.loading}
                </p>
              ) : selectedSegment && errorsBySegment[selectedSegment.id] ? (
                <div className="grid justify-items-start gap-2 p-3" role="alert">
                  <p className="text-sm text-(--ui-text-tertiary)">{copy.error}</p>
                  <Button onClick={retrySelectedSegment} size="sm" variant="secondary">
                    {t.common.retry}
                  </Button>
                </div>
              ) : selectedSegment && Object.hasOwn(messagesBySegment, selectedSegment.id) ? (
                <>
                  <p className="px-3 pt-3 text-xs text-(--ui-text-tertiary)" role="status">
                    {copy.readOnly}
                  </p>
                  <SegmentTranscript
                    messages={messagesBySegment[selectedSegment.id] ?? []}
                    sessionId={selectedSegment.id}
                  />
                  {onBranch && (
                    <div className="p-3">
                      <Button
                        onClick={() => onBranch(selectedSegment.id, profile, lineage.tip_session_id)}
                        size="sm"
                        variant="secondary"
                      >
                        {copy.branch}
                      </Button>
                    </div>
                  )}
                </>
              ) : null}
            </div>
          </div>
        </SheetContent>
      </Sheet>
    </>
  )
}

/**
 * Selected-row-only server cache. A missing lineage endpoint is an older
 * backend capability, so it intentionally preserves the ordinary row.
 */
export function SelectedContextLineage({
  compact,
  onBranch,
  renderContent,
  session
}: {
  compact: boolean
  onBranch?: (sessionId: string, profile?: string, lineageSessionId?: string) => void
  renderContent?: (control: ReactNode, compactContent: ReactNode) => ReactNode
  session: SessionInfo
}) {
  const requestIdentity = `${session.profile ?? 'default'}\u0000${session.id}`
  const [loaded, setLoaded] = useState<{ identity: string; lineage: CompressionLineage } | null>(null)

  useEffect(() => {
    let active = true

    void getCompressionLineage(session.id, session.profile)
      .then(result => {
        if (active) {
          setLoaded(result.segments.length > 1 ? { identity: requestIdentity, lineage: result } : null)
        }
      })
      .catch(() => {
        // A missing endpoint is expected with an older backend. A later user
        // selection safely retries without changing the live session.
        if (active) {
          setLoaded(null)
        }
      })

    return () => {
      active = false
    }
  }, [requestIdentity, session.id, session.profile])

  const lineage = loaded?.identity === requestIdentity ? loaded.lineage : null

  if (!lineage) {
    return renderContent ? renderContent(null, null) : null
  }

  return (
    <ContextLineage
      compact={compact}
      lineage={lineage}
      onBranch={onBranch}
      profile={session.profile}
      renderContent={renderContent}
    />
  )
}
