/**
 * Layer 8 — pet polling budget.
 *
 * Each pinned non-active profile holds a leased socket; polling every one of
 * them at the foreground rate would keep N backends permanently busy and burn
 * the spritesheet-bearing `pet.info` payload every few seconds. The budget:
 *
 *  - Meta-first: background profiles poll the cheap `pet.info.meta` and only
 *    fetch the full `pet.info` (base64 spritesheet) when the revision changes.
 *  - Cadence tiers: fast bootstrap until first payload, a steady foreground
 *    refresh, a slower background refresh, and a very slow blurred refresh
 *    (activity/unread/reply are event-driven over the leased sockets, so a slow
 *    metadata poll is safe when the window is hidden).
 *  - A single background lane: at most one background meta request at a time,
 *    staggered across the window. Foreground polling is a separate lane and is
 *    never queued behind background work.
 *  - Polls are skipped while a profile is offline or reauth-required.
 */

/** Bootstrap cadence — until the first full payload lands. */
export const PET_POLL_MS = 3_000
/** Loaded foreground (active) pet refresh. */
export const PET_ACTIVE_REFRESH_MS = 15_000
/** Loaded non-active (background) profile refresh. */
export const PET_BG_POLL_MS = 30_000
/** All loaded profiles when the window is blurred. */
export const PET_BLURRED_POLL_MS = 120_000
/** At most one background meta request in flight at a time. */
export const PET_BG_POLL_CONCURRENCY = 1

export interface PetPollContext {
  /** This profile is the active/foreground pet. */
  active: boolean
  /** The window is blurred (hidden/backgrounded). */
  blurred: boolean
  /** A full payload has loaded at least once. */
  loaded: boolean
  /** The profile's connection is offline or reauth-required. */
  offline: boolean
}

/**
 * The poll interval (ms) for one profile, or `null` to skip polling entirely
 * (offline / reauth-required). Blurred wins over the active/background tier —
 * every loaded profile slows to the blurred cadence when the window is hidden.
 */
export function petPollInterval(ctx: PetPollContext): null | number {
  if (ctx.offline) {
    return null
  }

  if (ctx.blurred) {
    return PET_BLURRED_POLL_MS
  }

  if (!ctx.loaded) {
    return PET_POLL_MS
  }

  return ctx.active ? PET_ACTIVE_REFRESH_MS : PET_BG_POLL_MS
}

/**
 * A bounded concurrency lane for background polls. At most `concurrency`
 * background tasks run at once; the rest queue. `runForeground` bypasses the
 * queue entirely — a foreground poll is never held behind background work.
 */
export interface PollLane {
  runForeground: <T>(task: () => Promise<T>) => Promise<T>
  runBackground: <T>(task: () => Promise<T>) => Promise<T>
  /** Tasks waiting for a background slot. */
  pending: () => number
}

export function createPollLane(concurrency: number = PET_BG_POLL_CONCURRENCY): PollLane {
  let running = 0
  const queue: Array<() => void> = []

  const drain = (): void => {
    while (running < concurrency && queue.length > 0) {
      const start = queue.shift()

      if (start) {
        running += 1
        start()
      }
    }
  }

  return {
    runForeground: task => task(),
    runBackground: <T>(task: () => Promise<T>): Promise<T> =>
      new Promise<T>((resolve, reject) => {
        queue.push(() => {
          task()
            .then(resolve, reject)
            .finally(() => {
              running -= 1
              drain()
            })
        })
        drain()
      }),
    pending: () => queue.length
  }
}

/**
 * Stagger offset (ms) for the Nth background profile so concurrent profiles
 * don't all poll on the same tick — spreads them across the background window.
 */
export function staggerOffset(index: number, windowMs: number = PET_BG_POLL_MS): number {
  const count = Math.max(1, index + 1)

  return Math.round((windowMs / Math.max(1, count)) * index) % windowMs
}
