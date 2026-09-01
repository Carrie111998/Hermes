/**
 * Per-bot last-viewed watermarks so headless / relay-driven Bot Chat
 * activity still badges after the Bots pane remounts.
 *
 * The in-memory poll watermark in roster-actions only sees activity that
 * arrives AFTER this window's first roster snapshot. A Telegram →
 * message_agent turn that finishes while the pane is unmounted (or after
 * the 90s Active-now window) used to seed as "already seen" on the next
 * mount. Last-viewed is the durable unread-since-last-viewed signal the
 * issue asked for — keep the 90s pulse for "live right now"; persist
 * last-viewed so late looks still show a row update.
 */

import { atom } from '@hermes/plugin-sdk'

import { getPluginCtx } from './shared'

export const LAST_VIEWED_STORAGE_KEY = 'roster-last-viewed-v1'

/** True once plugin storage has answered (or failed) for last-viewed. */
export const $lastViewedHydrated = atom(false)

/** Source-qualified bot → last_active the user last looked at (seconds). */
export const lastViewedByBot = new Map<string, number>()

/** Coerce a storage payload into a finite last_active map. */
export function parseLastViewedMap(raw: unknown): Record<string, number> {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    return {}
  }

  const out: Record<string, number> = {}

  for (const [key, value] of Object.entries(raw)) {
    const name = String(key || '').trim()
    const ts =
      typeof value === 'number' ? value : typeof value === 'string' && value.trim() ? Number(value) : Number.NaN

    if (!name || !Number.isFinite(ts) || ts < 0) {
      continue
    }

    out[name] = ts
  }

  return out
}

/** True when this bot's latest activity is newer than the last time the
 *  user opened it. A missing last-viewed means first encounter — seed,
 *  do not mark ancient history unread. */
export function botHasUnseenActivity(lastActive: number, lastViewed: number | undefined): boolean {
  if (!lastActive || lastViewed == null) {
    return false
  }

  return lastActive > lastViewed
}

function snapshotLastViewed(): Record<string, number> {
  return Object.fromEntries(lastViewedByBot)
}

export function persistLastViewed() {
  try {
    Promise.resolve(getPluginCtx()?.storage?.set?.(LAST_VIEWED_STORAGE_KEY, snapshotLastViewed())).catch(
      () => undefined
    )
  } catch {
    /* storage unavailable — last-viewed lasts for this window */
  }
}

/** Replace the in-memory map from a storage payload and mark hydrated. */
export function hydrateLastViewed(raw: unknown) {
  lastViewedByBot.clear()

  for (const [key, ts] of Object.entries(parseLastViewedMap(raw))) {
    lastViewedByBot.set(key, ts)
  }

  $lastViewedHydrated.set(true)
}

/** Record that the user is looking at this bot as of `lastActive`. */
export function rememberBotViewed(key: string, lastActive: number) {
  const name = String(key || '').trim()

  if (!name) {
    return
  }

  const ts = Number(lastActive) || 0
  const prev = lastViewedByBot.get(name) || 0

  if (lastViewedByBot.has(name) && ts <= prev) {
    return
  }

  lastViewedByBot.set(name, Math.max(prev, ts))
  persistLastViewed()
}

/** Drop a deleted bot so a recycled name does not inherit its unread. */
export function forgetBotLastViewed(key: string) {
  const name = String(key || '').trim()

  if (!name || !lastViewedByBot.has(name)) {
    return
  }

  lastViewedByBot.delete(name)
  persistLastViewed()
}
