import { atom, computed, type ReadableAtom } from 'nanostores'

// NOTE: use-prompt-actions pulls the session/store graph (transcribe, prompts,
// preview-status...) — it is intentionally NOT imported at top level because
// this factory is exported from the SDK index, and a static import would drag
// that graph into every SDK consumer (regression: profile-routing.test.ts
// failed on import). It is loaded lazily inside stageOne().
import { extractDroppedFiles, isImagePath, partitionDroppedFiles } from '@/app/chat/hooks/use-composer-actions'
import type { GatewayRequest } from '@/app/session/hooks/use-prompt-actions/utils'
import { attachmentDisplayText, attachmentId, pathLabel } from '@/lib/chat-runtime'
import { selectDesktopPaths } from '@/lib/desktop-fs'
import {
  type ComposerAttachment,
  createComposerAttachmentOccurrenceId,
  createComposerAttachmentScope
} from '@/store/composer'

export type AttachmentControllerErrorCode = 'canonical-ref-missing' | 'invalid-snapshot' | 'invalid-target' | 'stale'

export class AttachmentControllerError extends Error {
  readonly code: AttachmentControllerErrorCode

  constructor(code: AttachmentControllerErrorCode, message: string) {
    super(message)
    this.code = code
    this.name = 'AttachmentControllerError'
  }
}

export interface AttachmentControllerItem {
  /** Opaque identity for this exact add occurrence. Never a source path. */
  readonly id: string
  readonly kind: 'file' | 'image'
  readonly label: string
  readonly status: 'error' | 'ready' | 'staging'
  /** Deliberately generic: source paths and transport payloads stay private. */
  readonly error?: 'Attachment staging failed.'
}

export interface AttachmentControllerSnapshot {
  readonly attachments: readonly AttachmentControllerItem[]
  readonly contextKey: string
}

export interface AttachmentPickerOptions {
  defaultPath?: string
  filters?: Array<{ extensions: string[]; name: string }>
  title?: string
}

export interface AttachmentAddResult {
  added: number
  rejected: number
}

export interface AttachmentStageTarget {
  backendCwd?: null | string
  /** True when the target gateway does not share Desktop's filesystem. */
  remote: boolean
  /** Credential-free request door already bound to the target route/profile. */
  requestGateway: GatewayRequest
  /** Stable route identity, including connection + profile when applicable. */
  routeKey: string
  /** Current live runtime id for this target. */
  sessionId: string
  /** Durable session id used by core's existing stale-runtime recovery. */
  storedSessionId?: null | string
  terminalBackend?: string
  /** Notified only after a non-stale recovered stage has been accepted. */
  onSessionRecovered?: (sessionId: string) => void
}

export interface StagedAttachment {
  /** The opaque occurrence id from the immutable snapshot. */
  readonly id: string
  readonly kind: 'file' | 'image'
  readonly label: string
  /** Gateway-canonical @file:/@image: ref. Never source bytes or a data URL. */
  readonly refText: string
}

export interface AttachmentStageResult {
  readonly attachments: readonly StagedAttachment[]
  readonly sessionId: string
}

export interface AttachmentControllerOptions {
  /** Channel/draft identity. Change it to invalidate late async completions. */
  contextKey?: string
}

export interface AttachmentController {
  readonly $attachments: ReadableAtom<readonly AttachmentControllerItem[]>
  /** Synchronously consumes DataTransfer; path-only in-app refs fail closed. */
  addDropped(transfer: DataTransfer): AttachmentAddResult
  clear(): void
  pickFiles(options?: AttachmentPickerOptions): Promise<AttachmentAddResult>
  remove(id: string): boolean
  setContext(contextKey: string): void
  snapshot(): AttachmentControllerSnapshot
  stage(snapshot: AttachmentControllerSnapshot, target: AttachmentStageTarget): Promise<AttachmentStageResult>
}

interface SnapshotRecord {
  attachments: readonly ComposerAttachment[]
  contextVersion: number
}

interface InternalStagedAttachment {
  attachment: StagedAttachment
  sessionId: string
}

type TargetStageState = 'error' | 'staging'

const STAGING_ERROR = 'Attachment staging failed.' as const

function freezeItem(attachment: AttachmentControllerItem): AttachmentControllerItem {
  return Object.freeze(attachment)
}

function freezeItems(attachments: AttachmentControllerItem[]): readonly AttachmentControllerItem[] {
  return Object.freeze(attachments)
}

function targetIdentity(target: AttachmentStageTarget, sessionId = target.sessionId): string {
  return `${target.routeKey.trim()}\u0000${sessionId.trim()}\u0000${target.storedSessionId?.trim() || ''}`
}

function publicKind(attachment: ComposerAttachment): 'file' | 'image' {
  return attachment.kind === 'image' ? 'image' : 'file'
}

