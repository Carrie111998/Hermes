import { type RefObject, useLayoutEffect, useRef } from 'react'

import { usePaneVisible } from '@/components/pane-shell/pane-visibility'
import { SLASH_COMMAND_RE } from '@/lib/chat-runtime'
import { triggerHaptic } from '@/lib/haptics'
import { hasClarifyRequest } from '@/store/clarify'
import {
  clearSessionDraftIfRevision,
  type ComposerAttachment,
  isSessionDraftRevisionCurrent
} from '@/store/composer'
import { resetBrowseState } from '@/store/composer-input-history'
import { enqueueQueuedPrompt, type QueuedPromptEntry } from '@/store/composer-queue'
import { hasMcpSetupRequest, skipMcpSetupRequest } from '@/store/mcp-setup'
import { hasBlockingPromptRequest } from '@/store/prompts'

import { cloneAttachments, type QueueEditState } from '../composer-utils'
import { onComposerSubmitRequest } from '../focus'
import { pathifyRefs } from '../path-refs'
import { composerPlainText } from '../rich-editor'
import { useComposerScope, useComposerSurfaceId } from '../scope'
import type { ChatBarProps } from '../types'

interface UseComposerSubmitArgs {
  activeQueueSessionKey: string | null
  activeQueueSessionKeyRef: RefObject<string | null>
  attachments: ComposerAttachment[]
  busy: boolean
  compacting: boolean
  clearDraft: (preservePersistedDraft?: boolean) => void
  disabled: boolean
  draftIntentGenerationRef: RefObject<number>
  draftRef: RefObject<string>
  drainNextQueued: () => Promise<boolean>
  editorRef: RefObject<HTMLDivElement | null>
  exitQueuedEdit: (action: 'cancel' | 'save') => boolean
  focusInput: () => void
  inputDisabled: boolean
  loadIntoComposer: (text: string, attachments: ComposerAttachment[]) => void
  onCancel: ChatBarProps['onCancel']
  onSteer: ChatBarProps['onSteer']
  onSubmit: ChatBarProps['onSubmit']
  queueCurrentDraft: () => boolean
  queueEdit: QueueEditState | null
  queuedPrompts: QueuedPromptEntry[]
  releasePersistedDraftReceipt: () => void
  sessionId: string | null | undefined
  setComposerText: (value: string) => void
  stashAt: (scope: string | null, text: string, attachments: ComposerAttachment[]) => number
}

function sameAttachmentReceipt(current: ComposerAttachment[], submitted: ComposerAttachment[]): boolean {
  if (current.length !== submitted.length) {
    return false
  }

  return current.every((attachment, index) => {
    const receipt = submitted[index]!

    return attachment.occurrenceId !== undefined || receipt.occurrenceId !== undefined
      ? attachment.id === receipt.id && attachment.occurrenceId === receipt.occurrenceId
      : attachment.id === receipt.id &&
          attachment.kind === receipt.kind &&
          attachment.path === receipt.path &&
          attachment.refText === receipt.refText
  })
}

/**
 * The composer's submit engine — the orchestration seam where the draft and
 * queue meet. `submitDraft` is the one decision tree (queue-edit save · slash-
 * now-while-busy · queue · drain · send · stop); `dispatchSubmit` is the shared
 * send-with-restore primitive (re-loads + re-stashes the draft if the gateway
 * rejects, so nothing is ever lost); `steerDraft` redirects the live turn. Reads
 * the draft + queue APIs; owns no state of its own beyond the stable
 * external-submit listener ref.
 */
