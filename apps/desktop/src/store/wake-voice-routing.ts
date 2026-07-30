export type WakeVoiceRouteOutcome = 'not-route' | 'rejected' | 'routed'
export type WakeVoiceRouteHandler = (transcript: string, profile: null | string) => Promise<WakeVoiceRouteOutcome>

let handler: WakeVoiceRouteHandler | null = null

/** Register the primary window's session-aware routing bridge. */
export function setWakeVoiceRouteHandler(next: WakeVoiceRouteHandler | null): void {
  handler = next
}

/**
 * Offer a wake-triggered first transcript to the routing bridge. `not-route`
 * means the ordinary composer must submit the original transcript unchanged.
 */
export async function routeWakeVoiceTranscript(
  transcript: string,
  profile: null | string
): Promise<WakeVoiceRouteOutcome> {
  return handler ? handler(transcript, profile) : 'not-route'
}
