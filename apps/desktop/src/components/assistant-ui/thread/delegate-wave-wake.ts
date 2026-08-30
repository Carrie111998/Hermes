export type DelegateWaveWakeKind = 'completed' | 'failed' | 'question' | 'ready'

export type DelegateWaveWake = {
  kind: DelegateWaveWakeKind
  task: string
  detail?: string
}

const MARKER_RE = /\[delegate-wave-wake:wake_[^\]]+\]/i
const TASK_RE = /delegate-wave session working on "([^"]+)"/i

function taskFrom(text: string): string {
  return TASK_RE.exec(text)?.[1]?.trim() || 'the delegated task'
}

export function parseDelegateWaveWake(text: string): DelegateWaveWake | null {
  if (!MARKER_RE.test(text)) {
    return null
  }

  const task = taskFrom(text)

  const question = text.match(
    /needs an answer before it can continue\.\s*\n+([\s\S]*?)(?:\n+Why it matters:|\n+Answer it with session_answer|\n+\[delegate-wave-wake:|$)/i
  )

  if (question) {
    return {
      kind: 'question',
      task,
      detail: question[1]?.trim() || undefined
    }
  }

  if (/finished and its result is on the branch/i.test(text)) {
    return { kind: 'completed', task }
  }

  if (/has a finished, validated candidate/i.test(text)) {
    return { kind: 'ready', task }
  }

  if (/delegate-wave session[\s\S]* failed\./i.test(text)) {
    const outcome = text.match(/failed\.\s*(?:\n+([\s\S]*?))?(?:\n+Use session_poll|\n+\[delegate-wave-wake:|$)/i)

    return {
      kind: 'failed',
      task,
      detail: outcome?.[1]?.trim() || undefined
    }
  }

  return null
}

/** Only a backend-typed timeline row has authority to become a wake card.
 * The prose parser derives the subtype/detail; it never authenticates origin. */
export function parseDelegateWaveWakeEvent(text: string, displayKind: unknown): DelegateWaveWake | null {
  return displayKind === 'delegate_wave_wake' ? parseDelegateWaveWake(text) : null
}
