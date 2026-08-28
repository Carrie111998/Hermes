export type DelegateWaveWakeKind = 'completed' | 'failed' | 'question' | 'ready'

export type DelegateWaveWake = {
  kind: DelegateWaveWakeKind
  label: string
  summary: string
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
      label: 'Needs input',
      summary: question[1]?.trim() || `A decision is needed for ${task}.`
    }
  }

  if (/finished and its result is on the branch/i.test(text)) {
    return { kind: 'completed', label: 'Completed', summary: `Finished ${task} and published the result.` }
  }

  if (/has a finished, validated candidate/i.test(text)) {
    return { kind: 'ready', label: 'Ready for review', summary: `Validated ${task}; the candidate is waiting for review.` }
  }

  if (/delegate-wave session[\s\S]* failed\./i.test(text)) {
    const outcome = text.match(/failed\.\s*(?:\n+([\s\S]*?))?(?:\n+Use session_poll|\n+\[delegate-wave-wake:|$)/i)

    return {
      kind: 'failed',
      label: 'Stopped',
      summary: outcome?.[1]?.trim() || `Could not finish ${task}.`
    }
  }

  return null
}
