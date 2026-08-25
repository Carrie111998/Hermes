// One lazily-created AudioContext for the app's synthesized UI cues (wake
// chime, turn-completion sound, thinking blips). Browsers cap how many
// contexts a page may open, and these cues are short oscillator bursts that
// need no isolation from each other, so they share one instead of each holding
// its own. Nothing closes it — it lives for the window.

let ctx: AudioContext | null = null

/** The shared AudioContext, or null where WebAudio is unavailable. */
export function getAudioContext(): AudioContext | null {
  if (typeof window === 'undefined') {
    return null
  }

  try {
    const Ctor =
      window.AudioContext || (window as unknown as { webkitAudioContext?: typeof AudioContext }).webkitAudioContext

    if (!Ctor) {
      return null
    }

    if (!ctx || ctx.state === 'closed' || !(ctx instanceof Ctor)) {
      ctx = new Ctor()
    }

    // Autoplay policies can leave the context suspended until a gesture; a
    // resume() here recovers it once the user has interacted with the window.
    if (ctx.state === 'suspended') {
      void ctx.resume().catch(() => undefined)
    }

    return ctx
  } catch {
    return null
  }
}

/** Wait until the shared context can actually schedule audible work. The
 * synchronous getter initiates resume for legacy callers; attention/completion
 * cues await the same transition so tones are not scheduled into a context
 * that never left `suspended`. */
export async function getRunningAudioContext(): Promise<AudioContext | null> {
  const ac = getAudioContext()

  if (!ac) {
    return null
  }

  if (ac.state === 'suspended') {
    try {
      await ac.resume()
    } catch {
      return null
    }
  }

  return ac.state === 'running' ? ac : null
}
