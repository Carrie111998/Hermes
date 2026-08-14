import { useEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'

import type { VoiceActivityState, VoiceStatus } from '../types'

import { useMicRecorder } from './use-mic-recorder'

interface VoiceRecorderOptions {
  maxRecordingSeconds: number
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  focusInput: () => void
  onTranscript: (text: string) => void
  /** Called after a recording is stopped or cancelled, including max duration. */
  onFinished?: () => void
}

export function useVoiceRecorder({
  maxRecordingSeconds,
  onTranscribeAudio,
  focusInput,
  onTranscript,
  onFinished
}: VoiceRecorderOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const { handle, level, recording } = useMicRecorder(voiceCopy)
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('idle')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const startedAtRef = useRef(0)
  const intervalRef = useRef<number | null>(null)
  const timeoutRef = useRef<number | null>(null)
  // Cancelling while transcription is in flight cannot abort every configured
  // STT transport, but it must make its eventual response harmless.
  const recordingGenerationRef = useRef(0)

  const clearTimers = () => {
    if (intervalRef.current) {
      window.clearInterval(intervalRef.current)
      intervalRef.current = null
    }

    if (timeoutRef.current) {
      window.clearTimeout(timeoutRef.current)
      timeoutRef.current = null
    }
  }

  useEffect(() => () => clearTimers(), [])

  const stop = async () => {
    const generation = recordingGenerationRef.current
    clearTimers()
    const result = await handle.stop()

    if (generation !== recordingGenerationRef.current) {
      return
    }

    if (!result) {
      setVoiceStatus('idle')

      onFinished?.()

      return
    }

    if (!onTranscribeAudio) {
      setVoiceStatus('idle')

      onFinished?.()

      return
    }

    setVoiceStatus('transcribing')

    try {
      const transcript = (await onTranscribeAudio(result.audio)).trim()

      if (generation !== recordingGenerationRef.current) {
        return
      }

      if (!transcript) {
        notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
      } else {
        onTranscript(transcript)
      }
    } catch (error) {
      if (generation === recordingGenerationRef.current) {
        notifyError(error, voiceCopy.transcriptionFailed)
      }
    } finally {
      if (generation === recordingGenerationRef.current) {
        setVoiceStatus('idle')
        focusInput()
        onFinished?.()
      }
    }
  }

  const start = async () => {
    if (!onTranscribeAudio) {
      notify({ kind: 'warning', title: voiceCopy.unavailable, message: voiceCopy.transcriptionUnavailable })

      return false
    }

    try {
      recordingGenerationRef.current += 1
      await handle.start({ onError: error => notifyError(error, voiceCopy.recordingFailed) })
      startedAtRef.current = Date.now()
      setElapsedSeconds(0)
      setVoiceStatus('recording')
      intervalRef.current = window.setInterval(() => setElapsedSeconds((Date.now() - startedAtRef.current) / 1000), 250)
      const cap = Math.max(1, Math.min(Math.trunc(maxRecordingSeconds), 600))
      timeoutRef.current = window.setTimeout(() => void stop(), cap * 1000)

      return true
    } catch (error) {
      setVoiceStatus('idle')
      notifyError(error, voiceCopy.recordingFailed)

      return false
    }
  }

  const cancel = () => {
    recordingGenerationRef.current += 1
    clearTimers()
    handle.cancel()
    setVoiceStatus('idle')
    focusInput()
    onFinished?.()
  }

  const dictate = () => {
    if (recording) {
      void stop()
    } else if (voiceStatus === 'idle') {
      void start()
    }
  }

  const voiceActivityState: VoiceActivityState = {
    elapsedSeconds,
    level,
    status: voiceStatus
  }

  return { cancel, dictate, start, stop, voiceActivityState, voiceStatus }
}
