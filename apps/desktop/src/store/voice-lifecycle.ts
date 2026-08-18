import { atom } from 'nanostores'

export type VoiceLifecycleEventType = 'tts_ended' | 'tts_started' | 'user_speech_ended' | 'user_speech_started'

export interface VoiceLifecycleEvent {
  type: VoiceLifecycleEventType
}

export interface VoiceLifecycleState {
  ttsSpeaking: boolean
  userSpeaking: boolean
}

export type VoiceLifecycleListener = (event: VoiceLifecycleEvent) => void

export const $voiceLifecycle = atom<VoiceLifecycleState>({
  ttsSpeaking: false,
  userSpeaking: false
})

const listeners = new Map<VoiceLifecycleEventType | '*', Set<VoiceLifecycleListener>>()
const userSpeechSources = new Set<string>()

export function onVoiceLifecycleEvent(
  type: VoiceLifecycleEventType | '*',
  listener: VoiceLifecycleListener
): () => void {
  const set = listeners.get(type) ?? new Set<VoiceLifecycleListener>()
  set.add(listener)
  listeners.set(type, set)

  return () => {
    set.delete(listener)

    if (set.size === 0) {
      listeners.delete(type)
    }
  }
}

function emitVoiceLifecycleEvent(type: VoiceLifecycleEventType): void {
  const event = { type }

  for (const key of [type, '*'] as const) {
    for (const listener of listeners.get(key) ?? []) {
      try {
        listener(event)
      } catch (error) {
        console.error('[plugins] voice lifecycle listener failed', error)
      }
    }
  }
}

function setLifecycleFlag(flag: keyof VoiceLifecycleState, active: boolean): void {
  const current = $voiceLifecycle.get()

  if (current[flag] === active) {
    return
  }

  $voiceLifecycle.set({ ...current, [flag]: active })

  if (flag === 'userSpeaking') {
    emitVoiceLifecycleEvent(active ? 'user_speech_started' : 'user_speech_ended')
  } else {
    emitVoiceLifecycleEvent(active ? 'tts_started' : 'tts_ended')
  }
}

export function setTtsSpeaking(active: boolean): void {
  setLifecycleFlag('ttsSpeaking', active)
}

export function setUserSpeaking(active: boolean, source = 'default'): void {
  if (active) {
    userSpeechSources.add(source)
  } else {
    userSpeechSources.delete(source)
  }

  setLifecycleFlag('userSpeaking', userSpeechSources.size > 0)
}
