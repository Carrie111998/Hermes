// Dedicated approval alarm: two identical rising pulses, separated enough to
// read as "attention required" rather than as a turn-completion chime. Synthesized
// with WebAudio so there is no asset to ship. One window owns each cue.

import { getRunningAudioContext } from '@/lib/audio-context'
import { ownsAmbientCue } from '@/store/ambient'
import { $hapticsMuted } from '@/store/haptics'

function tone(ac: AudioContext, master: GainNode, start: number, frequency: number, type: OscillatorType) {
  const oscillator = ac.createOscillator()
  const envelope = ac.createGain()
  const end = start + 0.2

  oscillator.type = type
  oscillator.frequency.setValueAtTime(frequency, start)

  envelope.gain.setValueAtTime(0.0001, start)
  envelope.gain.exponentialRampToValueAtTime(0.16, start + 0.008)
  envelope.gain.exponentialRampToValueAtTime(0.0001, end)

  oscillator.connect(envelope)
  envelope.connect(master)
  oscillator.start(start)
  oscillator.stop(end + 0.02)
}

function playAlarm(ac: AudioContext): void {
  try {
    const master = ac.createGain()
    master.gain.setValueAtTime(0.9, ac.currentTime)
    master.connect(ac.destination)

    const t0 = ac.currentTime + 0.01

    // E5→B5, pause, E5→B5. The repeated cadence is deliberately unlike every
    // completion preset: this asks for action rather than announcing "done".
    tone(ac, master, t0, 659.25, 'triangle')
    tone(ac, master, t0 + 0.12, 987.77, 'square')
    tone(ac, master, t0 + 0.48, 659.25, 'triangle')
    tone(ac, master, t0 + 0.6, 987.77, 'square')
  } catch {
    // A dead audio context must never disrupt approval rendering.
  }
}

export async function playApprovalSound(dedupeKey?: string): Promise<void> {
  if ($hapticsMuted.get()) {
    return
  }

  const ac = await getRunningAudioContext()

  if (!ac || $hapticsMuted.get()) {
    return
  }

  if (dedupeKey && !(await ownsAmbientCue(`approval-sound:${dedupeKey}`))) {
    return
  }

  if ($hapticsMuted.get()) {
    return
  }

  playAlarm(ac)
}
