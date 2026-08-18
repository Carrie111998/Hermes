import { atom } from 'nanostores'

import { persistString, storedString } from '@/lib/storage'

import { activeGatewayConnectionId, requestGatewayForAgent } from './gateway'
import { withinNativeNotifyBaseline } from './notify-baseline'
import { $activeGatewayProfile, normalizeProfileKey } from './profile'
import { clearApprovalRequest, replayPendingApproval } from './prompts'
import { $activeSessionId } from './session'

// Native OS notifications (Electron `Notification`), separate from the in-app
// toast feed in `notifications.ts`. Each kind toggles independently.
export type NativeNotificationKind =
  'approval' | 'backgroundDone' | 'credits' | 'input' | 'plugin' | 'turnDone' | 'turnError'

export const NATIVE_NOTIFICATION_KINDS: readonly NativeNotificationKind[] = [
  'approval',
  'input',
  'turnDone',
  'turnError',
  'backgroundDone',
  'credits',
  'plugin'
]

// Blocking prompts — surface even while focused if they're for another session.
const ATTENTION_KINDS = new Set<NativeNotificationKind>(['approval', 'input'])

export interface NativeNotificationPrefs {
  enabled: boolean
  kinds: Record<NativeNotificationKind, boolean>
}

const STORAGE_KEY = 'hermes:native-notifications'

const DEFAULT_PREFS: NativeNotificationPrefs = {
  enabled: true,
  kinds: {
    approval: true,
    backgroundDone: true,
    credits: true,
    input: true,
    plugin: true,
    turnDone: true,
    turnError: true
  }
}

function readPrefs(): NativeNotificationPrefs {
  const raw = storedString(STORAGE_KEY)

  if (!raw) {
    return DEFAULT_PREFS
  }

  try {
    const parsed = JSON.parse(raw) as Partial<NativeNotificationPrefs>
    const kinds = { ...DEFAULT_PREFS.kinds }

    for (const kind of NATIVE_NOTIFICATION_KINDS) {
      const value = parsed.kinds?.[kind]

      if (typeof value === 'boolean') {
        kinds[kind] = value
      }
    }

    return {
      enabled: typeof parsed.enabled === 'boolean' ? parsed.enabled : DEFAULT_PREFS.enabled,
      kinds
    }
  } catch {
    return DEFAULT_PREFS
  }
}

export const $nativeNotifyPrefs = atom<NativeNotificationPrefs>(readPrefs())

function writePrefs(next: NativeNotificationPrefs) {
  $nativeNotifyPrefs.set(next)
  persistString(STORAGE_KEY, JSON.stringify(next))
}

export function setNativeNotifyEnabled(enabled: boolean) {
  writePrefs({ ...$nativeNotifyPrefs.get(), enabled })
}

export function setNativeNotifyKind(kind: NativeNotificationKind, on: boolean) {
  const prev = $nativeNotifyPrefs.get()
  writePrefs({ ...prev, kinds: { ...prev.kinds, [kind]: on } })
}

// De-dupe replayed events for the same kind+session. Self-evicting: entries
// older than the window are pruned on every dispatch, so the map can't grow.
const THROTTLE_MS = 1000
const lastFiredAt = new Map<string, number>()

function throttled(key: string, now: number): boolean {
  for (const [k, at] of lastFiredAt) {
    if (now - at >= THROTTLE_MS) {
      lastFiredAt.delete(k)
    }
  }

  if (lastFiredAt.has(key)) {
    return true
  }

  lastFiredAt.set(key, now)

  return false
}

// "Backgrounded" = the user isn't on Hermes. `document.hidden` only flips when
// minimized/occluded; an alt-tabbed window is visible-but-unfocused, so we also
// check `document.hasFocus()`.
function isBackgrounded(): boolean {
  if (typeof document === 'undefined') {
    return false
  }

  if (document.hidden) {
    return true
  }

  return typeof document.hasFocus === 'function' && !document.hasFocus()
}

function shouldFire(
  kind: NativeNotificationKind,
  sessionId?: null | string,
  global = false,
  approvalConnectionId?: null | string,
  approvalProfile?: string
): boolean {
  // Global notifications aren't tied to a chat session (e.g. pet generation,
  // which runs from the command center with no active conversation). They fire
  // whenever the user is away, with no session-match requirement — otherwise a
  // background run started without an open session would be silently dropped.
  if (global) {
    return isBackgrounded()
  }

  // Attention kinds break through for an off-screen session even while focused.
  if (ATTENTION_KINDS.has(kind)) {
    const activeSessionMatches = Boolean(sessionId) && sessionId === $activeSessionId.get()

    const activeSourceMatches =
      kind !== 'approval' ||
      ((approvalConnectionId ?? null) === activeGatewayConnectionId() &&
        normalizeProfileKey(approvalProfile ?? $activeGatewayProfile.get()) ===
          normalizeProfileKey($activeGatewayProfile.get()))

    return isBackgrounded() || !activeSessionMatches || !activeSourceMatches
  }

  // Completion kinds: only the active session, only while away — so a busy
  // gateway (messaging, kanban, cron) can't spam a toast per background session.
  return isBackgrounded() && Boolean(sessionId) && sessionId === $activeSessionId.get()
}

