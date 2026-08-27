import { useEffect, useLayoutEffect, useRef, useState } from 'react'

import { useI18n } from '@/i18n'
import { notify, notifyError } from '@/store/notifications'

import type { VoiceActivityState, VoiceStatus } from '../types'

import { useMicRecorder } from './use-mic-recorder'

interface VoiceRecorderOptions {
  maxRecordingSeconds: number
  onTranscribeAudio?: (audio: Blob) => Promise<string>
  focusInput: () => void
  onTranscript: (text: string) => void
}

export function useVoiceRecorder({
  maxRecordingSeconds,
  onTranscribeAudio,
  focusInput,
  onTranscript
}: VoiceRecorderOptions) {
  const { t } = useI18n()
  const voiceCopy = t.notifications.voice
  const { handle, level, recording } = useMicRecorder(voiceCopy)
  const handleRef = useRef(handle)
  const [voiceStatus, setVoiceStatus] = useState<VoiceStatus>('idle')
  const [elapsedSeconds, setElapsedSeconds] = useState(0)
  const startedAtRef = useRef(0)
  const intervalRef = useRef<number | null>(null)
  const timeoutRef = useRef<number | null>(null)
  const recordingEpochRef = useRef(0)
  const startingRef = useRef(false)

  useLayoutEffect(() => {
    handleRef.current = handle
  }, [handle])

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

  useEffect(
    () => () => {
      recordingEpochRef.current += 1
      startingRef.current = false
      clearTimers()
      handleRef.current.cancel()
    },
    [] // recorder handle methods close over stable refs; cleanup must run once
  )

  const stop = async () => {
    const recordingEpoch = recordingEpochRef.current

    clearTimers()
    const result = await handle.stop()

    if (recordingEpochRef.current !== recordingEpoch) {
      return
    }

    if (!result) {
      setVoiceStatus('idle')

      return
    }

    if (!onTranscribeAudio) {
      setVoiceStatus('idle')

      return
    }

    setVoiceStatus('transcribing')

    try {
      const transcript = (await onTranscribeAudio(result.audio)).trim()

      if (recordingEpochRef.current !== recordingEpoch) {
        return
      }

      if (!transcript) {
        notify({ kind: 'warning', title: voiceCopy.noSpeechDetected, message: voiceCopy.tryRecordingAgain })
      } else {
        onTranscript(transcript)
      }
    } catch (error) {
      if (recordingEpochRef.current === recordingEpoch) {
        notifyError(error, voiceCopy.transcriptionFailed)
      }
    } finally {
      if (recordingEpochRef.current === recordingEpoch) {
        setVoiceStatus('idle')
        focusInput()
      }
    }
  }

  const start = async () => {
    if (startingRef.current || recording || voiceStatus !== 'idle') {
      return
    }

    if (!onTranscribeAudio) {
      notify({ kind: 'warning', title: voiceCopy.unavailable, message: voiceCopy.transcriptionUnavailable })

      return
    }

    const recordingEpoch = recordingEpochRef.current + 1

    recordingEpochRef.current = recordingEpoch
    startingRef.current = true

    try {
      await handle.start({ onError: error => notifyError(error, voiceCopy.recordingFailed) })

      if (recordingEpochRef.current !== recordingEpoch) {
        handle.cancel()

        return
      }

      startedAtRef.current = Date.now()
      setElapsedSeconds(0)
      setVoiceStatus('recording')
      intervalRef.current = window.setInterval(() => setElapsedSeconds((Date.now() - startedAtRef.current) / 1000), 250)
      const cap = Math.max(1, Math.min(Math.trunc(maxRecordingSeconds), 600))
      timeoutRef.current = window.setTimeout(() => void stop(), cap * 1000)
    } catch (error) {
      if (recordingEpochRef.current === recordingEpoch) {
        setVoiceStatus('idle')
        notifyError(error, voiceCopy.recordingFailed)
      }
    } finally {
      if (recordingEpochRef.current === recordingEpoch) {
        startingRef.current = false
      }
    }
  }

  const cancel = () => {
    recordingEpochRef.current += 1
    startingRef.current = false
    clearTimers()
    handle.cancel()
    setElapsedSeconds(0)
    setVoiceStatus('idle')
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

  return { cancel, dictate, voiceActivityState, voiceStatus }
}
