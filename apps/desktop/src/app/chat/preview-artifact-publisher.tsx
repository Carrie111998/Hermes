import { useStore } from '@nanostores/react'
import { computed } from 'nanostores'
import { memo, useEffect, useMemo } from 'react'

import { useSessionView } from '@/app/chat/session-view'
import {
  isPreviewableTarget,
  isSuccessfulToolPart,
  previewTargetFromToolPart,
  type ToolPart
} from '@/components/assistant-ui/tool/fallback-model'
import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import type { ChatMessage } from '@/lib/chat-messages'
import {
  clearPreviewArtifacts,
  previewArtifactBackendIdentity,
  previewArtifactOwnerId,
  previewArtifactPublicationKey,
  syncPreviewArtifacts
} from '@/store/preview-status'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $connection, $sessions } from '@/store/session'

export interface PreviewArtifactPublication {
  publicationId: string
  target: string
}

// The stream reducer replaces a tool part whenever its args, result, name, or
// error state changes. Cache by that immutable identity so transcript-only
// deltas do not reparse every completed tool call in loaded history.
const publicationByToolPart = new WeakMap<object, PreviewArtifactPublication | null>()

const publicationFromToolPart = (part: ToolPart): PreviewArtifactPublication | null => {
  const cached = publicationByToolPart.get(part)

  if (cached !== undefined) {
    return cached
  }

  if (!part.toolCallId || part.isError || !isSuccessfulToolPart(part)) {
    publicationByToolPart.set(part, null)

    return null
  }

  const target = previewTargetFromToolPart(part)
  const publication = isPreviewableTarget(target) ? { publicationId: part.toolCallId, target } : null
  publicationByToolPart.set(part, publication)

  return publication
}

export function previewArtifactPublications(messages: readonly ChatMessage[]): PreviewArtifactPublication[] {
  const publications: PreviewArtifactPublication[] = []

  for (const message of messages) {
    for (const part of message.parts) {
      if (part.type !== 'tool-call') {
        continue
      }

      const publication = publicationFromToolPart(part)

      if (publication) {
        publications.push(publication)
      }
    }
  }

  return publications
}

const samePublications = (
  left: readonly PreviewArtifactPublication[],
  right: readonly PreviewArtifactPublication[]
): boolean =>
  left.length === right.length &&
  left.every(
    (publication, index) =>
      publication.publicationId === right[index]?.publicationId && publication.target === right[index]?.target
  )

interface PublicationSnapshot {
  messages: readonly ChatMessage[]
  publications: PreviewArtifactPublication[]
  tailPublications: PreviewArtifactPublication[]
}

const publicationSnapshot = (
  messages: readonly ChatMessage[],
  previous?: PublicationSnapshot
): PublicationSnapshot => {
  const tail = messages.at(-1)
  const previousTail = previous?.messages.at(-1)

  // mutateStream preserves every earlier message object and replaces only the
  // live tail. Rescan that message during token flushes; hydration, edits, and
  // structural history changes still take the complete authoritative path.
  if (
    previous &&
    tail &&
    previousTail &&
    messages.length === previous.messages.length &&
    tail !== previousTail &&
    tail.id === previousTail.id &&
    (messages.length === 1 || messages.at(-2) === previous.messages.at(-2))
  ) {
    const tailPublications = previewArtifactPublications([tail])

    if (samePublications(previous.tailPublications, tailPublications)) {
      return { messages, publications: previous.publications, tailPublications: previous.tailPublications }
    }

    return {
      messages,
      publications: [
        ...previous.publications.slice(0, previous.publications.length - previous.tailPublications.length),
        ...tailPublications
      ],
      tailPublications
    }
  }

  const publications = previewArtifactPublications(messages)

  return {
    messages,
    publications: previous && samePublications(previous.publications, publications) ? previous.publications : publications,
    tailPublications: tail ? previewArtifactPublications([tail]) : []
  }
}

interface RuntimePublicationOwner {
  backend: string
  profile: string
}

function ActivePreviewArtifactPublisher() {
  const sessionView = useSessionView()

  const publicationsStore = useMemo(() => {
    let previous: PublicationSnapshot | undefined

    return computed(sessionView.$messages, messages => {
      previous = publicationSnapshot(messages, previous)

      return previous.publications
    })
  }, [sessionView.$messages])

  const publications = useStore(publicationsStore)
  const runtimeId = useStore(sessionView.$runtimeId)
  const storedSessionId = useStore(sessionView.$storedId)
  const cwd = useStore(sessionView.$cwd)
  const sessions = useStore($sessions)
  const connection = useStore($connection)
  const profile = useStore($activeGatewayProfile)
  const runtimeOwners = useMemo(() => new Map<string, RuntimePublicationOwner>(), [])

  useEffect(() => {
    if (!runtimeId) {
      return
    }

    return () => clearPreviewArtifacts(runtimeId)
  }, [runtimeId])

  useEffect(() => {
    if (!runtimeId) {
      return
    }

    if (!connection || !storedSessionId) {
      clearPreviewArtifacts(runtimeId)

      return
    }

    const normalizedProfile = normalizeProfileKey(profile)
    const connectionMode = connection.mode ?? 'local'

    const backend = JSON.stringify([
      connectionMode,
      previewArtifactBackendIdentity({
        baseUrl: connection.baseUrl,
        mode: connectionMode,
        remoteIdentity: connection.remoteIdentity
      })
    ])

    const boundOwner = runtimeOwners.get(runtimeId)

    // Profile or backend selection can move before the outgoing SessionView is
    // replaced. Never reinterpret one mounted runtime's transcript under the
    // destination scope: both may legitimately contain the same stored id.
    if (boundOwner && (boundOwner.profile !== normalizedProfile || boundOwner.backend !== backend)) {
      clearPreviewArtifacts(runtimeId)

      return
    }

    if (!boundOwner) {
      runtimeOwners.clear()
      runtimeOwners.set(runtimeId, { backend, profile: normalizedProfile })
    }

    const ownerId = previewArtifactOwnerId(storedSessionId, sessions, normalizedProfile)

    if (!ownerId) {
      clearPreviewArtifacts(runtimeId)

      return
    }

    syncPreviewArtifacts(
      runtimeId,
      publications.map(publication => ({
        cwd,
        publicationKey: previewArtifactPublicationKey({
          baseUrl: connection.baseUrl,
          mode: connectionMode,
          ownerId,
          profile: normalizedProfile,
          publicationId: publication.publicationId,
          remoteIdentity: connection.remoteIdentity
        }),
        target: publication.target
      }))
    )
  }, [
    connection,
    cwd,
    profile,
    publications,
    runtimeId,
    runtimeOwners,
    sessions,
    storedSessionId
  ])

  return null
}

const ActivePreviewArtifactPublisherMemo = memo(ActivePreviewArtifactPublisher)

function PreviewArtifactPublisherComponent({ disabled = false }: { disabled?: boolean }) {
  const visible = usePaneVisible()

  return disabled || !visible ? null : <ActivePreviewArtifactPublisherMemo />
}

export const PreviewArtifactPublisher = memo(PreviewArtifactPublisherComponent)