export interface NativeNotificationAction {
  id: string
  text: string
}

export interface NativeNotificationInput {
  kind: NativeNotificationKind
  title: string
  body?: string
  sessionId?: null | string
  /**
   * Not tied to a chat session (e.g. pet generation). Fires whenever the user
   * is away, bypassing the session-match gate that completion kinds normally
   * require.
   */
  global?: boolean
  silent?: boolean
  actions?: NativeNotificationAction[]
  /** Opaque backend authority captured when an approval notification is created. */
  approvalRequestId?: string
  /** Backend source captured with the opaque approval authority. */
  approvalConnectionId?: null | string
  approvalProfile?: string
  /**
   * Extra throttle/dedupe discriminator for session-less notifications (e.g.
   * the plugin id), so unrelated emitters of the same kind don't collapse
   * into one another. Never drives click-to-focus like `sessionId` does.
   */
  tag?: string
}

export function dispatchNativeNotification(input: NativeNotificationInput): void {
  const prefs = $nativeNotifyPrefs.get()

  if (!prefs.enabled || !prefs.kinds[input.kind]) {
    return
  }

  if (withinNativeNotifyBaseline()) {
    return
  }

  if (
    !shouldFire(
      input.kind,
      input.sessionId,
      input.global,
      input.approvalConnectionId,
      input.approvalProfile
    )
  ) {
    return
  }

  const throttleKey =
    input.kind === 'approval'
      ? JSON.stringify([
          input.kind,
          input.sessionId ?? null,
          input.approvalConnectionId ?? null,
          input.approvalProfile ?? null,
          input.approvalRequestId ?? null
        ])
      : `${input.kind}:${input.sessionId ?? input.tag ?? (input.global ? 'global' : '')}`

  if (throttled(throttleKey, Date.now())) {
    return
  }

  void window.hermesDesktop?.notify({
    actions: input.actions,
    approvalConnectionId: input.approvalConnectionId,
    approvalProfile: input.approvalProfile,
    approvalRequestId: input.approvalRequestId,
    body: input.body,
    kind: input.kind,
    sessionId: input.sessionId ?? undefined,
    silent: input.silent,
    tag: input.tag,
    title: input.title
  })
}

// -- the plugin door (`ctx.os.notify`) ----------------------------------------

export interface PluginNativeNotificationInput {
  title: string
  body?: string
  silent?: boolean
}

/** Native OS notification on behalf of a plugin. One "Plugin notifications"
 *  preference gates all plugins; the plugin id keys throttling/dedupe so two
 *  plugins can't collapse each other's notifications. Fires only while the
 *  user is away from Hermes — the in-app toast (`host.notify`) covers the
 *  foreground case. */
export function dispatchPluginNativeNotification(pluginId: string, input: PluginNativeNotificationInput): void {
  dispatchNativeNotification({ ...input, global: true, kind: 'plugin', tag: pluginId })
}

// Resolve the exact approval captured by this notification, mirroring the
// in-app Run/Reject bar. Never re-read mutable session prompt state here: an old
// OS notification can outlive the request that replaced it in the renderer.
export async function respondToApprovalAction(
  sessionId: null | string,
  requestId: string,
  actionId: string,
  source: { connectionId: null | string; profile: string }
): Promise<void> {
  const choice = actionId === 'approve' ? 'once' : actionId === 'reject' ? 'deny' : null

  if (!choice) {
    return
  }

  if (!source.profile) {
    return
  }

  if (!requestId) {
    return
  }

  try {
    const request = <T>(method: string, params: Record<string, unknown>) =>
      requestGatewayForAgent<T>(source.connectionId, source.profile, method, params)

    const result = await request<{ resolved?: number }>('approval.respond', {
      choice,
      request_id: requestId,
      session_id: sessionId ?? undefined
    })

    if (result?.resolved === 1) {
      clearApprovalRequest(sessionId, requestId, source)
      await replayPendingApproval({ request }, sessionId, source)
    }
  } catch {
    // Leave the prompt parked so the user can still resolve it in-app.
  }
}

// Settings "send test" — bypasses gating. Returns whether the OS accepted it so
// the panel can flag a silent permission failure instead of looking dead.
export async function sendTestNativeNotification(title: string, body: string): Promise<boolean> {
  const bridge = window.hermesDesktop

  if (!bridge?.notify) {
    return false
  }

  try {
    return await bridge.notify({ body, kind: 'turnDone', title })
  } catch {
    return false
  }
}