/**
 * Optional generic Desktop attachment capability for plugin-owned composers.
 *
 * Raw source paths remain inside this controller. Picker/drop ingestion reuses
 * the core partition, and staging delegates to `uploadComposerAttachment`, so
 * local/remote byte limits, cross-filesystem behavior, session recovery, and
 * canonical refs have one authority. Consumers feature-detect this factory.
 */
export function createAttachmentController(options: AttachmentControllerOptions = {}): AttachmentController {
  const scope = createComposerAttachmentScope()
  const snapshots = new WeakMap<object, SnapshotRecord>()
  const stageCache = new Map<string, Map<string, InternalStagedAttachment>>()
  const stageInFlight = new Map<string, Map<string, Promise<InternalStagedAttachment>>>()
  const targetStates = new Map<string, Map<string, TargetStageState>>()
  const $statusVersion = atom(0)
  let contextKey = options.contextKey ?? ''
  let contextVersion = 0

  const occurrenceId = (attachment: ComposerAttachment): string => {
    if (!attachment.occurrenceId) {
      throw new AttachmentControllerError('invalid-snapshot', 'Attachment occurrence identity is missing.')
    }

    return attachment.occurrenceId
  }

  const isCurrent = (attachment: ComposerAttachment, expectedContextVersion: number): boolean =>
    expectedContextVersion === contextVersion &&
    scope.$attachments
      .get()
      .some(current => current.id === attachment.id && current.occurrenceId === attachment.occurrenceId)

  const stageStatus = (id: string): AttachmentControllerItem['status'] => {
    const states = targetStates.get(id)

    if (!states?.size) {
      return 'ready'
    }

    if ([...states.values()].some(state => state === 'error')) {
      return 'error'
    }

    return 'staging'
  }

  const $attachments = computed([scope.$attachments, $statusVersion], attachments =>
    freezeItems(
      attachments.map(attachment => {
        const id = occurrenceId(attachment)
        const status = stageStatus(id)

        return freezeItem({
          id,
          kind: publicKind(attachment),
          label: attachment.label,
          status,
          ...(status === 'error' ? { error: STAGING_ERROR } : {})
        })
      })
    )
  )

  const bumpStatus = () => $statusVersion.set($statusVersion.get() + 1)

  const setTargetState = (id: string, targetKey: string, state?: TargetStageState) => {
    let states = targetStates.get(id)

    if (state) {
      states ??= new Map()
      states.set(targetKey, state)
      targetStates.set(id, states)
    } else if (states) {
      states.delete(targetKey)

      if (states.size === 0) {
        targetStates.delete(id)
      }
    }

    bumpStatus()
  }

  const pruneOccurrence = (id: string) => {
    stageCache.delete(id)
    stageInFlight.delete(id)
    targetStates.delete(id)
    bumpStatus()
  }

  const addPath = (path: string, hintedKind?: 'file' | 'image'): boolean => {
    if (!path.trim()) {
      return false
    }

    const kind = hintedKind ?? (isImagePath(path) ? 'image' : 'file')
    const internalId = attachmentId(kind, path)
    const previous = scope.$attachments.get().find(attachment => attachment.id === internalId)

    if (previous?.occurrenceId) {
      pruneOccurrence(previous.occurrenceId)
    }

    scope.add({
      id: internalId,
      kind,
      label: pathLabel(path),
      occurrenceId: createComposerAttachmentOccurrenceId(),
      path
    })

    return true
  }

  const assertSnapshotCurrent = (record: SnapshotRecord) => {
    if (record.contextVersion !== contextVersion || record.attachments.some(attachment => !isCurrent(attachment, record.contextVersion))) {
      throw new AttachmentControllerError('stale', 'Attachment snapshot is no longer current.')
    }
  }

  const stageOne = async (
    attachment: ComposerAttachment,
    target: AttachmentStageTarget,
    expectedContextVersion: number,
    liveSessionId: string
  ): Promise<InternalStagedAttachment> => {
    const id = occurrenceId(attachment)
    const requestedTargetKey = targetIdentity(target, liveSessionId)
    const cached = stageCache.get(id)?.get(requestedTargetKey)

    if (cached) {
      return cached
    }

    const inFlight = stageInFlight.get(id)?.get(requestedTargetKey)

    if (inFlight) {
      return inFlight
    }

    setTargetState(id, requestedTargetKey, 'staging')

    const task = (async () => {
      let recoveredSessionId: string | undefined

      try {
        // Lazy boundary: a static import here would pull the whole
        // session/store graph into every SDK consumer (sdk/index.ts exports
        // this factory). Loaded only when staging actually runs.
        const { uploadComposerAttachment } = await import('@/app/session/hooks/use-prompt-actions')
        const uploaded = await uploadComposerAttachment(attachment, {
          backendCwd: target.backendCwd,
          remote: target.remote,
          requestGateway: target.requestGateway,
          sessionId: liveSessionId,
          storedSessionId: target.storedSessionId,
          terminalBackend: target.terminalBackend,
          onSessionRecovered: sessionId => {
            recoveredSessionId = sessionId
          }
        })

        if (!isCurrent(attachment, expectedContextVersion)) {
          throw new AttachmentControllerError('stale', 'Attachment completion belongs to an obsolete occurrence or context.')
        }

        const refText = attachmentDisplayText(uploaded)

        if (!refText || refText.startsWith('data:')) {
          throw new AttachmentControllerError('canonical-ref-missing', 'Attachment staging returned no canonical ref.')
        }

        const sessionId = uploaded.attachedSessionId || recoveredSessionId || liveSessionId
        const result: InternalStagedAttachment = Object.freeze({
          attachment: Object.freeze({
            id,
            kind: publicKind(attachment),
            label: uploaded.label || attachment.label,
            refText
          }),
          sessionId
        })
        let cache = stageCache.get(id)

        cache ??= new Map()
        cache.set(requestedTargetKey, result)
        cache.set(targetIdentity(target, sessionId), result)
        stageCache.set(id, cache)
        setTargetState(id, requestedTargetKey)

        if (recoveredSessionId) {
          target.onSessionRecovered?.(recoveredSessionId)
        }

        return result
      } catch (error) {
        if (isCurrent(attachment, expectedContextVersion)) {
          setTargetState(id, requestedTargetKey, 'error')
        }

        throw error
      } finally {
        const current = stageInFlight.get(id)
        current?.delete(requestedTargetKey)

        if (current?.size === 0) {
          stageInFlight.delete(id)
        }
      }
    })()
    let inFlightByTarget = stageInFlight.get(id)

    inFlightByTarget ??= new Map()
    inFlightByTarget.set(requestedTargetKey, task)
    stageInFlight.set(id, inFlightByTarget)

    return task
  }

  return {
    $attachments,
    addDropped(transfer) {
      const candidates = extractDroppedFiles(transfer)
      const { inAppRefs, osDrops } = partitionDroppedFiles(candidates)
      let added = 0
      let rejected = inAppRefs.length

      for (const candidate of osDrops) {
        const path = candidate.path

        if (candidate.isDirectory || !path) {
          rejected += 1
          continue
        }

        const kind =
          candidate.file?.type.startsWith('image/') || isImagePath(candidate.file?.name || path) || isImagePath(path)
            ? 'image'
            : 'file'

        if (addPath(path, kind)) {
          added += 1
        } else {
          rejected += 1
        }
      }

      return { added, rejected }
    },
    clear() {
      scope.clear()
      stageCache.clear()
      stageInFlight.clear()
      targetStates.clear()
      bumpStatus()
    },
    async pickFiles(pickerOptions = {}) {
      const paths = await selectDesktopPaths({
        ...pickerOptions,
        directories: false,
        multiple: true
      })
      let added = 0
      let rejected = 0

      for (const path of paths ?? []) {
        if (addPath(path)) {
          added += 1
        } else {
          rejected += 1
        }
      }

      return { added, rejected }
    },
    remove(id) {
      const current = scope.$attachments.get().find(attachment => attachment.occurrenceId === id)

      if (!current) {
        return false
      }

      scope.remove(current.id)
      pruneOccurrence(id)

      return true
    },
    setContext(nextContextKey) {
      if (nextContextKey === contextKey) {
        return
      }

      contextKey = nextContextKey
      contextVersion += 1
      stageCache.clear()
      targetStates.clear()
      bumpStatus()
    },
    snapshot() {
      const attachments = scope.$attachments.get().map(attachment => Object.freeze({ ...attachment }))
      const snapshot = Object.freeze({
        attachments: freezeItems(
          attachments.map(attachment => {
            const id = occurrenceId(attachment)
            const status = stageStatus(id)

            return freezeItem({
              id,
              kind: publicKind(attachment),
              label: attachment.label,
              status,
              ...(status === 'error' ? { error: STAGING_ERROR } : {})
            })
          })
        ),
        contextKey
      })

      snapshots.set(snapshot, { attachments, contextVersion })

      return snapshot
    },
    async stage(snapshot, target) {
      const routeKey = target.routeKey?.trim()
      const sessionId = target.sessionId?.trim()

      if (!routeKey || !sessionId || typeof target.requestGateway !== 'function') {
        throw new AttachmentControllerError('invalid-target', 'Attachment stage target requires route, session, and request.')
      }

      const record = snapshots.get(snapshot)

      if (!record) {
        throw new AttachmentControllerError('invalid-snapshot', 'Attachment snapshot was not created by this controller.')
      }

      assertSnapshotCurrent(record)

      const staged: StagedAttachment[] = []
      let liveSessionId = sessionId

      for (const attachment of record.attachments) {
        const result = await stageOne(attachment, { ...target, routeKey, sessionId }, record.contextVersion, liveSessionId)
        liveSessionId = result.sessionId
        staged.push(result.attachment)
      }

      assertSnapshotCurrent(record)

      return Object.freeze({ attachments: Object.freeze(staged), sessionId: liveSessionId })
    }
  }
}
