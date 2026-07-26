import { listAllProfileSessions } from '@/hermes'
import { enqueueQueuedPrompt } from '@/store/composer-queue'
import { requestGatewayForProfile, withProfileGatewayLease } from '@/store/gateway'
import { notify } from '@/store/notifications'
import {
  hasBusyActivityForStoredSession,
  profileRuntimeBusy,
  setProfileAwaitingResponse,
  setProfileSubmitBusy,
  updateBackgroundSessionState
} from '@/store/pet-multi'
import { $activeGatewayProfile, normalizeProfileKey } from '@/store/profile'
import { $workingSessionIds } from '@/store/session-states'

import type { SubmitExecution, SubmitTextOptions } from './utils'

/**
 * Profile-safe submission (Layer 6c).
 *
 * A reply aimed at a NON-active profile must not touch the foreground pipeline's
 * inputs — the active session refs, the routed/selected ids, the foreground
 * `$busy`/`$messages`, or the active gateway. Instead it runs the SAME submit
 * pipeline through a `backgroundSubmitExecution(profile)`: a profile-routed
 * gateway, profile-scoped busy/awaiting, and a background state adapter that
 * never publishes into the foreground cache. There is deliberately no reduced
 * parallel pipeline — only this per-call execution seam.
 */

/**
 * The execution seam for a background, profile-targeted submit. Routes every
 * gateway call through the profile's OWN socket (never the active gateway),
 * drives the profile's busy pose through profile-scoped flags, and runs the
 * pipeline's optimistic state against the background adapter.
 */
export function backgroundSubmitExecution(profile: string): SubmitExecution {
  const key = normalizeProfileKey(profile)

  return {
    background: true,
    profile: key,
    // Profile-aware busy is decided BEFORE we get here (isProfileSessionBusy);
    // once executing, the foreground busy guard is irrelevant.
    readBusy: () => false,
    requestGateway: (method, params, timeoutMs, signal) =>
      requestGatewayForProfile(key, method, { ...params, profile: key }, timeoutMs, signal),
    resolveConnectionMode: async () => (await window.hermesDesktop?.getConnection(key))?.mode ?? 'local',
    scope: {
      clearAttachments: () => {},
      readAttachments: () => [],
      setAwaitingResponse: awaiting => setProfileAwaitingResponse(key, awaiting),
      setBusy: busy => setProfileSubmitBusy(key, busy),
      setMessages: () => {}
    },
    updateSessionState: (runtimeId, updater, storedId) => updateBackgroundSessionState(key, runtimeId, updater, storedId)
  }
}

/**
 * Whether a profile's session is busy, from real data sources: per-session
 * activity (runtime id), stored-id activity (survives runtime rotation), and —
 * only for the ACTIVE profile — the foreground working-session cache.
 */
export function isProfileSessionBusy(profile: string, runtimeId?: string | null, storedId?: string | null): boolean {
  const key = normalizeProfileKey(profile)

  if (runtimeId && profileRuntimeBusy(key, runtimeId)) {
    return true
  }

  if (storedId && hasBusyActivityForStoredSession(key, storedId)) {
    return true
  }

  return Boolean(
    storedId &&
      key === normalizeProfileKey($activeGatewayProfile.get()) &&
      $workingSessionIds.get().includes(storedId)
  )
}

/**
 * Pick a target session for a profile: the most-recent non-busy session from a
 * cross-profile listing scoped to that profile. Returns a durable storedSessionId
 * (the runtime id is resolved by the pipeline's session.resume path).
 */
export async function resolveProfileSession(
  profile: string,
  opts: { excludeBusy: boolean }
): Promise<{ sessionId?: string | null; storedSessionId?: string | null }> {
  const key = normalizeProfileKey(profile)
  const { sessions } = await listAllProfileSessions(200, 1, 'exclude', 'recent', key)

  for (const row of sessions) {
    if (opts.excludeBusy && isProfileSessionBusy(key, null, row.id)) {
      continue
    }

    return { sessionId: null, storedSessionId: row.id }
  }

  return {}
}

type SubmitTextFn = (text: string, options?: SubmitTextOptions) => Promise<boolean> | boolean

/**
 * Shared profile-targeted entry point used by the overlay submit handler and the
 * background queue drain. Resolves a target session when the caller has none,
 * queues behind a busy session (under the `(profile, storedSessionId)` key), or
 * runs the pipeline under a temporary gateway lease. `submitText` is injected so
 * this stays decoupled from the hook that owns the foreground pipeline.
 */
export async function submitTextForProfile(
  profile: string,
  text: string,
  source: { sessionId?: string | null; storedSessionId?: string | null },
  submitText: SubmitTextFn
): Promise<boolean> {
  const key = normalizeProfileKey(profile)
  let target = source

  if (!target.sessionId && !target.storedSessionId) {
    target = await resolveProfileSession(key, { excludeBusy: true })
  }

  if (!target.sessionId && !target.storedSessionId) {
    // TODO(PR4 i18n): copy.petNoActiveSession(key)
    notify({ kind: 'warning', message: `No active session to reply to on "${key}".` })

    return false
  }

  if (isProfileSessionBusy(key, target.sessionId, target.storedSessionId)) {
    if (!target.storedSessionId) {
      // TODO(PR4 i18n): copy.petCannotQueueNoDurableSession(key)
      notify({ kind: 'warning', message: `"${key}" is busy and can't be queued without a durable session.` })

      return false
    }

    return Boolean(
      enqueueQueuedPrompt(
        { profile: key, storedSessionId: target.storedSessionId },
        { attachments: [], profile: key, text }
      )
    )
  }

  return withProfileGatewayLease(key, () =>
    Promise.resolve(
      submitText(text, {
        sessionId: target.sessionId,
        storedSessionId: target.storedSessionId,
        // The active profile runs the ordinary foreground execution; only a
        // genuinely background profile gets the profile-routed seam.
        execution:
          key === normalizeProfileKey($activeGatewayProfile.get()) ? undefined : backgroundSubmitExecution(key)
      })
    )
  )
}