export function useComposerSubmit({
  activeQueueSessionKey,
  activeQueueSessionKeyRef,
  attachments,
  busy,
  compacting,
  clearDraft,
  disabled,
  draftIntentGenerationRef,
  draftRef,
  drainNextQueued,
  editorRef,
  exitQueuedEdit,
  focusInput,
  inputDisabled,
  loadIntoComposer,
  onCancel,
  onSteer,
  onSubmit,
  queueCurrentDraft,
  queueEdit,
  queuedPrompts,
  releasePersistedDraftReceipt,
  sessionId,
  setComposerText,
  stashAt
}: UseComposerSubmitArgs) {
  const paneVisible = usePaneVisible()
  const scope = useComposerScope()
  const surfaceId = useComposerSurfaceId()

  // Shared send primitive: fire onSubmit, and if the gateway rejects (accepted
  // === false) or throws, re-load + re-stash the draft so the words survive.
  const dispatchSubmit = (
    text: string,
    attachments?: ComposerAttachment[],
    displayKind?: 'hidden',
    clearRehydratedReceipt = false,
    ownsComposerReceipt = true
  ) => {
    const submittedScope = activeQueueSessionKeyRef.current
    const submittedAttachments = attachments ?? []
    const receiptIntentGeneration = draftIntentGenerationRef.current

    const submit = () =>
      attachments
        ? onSubmit(text, { attachments, composerScope: submittedScope, ...(displayKind ? { displayKind } : {}) })
        : onSubmit(text, { composerScope: submittedScope, ...(displayKind ? { displayKind } : {}) })

    // Review-pane and other routed submits do not originate in this composer.
    // Their retry/receipt state belongs to the caller; borrowing this session's
    // stash would overwrite an unrelated unsent draft.
    if (!ownsComposerReceipt) {
      void Promise.resolve()
        .then(submit)
        .catch(() => undefined)

      return
    }

    // Keep one durable local copy until the gateway explicitly accepts the
    // submit. The visible editor can clear immediately, but a hung RPC,
    // reconnect, or late rejection must still have words to restore.
    const receiptRevision = stashAt(submittedScope, text, submittedAttachments)

    const restore = () => {
      // A later keystroke/attachment owns newer visible intent even before its
      // debounce advances the persisted revision. Restore only into the still
      // empty submitted composer or over the exact same rehydrated receipt.
      const liveAttachments = scope.attachments.$attachments.get()
      const composerEmpty = !draftRef.current && liveAttachments.length === 0

      const visibleReceiptMatches =
        draftIntentGenerationRef.current === receiptIntentGeneration &&
        draftRef.current === text &&
        sameAttachmentReceipt(liveAttachments, submittedAttachments)

      if (
        isSessionDraftRevisionCurrent(submittedScope, receiptRevision) &&
        draftIntentGenerationRef.current === receiptIntentGeneration &&
        activeQueueSessionKeyRef.current === submittedScope &&
        (composerEmpty || visibleReceiptMatches)
      ) {
        loadIntoComposer(text, submittedAttachments)
      }
    }

    const settleAcceptance = () => {
      const activeScopeMatches = activeQueueSessionKeyRef.current === submittedScope

      if (!activeScopeMatches) {
        clearSessionDraftIfRevision(submittedScope, receiptRevision)

        return
      }

      const liveAttachments = scope.attachments.$attachments.get()
      const composerEmpty = !draftRef.current && liveAttachments.length === 0

      const visibleReceiptMatches =
        clearRehydratedReceipt &&
        draftIntentGenerationRef.current === receiptIntentGeneration &&
        draftRef.current === text &&
        sameAttachmentReceipt(liveAttachments, submittedAttachments)

      // A normal submit leaves the composer empty while the receipt waits in
      // the stash. A session round-trip can repaint that exact receipt; clear
      // both copies on acceptance. Any other visible state is a newer draft.
      if (composerEmpty) {
        // Release clearDraft(true)'s receipt guard once the matching request is
        // accepted, so the next attachment-only draft persists normally.
        releasePersistedDraftReceipt()
      } else if (visibleReceiptMatches) {
        clearDraft()
        scope.attachments.removeOccurrences(submittedAttachments)
      }

      if (composerEmpty || visibleReceiptMatches) {
        clearSessionDraftIfRevision(submittedScope, receiptRevision)
      }
    }

    void Promise.resolve()
      .then(submit)
      .then(accepted => void (accepted === false ? restore() : settleAcceptance()))
      .catch(restore)
  }

  // External "submit this prompt" requests (e.g. the review pane's agent-ship
  // button) route through the same send path. Match both the composer target
  // and the exact visible surface captured at click time — every tile stays
  // mounted, and a session can be rendered in more than one pane.
  const dispatchSubmitRef = useRef(dispatchSubmit)
  dispatchSubmitRef.current = dispatchSubmit

  useLayoutEffect(
    () =>
      onComposerSubmitRequest(({ surfaceId: requestedSurfaceId, target, text, displayKind }) => {
        if (
          target === scope.target &&
          surfaceId !== null &&
          requestedSurfaceId === surfaceId &&
          paneVisible &&
          !inputDisabled
        ) {
          dispatchSubmitRef.current(text, undefined, displayKind, false, false)
        }
      }),
    [inputDisabled, paneVisible, scope.target, surfaceId]
  )

  const submitDraft = () => {
    if (disabled) {
      return
    }

    // Source the text from the DOM editor, not React state. The AUI composer
    // state (`draft`) and the derived `hasComposerPayload` lag the DOM by a
    // render, so on fast typing or IME composition the final keystroke(s) may
    // not have synced yet — reading state here drops the message (Enter looks
    // like it does nothing; typing a trailing space only "fixes" it because the
    // extra input event forces a state sync). draftRef is updated on every
    // input event; refresh it from the editor once more to also cover an
    // in-flight keystroke that hasn't fired its input event yet.
    const editor = editorRef.current

    if (editor) {
      const domText = composerPlainText(editor)

      if (domText !== draftRef.current) {
        draftRef.current = domText
        setComposerText(domText)
      }
    }

    // A path that never got its committing space (`@apps/desktop/` left by a Tab
    // descend, then Enter) is still the reference the user picked — promote it
    // on the way out so it attaches instead of submitting as inert text.
    const text = pathifyRefs(draftRef.current)
    const payloadPresent = text.trim().length > 0 || attachments.length > 0

    // A clarify card is an explicit user decision surface. Typing unrelated
    // prose must not silently answer it with an empty value and tear the card
    // down. Park the prose visibly as the next queued turn; the card remains
    // live until the user chooses an option or presses its explicit Skip.
    const pendingClarify = payloadPresent && !queueEdit && hasClarifyRequest(sessionId)

    if (pendingClarify) {
      queueCurrentDraft()
      focusInput()

      return
    }

    // Same deal for a pending MCP setup card: the agent is blocked on
    // mcp.setup.respond, so a typed message declines the card and rides on.
    if (payloadPresent && !queueEdit && hasMcpSetupRequest(sessionId)) {
      void skipMcpSetupRequest(sessionId)
    }

    // Approval / sudo / secret prompts also park the turn inside a tool batch,
    // but typing CANNOT answer them (no message text approves a command or
    // supplies a password), so there is no skip-and-steer path: a steer would
    // sit undelivered behind the blocked prompt, and stopping the turn to force
    // it through resolves the prompt to empty and ends the turn as "Operation
    // interrupted." — the message looks eaten. Queue the words as the next turn
    // instead; the prompt stays answerable and the queue drains on settle.
    const blockingPrompt = !queueEdit && hasBlockingPromptRequest(sessionId)

    if (queueEdit) {
      exitQueuedEdit('save')
    } else if (busy) {
      // Slash commands should execute immediately even while the agent is
      // busy — they're client-side operations (/yolo, /skin, /new, /help,
      // etc.) or self-contained gateway RPCs (/status, /compress).  onSubmit
      // routes them to executeSlashCommand, which has its own per-command
      // busy guard for commands that genuinely need an idle session (skill
      // /send directives).  Queuing them would make every slash command wait
      // for the current turn to finish, which is how the TUI never behaves.
      if (!attachments.length && SLASH_COMMAND_RE.test(text.trim())) {
        triggerHaptic('submit')
        dispatchSubmit(text, undefined, undefined, true)
        clearDraft(true)
      } else if (!compacting && !blockingPrompt && !attachments.length && text.trim()) {
        // Cursor-style stop-and-correct: interrupt the live turn and redirect
        // it with this text. redirect() preserves the shown reasoning/work; if
        // the turn already ended, steerDraft re-queues so nothing is lost.
        steerDraft()
      } else if (payloadPresent) {
        // Attachments can't ride a redirect (no tool-result image carriage) —
        // queue the whole payload for the next turn. Same for a turn parked on
        // an approval/sudo/secret prompt: a steer can't reach the model while
        // the tool batch is blocked, so the message runs as the next turn.
        queueCurrentDraft()
      } else {
        // Stop button (the only way to reach here while busy with an empty
        // composer — empty Enter is short-circuited in the keydown handler).
        triggerHaptic('cancel')
        void Promise.resolve(onCancel())
      }
    } else if (!payloadPresent && queuedPrompts.length > 0) {
      void drainNextQueued()
    } else if (payloadPresent) {
      const submittedAttachments = cloneAttachments(attachments)
      triggerHaptic('submit')
      resetBrowseState(sessionId)
      dispatchSubmit(text, submittedAttachments, undefined, true)
      clearDraft(true)
      scope.attachments.clear()
    }

    focusInput()
  }

  // Redirect the live turn with a correction. The gateway either restarts the
  // active model request with its displayed context or waits for the current
  // tool boundary. If the turn already ended, queue the words instead.
  const steerDraft = () => {
    const text = draftRef.current.trim()

    // Guard on live editor state, not the render-lagged `canSteer`: a redirect
    // fired on a fast Enter must not be dropped because state hasn't synced.
    if (!onSteer || !text || attachments.length > 0 || SLASH_COMMAND_RE.test(text)) {
      return
    }

    const submittedScope = activeQueueSessionKeyRef.current
    const submittedAttachments: ComposerAttachment[] = []
    const receiptIntentGeneration = draftIntentGenerationRef.current
    const receiptRevision = stashAt(submittedScope, text, submittedAttachments)

    const restore = () => {
      const liveAttachments = scope.attachments.$attachments.get()
      const composerEmpty = !draftRef.current && liveAttachments.length === 0

      const visibleReceiptMatches =
        draftIntentGenerationRef.current === receiptIntentGeneration &&
        draftRef.current === text &&
        sameAttachmentReceipt(liveAttachments, submittedAttachments)

      if (
        isSessionDraftRevisionCurrent(submittedScope, receiptRevision) &&
        draftIntentGenerationRef.current === receiptIntentGeneration &&
        activeQueueSessionKeyRef.current === submittedScope &&
        (composerEmpty || visibleReceiptMatches)
      ) {
        loadIntoComposer(text, submittedAttachments)
      }
    }

    const settleSteerReceipt = () => {
      const activeScopeMatches = activeQueueSessionKeyRef.current === submittedScope

      if (!activeScopeMatches) {
        clearSessionDraftIfRevision(submittedScope, receiptRevision)

        return
      }

      const liveAttachments = scope.attachments.$attachments.get()
      const composerEmpty = !draftRef.current && liveAttachments.length === 0

      const visibleReceiptMatches =
        draftIntentGenerationRef.current === receiptIntentGeneration &&
        draftRef.current === text &&
        sameAttachmentReceipt(liveAttachments, submittedAttachments)

      if (composerEmpty) {
        releasePersistedDraftReceipt()
        clearSessionDraftIfRevision(submittedScope, receiptRevision)
      } else if (visibleReceiptMatches) {
        clearDraft()
        clearSessionDraftIfRevision(submittedScope, receiptRevision)
      }
    }

    // Same durability contract as an idle submit: the editor may clear, but
    // the per-session stash remains until steer acceptance or queue fallback.
    triggerHaptic('submit')
    clearDraft(true)

    void Promise.resolve()
      .then(() => onSteer(text))
      .then(accepted => {
        if (accepted) {
          settleSteerReceipt()
        } else if (submittedScope && enqueueQueuedPrompt(submittedScope, { text, attachments: submittedAttachments })) {
          settleSteerReceipt()
        } else {
          restore()
        }
      })
      .catch(restore)
  }

  const queueDraft = () => {
    if (disabled || !busy) {
      return
    }

    queueCurrentDraft()
    focusInput()
  }

  return { dispatchSubmit, queueDraft, steerDraft, submitDraft }
}
