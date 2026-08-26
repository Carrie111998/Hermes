import { useStore } from '@nanostores/react'
import { useCallback, useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { chatMessageText, collectUnspokenTurnSpeech } from '@/lib/chat-messages'
import { triggerHaptic } from '@/lib/haptics'
import { markAssistantIdSpoken, resolveSpokenReply } from '@/lib/spoken-reply'
import { clearWakeIndicator, syncWakeIndicatorWithVoice } from '@/lib/wake-indicator'
import { $voiceConversationStartRequest, takeVoiceConversationStart } from '@/store/composer'
import { resetBrowseState } from '@/store/composer-input-history'
import { $gateway } from '@/store/gateway'
import { notify, notifyError } from '@/store/notifications'
import { $autoSpeakReplies, $voiceStopPhrase, setAutoSpeakReplies } from '@/store/voice-prefs'
import { resumeWakeAfterVoice } from '@/store/wake-word'

import type { ComposerTarget } from '../focus'
import { onComposerVoiceToggleRequest } from '../focus'
import { useComposerScope } from '../scope'
import type { ChatBarProps } from '../types'

import { useAutoSpeakReplies } from './use-auto-speak-replies'
import { useVoiceConversation } from './use-voice-conversation'
import { useVoiceRecorder } from './use-voice-recorder'

interface UseComposerVoiceArgs {
  busy: boolean
  clearDraft: () => void
  disabled: boolean
  focusInput: () => void
  insertText: (text: string) => void
  maxRecordingSeconds: number
  /** Interrupt the in-flight agent turn (Stop-button seam) — fired when the
   *  user speaks over the model while it is still generating. */
  onInterrupt?: () => Promise<void> | void
  onSubmit: ChatBarProps['onSubmit']
  onTranscribeAudio: ChatBarProps['onTranscribeAudio']
  sessionId: string | null | undefined
  /** Durable identity for the route-scoped draft this callback belongs to. */
  submissionKey: string | null
  /** This composer's focus-bus key — voice toggles targeting another
   *  composer (or the active one, when not us) are ignored. */
  target: ComposerTarget
}

export function canSubmitVoiceTurn({ busy, disabled }: Pick<UseComposerVoiceArgs, 'busy' | 'disabled'>): boolean {
  return !busy && !disabled
}

/**
 * The composer's voice engine: push-to-talk dictation (transcript → draft), the
 * full voice-conversation loop, and auto-speak of replies. Self-contained — it
 * consumes the draft/submit primitives passed in but nothing depends back on it,
 * so it lifts cleanly out of ChatBar.
 */
export function useComposerVoice({
  busy,
  clearDraft,
  disabled,
  focusInput,
  insertText,
  maxRecordingSeconds,
  onInterrupt,
  onSubmit,
  onTranscribeAudio,
  sessionId,
  submissionKey,
  target
}: UseComposerVoiceArgs) {
  const { t } = useI18n()
  // A tile's composer speaks ITS transcript, not the primary chat's.
  const { $messages } = useComposerScope()
  const [voiceConversationActive, setVoiceConversationActive] = useState(false)
  const ownsWakeIndicatorRef = useRef(false)
  const voiceStartRequest = useStore($voiceConversationStartRequest)

  // Voice transcription completes asynchronously. The composer intentionally
  // stays mounted across route switches, so a callback created for A can finish
  // after the same editor is showing B. Invalidate every callback generation on
  // a durable draft-scope change and read all action dependencies live.
  const voiceScopeEpochRef = useRef({ key: submissionKey, value: 0 })

  if (voiceScopeEpochRef.current.key !== submissionKey) {
    voiceScopeEpochRef.current = { key: submissionKey, value: voiceScopeEpochRef.current.value + 1 }
  }

  const renderVoiceScopeEpoch = voiceScopeEpochRef.current.value

  const voiceActionStateRef = useRef({
    busy,
    clearDraft,
    disabled,
    focusInput,
    insertText,
    onInterrupt,
    onSubmit,
    sessionId
  })

  voiceActionStateRef.current = { busy, clearDraft, disabled, focusInput, insertText, onInterrupt, onSubmit, sessionId }

  const voiceScopeIsCurrent = () =>
    voiceScopeEpochRef.current.value === renderVoiceScopeEpoch && !voiceActionStateRef.current.disabled

  const insertVoiceTranscript = (text: string) => {
    if (voiceScopeIsCurrent()) {
      voiceActionStateRef.current.insertText(text)
    }
  }

  const focusAfterVoice = () => {
    if (voiceScopeIsCurrent()) {
      voiceActionStateRef.current.focusInput()
    }
  }

  const { dictate, voiceActivityState, voiceStatus } = useVoiceRecorder({
    focusInput: focusAfterVoice,
    maxRecordingSeconds,
    onTranscript: insertVoiceTranscript,
    onTranscribeAudio
  })

  /** Auto-speak selector: the latest unspoken reply only — a backlog collapses to the newest. */
  const pendingResponse = () => {
    const messages = $messages.get()
    const last = messages.findLast(m => m.role === 'assistant' && !m.hidden)
    const spoken = resolveSpokenReply(sessionId, messages)

    if (!last || last.id === spoken?.id) {
      return null
    }

    const text = chatMessageText(last).trim()

    if (!text) {
      return null
    }

    return {
      id: last.id,
      pending: Boolean(last.pending),
      text
    }
  }

  /**
   * Voice-conversation selector: every unspoken assistant bubble of the turn,
   * in order — narration interims AND the final answer, not just whichever
   * bubble happens to be last. See `collectUnspokenTurnSpeech`.
   */
  const pendingTurnResponse = () => {
    const messages = $messages.get()

    return collectUnspokenTurnSpeech(messages, resolveSpokenReply(sessionId, messages)?.id ?? null)
  }

  const consumePendingResponse = () => {
    const messages = $messages.get()
    const last = messages.findLast(m => m.role === 'assistant' && !m.hidden)

    if (last) {
      markAssistantIdSpoken(sessionId, messages, last.id)
    }
  }

  const submitVoiceTurn = async (text: string) => {
    const action = voiceActionStateRef.current

    if (!voiceScopeIsCurrent() || !canSubmitVoiceTurn(action)) {
      return
    }

    triggerHaptic('submit')
    resetBrowseState(action.sessionId)
    action.clearDraft()
    await action.onSubmit(text)
  }

  const interruptVoiceTurn = async () => {
    if (!voiceScopeIsCurrent()) {
      return
    }

    await voiceActionStateRef.current.onInterrupt?.()
  }

  const wakePausedRef = useRef(false)
  // Resolves once the in-flight wake.pause round-trip completes (mic released by
  // the wake listener). The conversation awaits this before opening its own mic
  // so the two never contend for the device — on Windows especially, opening the
  // capture device while the wake listener still holds it makes getUserMedia
  // fail and the conversation never starts listening.
  const wakePauseBarrierRef = useRef<Promise<void> | null>(null)

  const conversation = useVoiceConversation({
    busy,
    consumePendingResponse,
    enabled: voiceConversationActive && !disabled,
    onFatalError: () => {
      if (voiceScopeIsCurrent()) {
        setVoiceConversationActive(false)
      }
    },
    // Speaking over the model mid-generation interrupts the in-flight turn —
    // the same seam as the Stop button — so the interjection becomes the next
    // turn instead of waiting behind a reply the user already rejected.
    onInterrupt: interruptVoiceTurn,
    // A spoken stop command ("stop", "never mind", "goodbye", …) ends the
    // hands-free conversation. Flipping the flag is the authoritative off
    // switch — the enabled=false prop + effect below drive conversation.end()
    // teardown (mic close, wake re-arm).
    onStopWord: () => {
      if (voiceScopeIsCurrent()) {
        setVoiceConversationActive(false)
      }
    },
    onSubmit: submitVoiceTurn,
    onTranscribeAudio,
    pendingResponse: pendingTurnResponse,
    // Before the conversation opens the mic, wait for any in-flight wake.pause
    // to finish releasing the capture device (see wakePauseBarrierRef).
    beforeMicOpen: () => wakePauseBarrierRef.current ?? undefined
  })

  const mountedVoiceScopeKeyRef = useRef(submissionKey)

  // eslint-disable-next-line no-restricted-syntax -- local lifecycle token, not an atom mirror
  useEffect(() => {
    if (mountedVoiceScopeKeyRef.current === submissionKey) {
      return
    }

    mountedVoiceScopeKeyRef.current = submissionKey
    setVoiceConversationActive(false)
    void conversation.end()
  }, [conversation, submissionKey])

  // eslint-disable-next-line no-restricted-syntax -- ownership token used only by unmount cleanup
  useEffect(() => {
    if (target !== 'main') {
      return
    }

    if (syncWakeIndicatorWithVoice(voiceConversationActive, conversation.status)) {
      ownsWakeIndicatorRef.current = voiceConversationActive
    }
  }, [conversation.status, target, voiceConversationActive])

  useEffect(
    () => () => {
      if (ownsWakeIndicatorRef.current) {
        clearWakeIndicator()
      }
    },
    []
  )

  // The `composer.voice` hotkey (Ctrl+B) toggles the conversation. Starting
  // with STT unconfigured lets the conversation surface its own "configure
  // speech-to-text" notice rather than silently no-opping.
  const toggleVoiceConversation = useCallback(() => {
    if (disabled) {
      return
    }

    if (voiceConversationActive) {
      setVoiceConversationActive(false)
      void conversation.end()
    } else {
      setVoiceConversationActive(true)
    }
  }, [conversation, disabled, voiceConversationActive])

  useEffect(
    () => onComposerVoiceToggleRequest(toggled => toggled === target && toggleVoiceConversation()),
    [target, toggleVoiceConversation]
  )

  useEffect(() => {
    if (target === 'main' && !disabled && takeVoiceConversationStart(voiceStartRequest) && !voiceConversationActive) {
      setVoiceConversationActive(true)
    }
  }, [disabled, target, voiceConversationActive, voiceStartRequest])

  const resumeWakeIfPaused = useCallback(() => {
    if (!wakePausedRef.current) {
      return
    }

    wakePausedRef.current = false
    wakePauseBarrierRef.current = null
    // Reconcile, don't just resume: the wake word is a persistent setting, so
    // ending a voice chat must re-arm the listener whenever config says
    // enabled — including when the raw resume loses the mic-release race.
    void resumeWakeAfterVoice()
  }, [])

  // The ref is a request token (did WE issue wake.pause?), not an atom mirror —
  // it guards resumeWakeIfPaused from resuming a detector another surface owns.
  const pauseWakeForVoice = useCallback(() => {
    wakePausedRef.current = true

    const barrier = (async () => {
      try {
        await $gateway.get()?.request('wake.pause', {})
      } catch {
        // No wake listener / older backend — nothing held the mic.
      }
    })()

    wakePauseBarrierRef.current = barrier

    return barrier
  }, [])

  useEffect(() => {
    if (voiceConversationActive) {
      pauseWakeForVoice()
    } else {
      resumeWakeIfPaused()
    }
  }, [pauseWakeForVoice, resumeWakeIfPaused, voiceConversationActive])

  // 'Say "stop" to end the voice chat.' notice when the conversation starts.
  // Phrase comes from voice.stop_phrases (first entry) so a custom phrase
  // renders correctly; a null phrase (stop_phrases: []) shows no notice.
  useEffect(() => {
    if (!voiceConversationActive) {
      return
    }

    const phrase = $voiceStopPhrase.get()

    if (phrase) {
      notify({
        id: 'voice-stop-hint',
        kind: 'info',
        icon: 'mic',
        message: t.notifications.voice.sayStopToEnd(phrase)
      })
    }
  }, [t, voiceConversationActive])

  useEffect(() => resumeWakeIfPaused, [resumeWakeIfPaused])

  // Explicit start/end for the on-screen conversation controls (the hotkey uses
  // the gated toggle above).
  const startConversation = useCallback(() => setVoiceConversationActive(true), [])

  const endConversation = useCallback(() => {
    setVoiceConversationActive(false)
    void conversation.end()
  }, [conversation])

  const handleToggleAutoSpeak = useCallback(() => {
    void setAutoSpeakReplies(!$autoSpeakReplies.get()).catch(error =>
      notifyError(error, t.settings.config.autosaveFailed)
    )
  }, [t])

  useAutoSpeakReplies({
    conversationActive: voiceConversationActive,
    failureLabel: t.assistant.thread.readAloudFailed,
    markSpoken: consumePendingResponse,
    pendingReply: pendingResponse,
    sessionId
  })

  return {
    conversation,
    dictate,
    endConversation,
    handleToggleAutoSpeak,
    startConversation,
    voiceActivityState,
    voiceConversationActive,
    voiceStatus
  }
}
