/**
 * Desktop self-update store. Tracks distance from the configured branch,
 * surfaces it as an ambient pill, and orchestrates the apply flow.
 */

import { atom } from 'nanostores'

import type {
  DesktopUpdateApplyOptions,
  DesktopUpdateApplyResult,
  DesktopUpdateBlocker,
  DesktopUpdateProgress,
  DesktopUpdateStage,
  DesktopUpdateStatus,
  DesktopVersionInfo
} from '@/global'
import { checkHermesUpdate, getActionStatus, getScopedStatus, getStatus, restartGateway, updateHermes } from '@/hermes'
import { translateNow } from '@/i18n'
import { persistString, storedString } from '@/lib/storage'
import { $connectionsRegistry, refreshConnectionsRegistry } from '@/store/connections'
import { applyFleetUpdates, type FleetUpdateResult } from '@/store/fleet-updates'
import { reconnectGateway } from '@/store/gateway-reconnect'
import { dismissNotification, notify } from '@/store/notifications'
import { $connection } from '@/store/session'
import type { BackendUpdateCheckResponse } from '@/types/hermes'

export interface UpdateApplyState {
  applying: boolean
  stage: DesktopUpdateStage
  message: string
  percent: number | null
  error: string | null
  /** When the stage is 'manual': the exact command the user should run
   *  (CLI install with no staged updater). */
  command: string | null
  /** Structured update blockers used by the safe close-and-update confirmation. */
  blockers?: readonly DesktopUpdateBlocker[] | null
  log: readonly { stage: DesktopUpdateStage; message: string; at: number }[]
}

const IDLE: UpdateApplyState = {
  applying: false,
  stage: 'idle',
  message: '',
  percent: null,
  error: null,
  command: null,
  log: []
}

export const $desktopVersion = atom<DesktopVersionInfo | null>(null)
export const $updateApply = atom<UpdateApplyState>(IDLE)
export const $updateChecking = atom<boolean>(false)
export const $updateOverlayOpen = atom<boolean>(false)
export const $updateStatus = atom<DesktopUpdateStatus | null>(null)

// Client and backend are independently updatable; each keeps its own state.
export const $backendUpdateStatus = atom<DesktopUpdateStatus | null>(null)
export const $backendUpdateApply = atom<UpdateApplyState>(IDLE)
export const $backendUpdateChecking = atom<boolean>(false)

export type UpdateTarget = 'client' | 'backend'
export const $updateOverlayTarget = atom<UpdateTarget>('client')

export const setUpdateOverlayOpen = (open: boolean) => $updateOverlayOpen.set(open)

export const openUpdateOverlayFor = (target: UpdateTarget) => {
  $updateOverlayTarget.set(target)
  $updateOverlayOpen.set(true)
  void (target === 'backend' ? checkBackendUpdates() : checkUpdates())
}

export const resetUpdateApplyState = () => {
  $updateApply.set(IDLE)
  $backendUpdateApply.set(IDLE)
}

const UPDATE_TOAST_IDS: Record<UpdateTarget, string> = {
  client: 'desktop-update-available',
  backend: 'backend-update-available'
}
// Time-based snooze instead of per-sha dismissal: this repo lands ~100 commits
// a day, so a "don't show this exact sha again" guard re-popped the toast on
// every new commit. We instead suppress the toast for a cooldown window that
// (re)starts whenever the user closes it.
const UPDATE_TOAST_SNOOZE_KEYS: Record<UpdateTarget, string> = {
  client: 'hermes:update-toast-snooze-until',
  backend: 'hermes:backend-update-toast-snooze-until'
}
const UPDATE_TOAST_COOLDOWN_MS = 24 * 60 * 60 * 1000

function snoozeUpdateToast(target: UpdateTarget): void {
  persistString(UPDATE_TOAST_SNOOZE_KEYS[target], String(Date.now() + UPDATE_TOAST_COOLDOWN_MS))
}

function isUpdateToastSnoozed(target: UpdateTarget): boolean {
  const until = Number(storedString(UPDATE_TOAST_SNOOZE_KEYS[target]) || 0)

  return Number.isFinite(until) && Date.now() < until
}

// Must match tui_gateway's DESKTOP_BACKEND_CONTRACT that this build was written
// against. The backend reports its own value in session runtime info; a lower
// value (or none — a pre-GUI checkout) means GUI<->backend skew.
// v2: requires the file.attach RPC (remote-gateway non-image file upload).
// v3: requires approvals.mode config RPCs and session.info reconciliation.
// v4: requires explicit Fast-off session creation and session-scoped Fast edits.
// v5: requires raised WebSocket frame size for large one-shot file.attach.
// v6: requires key-addressed plugins.manage rows (keyless rows render
//     read-only in Settings → Plugins).
const REQUIRED_BACKEND_CONTRACT = 6
const SKEW_TOAST_ID = 'backend-contract-skew'
// The contract check runs on every session.resume (applyRuntimeInfo), so
// without a snooze the warning re-popped on every thread the user opened, even
// right after they closed it. Mirror the update toast: persist a cooldown when
// the user dismisses it. It still reminds again after the window if the backend
// is still behind, and clears immediately once the backend catches up.
const SKEW_TOAST_SNOOZE_KEY = 'hermes:backend-skew-toast-snooze-until'
const SKEW_TOAST_COOLDOWN_MS = 24 * 60 * 60 * 1000

function snoozeSkewToast(): void {
  persistString(SKEW_TOAST_SNOOZE_KEY, String(Date.now() + SKEW_TOAST_COOLDOWN_MS))
}

function isSkewToastSnoozed(): boolean {
  const until = Number(storedString(SKEW_TOAST_SNOOZE_KEY) || 0)

  return Number.isFinite(until) && Date.now() < until
}

const INSTALL_METHOD_TOAST_ID = 'install-method-not-supported'
// Same time-based snooze pattern as the update/skew toasts: the warning is
// re-derived from every session.info (session.create/resume/activate all
// route through applyRuntimeInfo), so without a snooze it would re-pop on
// every session switch even right after the user dismissed it.
const INSTALL_METHOD_TOAST_SNOOZE_KEY = 'hermes:install-method-toast-snooze-until'
const INSTALL_METHOD_TOAST_COOLDOWN_MS = 24 * 60 * 60 * 1000

function snoozeInstallMethodToast(): void {
  persistString(INSTALL_METHOD_TOAST_SNOOZE_KEY, String(Date.now() + INSTALL_METHOD_TOAST_COOLDOWN_MS))
}

function isInstallMethodToastSnoozed(): boolean {
  const until = Number(storedString(INSTALL_METHOD_TOAST_SNOOZE_KEY) || 0)

  return Number.isFinite(until) && Date.now() < until
}

/**
 * Guard against a desktop GUI talking to a backend that predates its contract
 * (e.g. a bb/gui-built app pointed at a `main` checkout). Rather than failing
 * cryptically downstream, surface a warning with a one-click align that runs
 * the normal update flow (which self-heals to the right branch).
 *
 * Runs on every session open; closing the toast snoozes it for a cooldown so it
 * doesn't nag on every thread switch.
 */
export function reportBackendContract(contract: number | undefined): void {
  if ((contract ?? 0) >= REQUIRED_BACKEND_CONTRACT) {
    dismissNotification(SKEW_TOAST_ID)
    // Backend caught up — forget any prior snooze so a future regression warns
    // immediately rather than staying silent for the rest of the window.
    persistString(SKEW_TOAST_SNOOZE_KEY, null)

    return
  }

  if (isSkewToastSnoozed()) {
    return
  }

  notify({
    action: {
      label: translateNow('notifications.updateHermes'),
      onClick: () => {
        snoozeSkewToast()
        void applyBackendUpdate()
      }
    },
    durationMs: 0,
    id: SKEW_TOAST_ID,
    kind: 'warning',
    message: translateNow('notifications.backendOutOfDateMessage'),
    onDismiss: () => snoozeSkewToast(),
    title: translateNow('notifications.backendOutOfDateTitle')
  })
}

export function reportInstallMethodWarning(message: string | undefined): void {
  if (!message) {
    dismissNotification(INSTALL_METHOD_TOAST_ID)

    return
  }

  if (isInstallMethodToastSnoozed()) {
    return
  }

  notify({
    durationMs: 0,
    id: INSTALL_METHOD_TOAST_ID,
    kind: 'warning',
    message,
    onDismiss: () => snoozeInstallMethodToast(),
    title: translateNow('notifications.installMethodUnsupportedTitle')
  })
}

/**
 * Fire a toast when an update is available, at most once per cooldown window.
 * Closing the toast — dismissing it or opening the updates window from it —
 * (re)starts the cooldown, so a busy upstream branch doesn't re-spam the user
 * on every new commit. The snooze is persisted, so it survives relaunches too.
 */
export function maybeNotifyUpdateAvailable(status: DesktopUpdateStatus | null, target: UpdateTarget = 'client') {
  if (!status || status.supported === false || status.error || !status.targetSha) {
    return
  }

  const behind = typeof status.behind === 'number' ? status.behind : null

  // behind === null means "update available, exact count unknown" (shallow
  // clone). That still deserves the toast — just with count-free copy.
  if ((behind ?? 0) <= 0 && !status.updateAvailable) {
    return
  }

  if (isUpdateToastSnoozed(target)) {
    return
  }

  const apply = target === 'backend' ? $backendUpdateApply.get() : $updateApply.get()

  if (apply.applying) {
    return
  }

  notify({
    action: {
      label: translateNow('notifications.seeWhatsNew'),
      onClick: () => {
        snoozeUpdateToast(target)
        openUpdateOverlayFor(target)
      }
    },
    durationMs: 0,
    icon: 'gift',
    id: UPDATE_TOAST_IDS[target],
    kind: 'info',
    message:
      behind !== null && behind > 0
        ? translateNow('notifications.updateReadyMessage', behind)
        : translateNow('notifications.updateReadyMessageUnknown'),
    onDismiss: () => snoozeUpdateToast(target),
    title: translateNow('notifications.updateReadyTitle')
  })
}

export function openUpdatesWindow(): void {
  openUpdateOverlayFor(isRemoteMode() ? 'backend' : 'client')
}

/** Open and apply one explicitly named update target.
 *
 * Settings → About renders the desktop client and a connected remote
 * backend as separate rows.  Those row actions must stay pinned to the row the
 * user chose; routing through `startActiveUpdate()` would intentionally fan out
 * to every target and recreates the client/backend ambiguity that the split UI
 * is meant to remove.
 */
export function startUpdateFor(target: UpdateTarget): void {
  $updateOverlayTarget.set(target)
  $updateOverlayOpen.set(true)
  void (target === 'backend' ? applyBackendUpdate() : applyUpdates())
}

/**
 * Start applying the available update for the active target right away. Opens
 * the updates overlay first so the user sees apply progress (the overlay
 * renders ApplyingView once `applying` flips true), then kicks off the install.
 * Used by the "Update now" affordance on the About panel, which would otherwise
 * only be able to open the changelog overlay.
 *
 * Multi-target installs (remote mode / multi-connection registry) route
 * through the everything-flow so "update" means every machine, not just the
 * active target — the single-target ternary is what left remote-mode users
 * updating the backend forever while the GUI itself went stale.
 */
export function startActiveUpdate(): void {
  if (hasMultipleUpdateTargets()) {
    $updateOverlayOpen.set(true)
    void applyEverythingUpdate()

    return
  }

  const target: UpdateTarget = isRemoteMode() ? 'backend' : 'client'
  startUpdateFor(target)
}

/**
 * Command-palette entry point. The About panel's "Update now" only renders once
 * we know an update is waiting; this row is always listed, so it also has to
 * handle "already current" — open the overlay for the active target and let its
 * check answer, and only apply when there's something to install. On
 * multi-target installs an update waiting on EITHER the client or the backend
 * triggers the everything-flow.
 */
export function requestActiveUpdate(): void {
  if (hasMultipleUpdateTargets()) {
    const clientStatus = $updateStatus.get()
    const backendStatus = $backendUpdateStatus.get()

    const anyBehind =
      (clientStatus?.behind ?? 0) > 0 ||
      clientStatus?.updateAvailable ||
      (backendStatus?.behind ?? 0) > 0 ||
      backendStatus?.updateAvailable

    if (anyBehind) {
      startActiveUpdate()

      return
    }
  }

  const target: UpdateTarget = isRemoteMode() ? 'backend' : 'client'
  const status = target === 'backend' ? $backendUpdateStatus.get() : $updateStatus.get()

  if ((status?.behind ?? 0) > 0 || status?.updateAvailable) {
    startActiveUpdate()

    return
  }

  openUpdateOverlayFor(target)
}

/** Re-read the running app's version from the Electron main process and
 *  publish it on `$desktopVersion`. Called when the About panel mounts, the
 *  update flow finishes, and the window regains focus, so the About text
 *  stays in sync with the just-installed binary instead of frozen at the
 *  value captured at first-load. */
export async function refreshDesktopVersion(): Promise<DesktopVersionInfo | null> {
  if (typeof window === 'undefined') {
    return null
  }

  // Best-effort UI sync: callers (checkUpdates, startUpdatePoller, window
  // focus handler) all kick this off with `void refreshDesktopVersion()`,
  // so any rejection from the IPC bridge (e.g. main process shutting down
  // mid-reload, or the bridge not yet ready on first paint) would surface
  // as an unhandled promise rejection in the renderer. Swallow it.
  try {
    const next = await window.hermesDesktop?.getVersion?.()

    if (next) {
      $desktopVersion.set(next)
    }

    return next ?? null
  } catch {
    return null
  }
}

function isRemoteMode(): boolean {
  return $connection.get()?.mode === 'remote'
}

interface BackendAuthority {
  key: string
  scope: undefined | { connectionId: string; profile?: string }
}

/** Snapshot the backend identity before an await. Registry connections carry
 * an explicit request pin; legacy remotes keep their old ambient route but are
 * still guarded by base URL/profile so an A response cannot repaint B. */
function activeBackendAuthority(): BackendAuthority | null {
  const connection = $connection.get()

  if (connection?.mode !== 'remote') {
    return null
  }

  const connectionId = connection.connectionId?.trim()
  const baseUrl = connection.baseUrl?.trim() || ''
  const profile = connection.profile?.trim()

  return {
    key: `${baseUrl}::${connectionId || ''}::${profile || 'default'}`,
    scope: connectionId ? { connectionId, ...(profile ? { profile } : {}) } : undefined
  }
}

function backendAuthorityIsCurrent(authority: BackendAuthority): boolean {
  return activeBackendAuthority()?.key === authority.key
}

function checkBackendFor(authority: BackendAuthority, force = true): Promise<BackendUpdateCheckResponse> {
  return authority.scope ? checkHermesUpdate(force, authority.scope) : checkHermesUpdate(force)
}

function updateBackendFor(authority: BackendAuthority) {
  return authority.scope ? updateHermes(authority.scope) : updateHermes()
}

function backendActionStatusFor(authority: BackendAuthority, name: string, lines: number) {
  return getActionStatus(name, lines, authority.scope)
}

function backendStatusFor(authority: BackendAuthority) {
  return authority.scope ? getScopedStatus(authority.scope) : getStatus()
}

function mapBackendCheck(
  res: BackendUpdateCheckResponse,
  runtime?: Awaited<ReturnType<typeof getStatus>> | null
): DesktopUpdateStatus {
  return {
    supported: res.can_apply,
    message: res.message ?? undefined,
    updateAvailable: res.update_available,
    behind: res.behind === null ? null : res.behind > 0 ? res.behind : 0,
    currentVersion: res.current_version,
    gatewayRestartRequired: runtime?.gateway_restart_required === true,
    gatewayProfile: runtime?.gateway_profile?.trim() || undefined,
    gatewayCodeSha: runtime?.gateway_code_sha?.trim() || undefined,
    checkoutCodeSha: runtime?.checkout_code_sha?.trim() || undefined,
    targetSha: res.update_available ? `backend:${res.current_version}` : undefined,
    commits: res.commits,
    fetchedAt: Date.now()
  }
}

const backendChecks = new Map<string, Promise<DesktopUpdateStatus | null>>()

function syncBackendChecking(): void {
  const current = activeBackendAuthority()
  $backendUpdateChecking.set(Boolean(current && backendChecks.has(current.key)))
}

// The renderer has one visible backend-update slot, so its contents must have
// one equally explicit owner. Clear that slot synchronously on every remote
// connection/profile re-home; an A result may publish after A→B→A, but A's
// cached status can never be shown or acted on while B owns the window.
let observedBackendAuthorityKey = activeBackendAuthority()?.key ?? null
$connection.subscribe(() => {
  const authorityKey = activeBackendAuthority()?.key ?? null

  if (authorityKey === observedBackendAuthorityKey) {
    return
  }

  observedBackendAuthorityKey = authorityKey
  $backendUpdateStatus.set(null)
  $backendUpdateApply.set(IDLE)
  syncBackendChecking()
})

export function checkBackendUpdates(): Promise<DesktopUpdateStatus | null> {
  const authority = activeBackendAuthority()

  if (!authority) {
    return Promise.resolve($backendUpdateStatus.get())
  }

  const existing = backendChecks.get(authority.key)

  if (existing) {
    $backendUpdateChecking.set(true)
    return existing
  }

  $backendUpdateChecking.set(true)

  let request!: Promise<DesktopUpdateStatus | null>
  request = (async () => {
    try {
      const [check, runtime] = await Promise.all([
        checkBackendFor(authority, true),
        backendStatusFor(authority).catch(() => null)
      ])
      const status = mapBackendCheck(check, runtime)

      if (backendAuthorityIsCurrent(authority)) {
        $backendUpdateStatus.set(status)
        maybeNotifyUpdateAvailable(status, 'backend')
      }

      return status
    } catch (error) {
      const fallback: DesktopUpdateStatus = {
        supported: $backendUpdateStatus.get()?.supported ?? true,
        error: 'check-failed',
        message: error instanceof Error ? error.message : String(error),
        fetchedAt: Date.now()
      }

      if (backendAuthorityIsCurrent(authority)) {
        $backendUpdateStatus.set(fallback)
      }

      return fallback
    } finally {
      if (backendChecks.get(authority.key) === request) {
        backendChecks.delete(authority.key)
      }

      syncBackendChecking()
    }
  })()

  backendChecks.set(authority.key, request)

  return request
}

export async function checkUpdates(): Promise<DesktopUpdateStatus | null> {
  const bridge = window.hermesDesktop?.updates

  if (!bridge || $updateChecking.get()) {
    return $updateStatus.get()
  }

  $updateChecking.set(true)

  try {
    const status = await bridge.check()
    $updateStatus.set(status)
    maybeNotifyUpdateAvailable(status, 'client')
    void refreshDesktopVersion()

    return status
  } catch (error) {
    const previous = $updateStatus.get()

    const fallback: DesktopUpdateStatus = {
      supported: previous?.supported ?? true,
      branch: previous?.branch,
      error: 'check-failed',
      message: error instanceof Error ? error.message : String(error),
      fetchedAt: Date.now()
    }

    $updateStatus.set(fallback)

    return fallback
  } finally {
    $updateChecking.set(false)
  }
}

export async function applyUpdates(opts: DesktopUpdateApplyOptions = {}): Promise<DesktopUpdateApplyResult> {
  const bridge = window.hermesDesktop?.updates

  if (!bridge) {
    return { ok: false, error: 'unavailable', message: 'Desktop bridge unavailable.' }
  }

  dismissNotification(UPDATE_TOAST_IDS.client)
  $updateApply.set({ ...IDLE, applying: true, stage: 'prepare', message: 'Starting update…' })

  try {
    const result = await bridge.apply(opts)

    // CLI install with no staged updater: not an error — the user just runs
    // `hermes update` themselves. Land on a dedicated manual state so the
    // overlay shows the command + copy button instead of a dead retry loop.
    if (result?.manual) {
      $updateApply.set({
        ...IDLE,
        applying: false,
        stage: 'manual',
        message: result.command ?? 'hermes update',
        command: result.command ?? 'hermes update'
      })

      return result
    }

    // A detached relauncher took over (macOS bundle swap / Linux re-exec): the
    // app is about to quit and reopen, so hold the "Restarting…" view until it
    // does. Every other resolved outcome MUST land on a terminal, closeable
    // state: the apply IPC resolves here, but the progress stream may have left
    // us on a non-terminal stage (e.g. 'done'/'rebuild'), which renders as a
    // spinner with no close button — the exact hang this guards against.
    // Linux GUI/backend skew (#45205): the backend was updated but the running
    // desktop app PACKAGE was not changed (AppImage/.deb/.rpm). We must NOT tell
    // the user "the new version loads next launch" — that's false; this packaged
    // shell keeps running old GUI code against the new backend. Land on the
    // dedicated, closeable guiSkew terminal state telling them to update/reinstall
    // the desktop app.
    if (result?.guiSkew) {
      $updateApply.set({
        ...IDLE,
        applying: false,
        stage: 'guiSkew',
        message: result.message ?? translateNow('updates.guiSkewBody')
      })

      return result
    }

    // Backend updated but the app couldn't auto-relaunch (e.g. the rebuilt
    // sandbox helper isn't launchable): keep a closeable manual-restart state so
    // the user keeps a working window instead of a dead app or a stuck spinner.
    if (result?.ok && result?.manualRestart) {
      $updateApply.set({
        ...IDLE,
        applying: false,
        stage: 'manual',
        message: result.message ?? translateNow('updates.manualPickedUp')
      })

      return result
    }

    if (!result?.handedOff) {
      if (result?.ok) {
        // Updated, but couldn't relaunch in place (AppImage / dev run). Dismiss
        // the overlay and let the user know the new version loads next launch
        // rather than stranding them on an un-closeable spinner.
        setUpdateOverlayOpen(false)
        resetUpdateApplyState()
        notify({
          durationMs: 8000,
          id: UPDATE_TOAST_IDS.client,
          kind: 'success',
          message: translateNow('updates.manualPickedUp'),
          // No action button here, but it's still update-lifecycle news — keep
          // it with the other update toasts instead of the ambient bottom-right
          // stack.
          placement: 'default',
          title: translateNow('updates.allSetTitle')
        })
      } else {
        $updateApply.set({
          ...$updateApply.get(),
          applying: false,
          stage: 'error',
          error: result?.error ?? 'apply-failed',
          message: result?.message ?? translateNow('updates.errorBody'),
          blockers: result?.blockers ?? null
        })
      }
    }

    return result
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    $updateApply.set({ ...$updateApply.get(), applying: false, stage: 'error', error: 'apply-failed', message })

    return { ok: false, error: 'apply-failed', message }
  }
}

const BACKEND_ACTION_POLL_MS = 1500
const BACKEND_ACTION_MAX_MS = 6 * 60 * 1000
const BACKEND_RETURN_MAX_MS = 4 * 60 * 1000

function finishBackendApply(authority: BackendAuthority): DesktopUpdateApplyResult {
  if (backendAuthorityIsCurrent(authority)) {
    $backendUpdateApply.set(IDLE)
    setUpdateOverlayOpen(false)
    void checkBackendUpdates()
    // The update restarted the gateway process, which strands this window's
    // WebSocket: over SSH/tailscale tunnels the old TCP connection often dies
    // without a close event, so connectionState still reads 'open' while every
    // RPC hangs — users force-quit the app to recover. Nudge the registered
    // reconnect handler (forceReconnectNow), which retires the half-open
    // socket and re-dials with a fresh ticket. Best-effort: local installs
    // whose socket survived treat it as a cheap probe.
    void reconnectGateway().catch(() => undefined)
    // The backend caught up, but the CLIENT may still be behind — the exact
    // gap that strands remote-mode users on an old GUI forever (every update
    // affordance in remote mode targets the backend, so nothing ever told
    // them the app itself was stale). Nudge with a one-click client update.
    void maybeNudgeClientAfterBackendUpdate()
  }

  return { ok: true, message: 'Backend update applied.' }
}

function ingestBackendActionStatus(
  authority: BackendAuthority,
  status: Awaited<ReturnType<typeof getActionStatus>>
): void {
  if (!backendAuthorityIsCurrent(authority)) {
    return
  }

  const current = $backendUpdateApply.get()

  const log = status.lines
    .filter(line => line.trim().length > 0)
    .map(line => ({ at: Date.now(), message: line, stage: current.stage }))
    .slice(-50)

  const latest = log.at(-1)?.message

  if (log.length === 0 && !latest) {
    return
  }

  $backendUpdateApply.set({
    ...current,
    applying: true,
    stage: current.stage === 'idle' ? 'pull' : current.stage,
    log,
    message: latest ?? current.message
  })
}

function completedAfterRestart(
  status: Awaited<ReturnType<typeof getActionStatus>>,
  actionId: string | undefined
): boolean {
  return !!actionId && status.lines.some(line => line === `=== hermes-update completed ${actionId} ===`)
}

/** Whether the durable update receipt attached to the status proves the
 *  outcome of THIS apply (#91277 bullet 3). Only a finished receipt whose
 *  run started at-or-after we kicked the update off counts — an older
 *  receipt describes a previous update, and a still-running one proves
 *  nothing yet. The 60s slack absorbs client/backend clock skew. */
function receiptProvesOutcome(
  status: Awaited<ReturnType<typeof getActionStatus>>,
  applyStartedAtMs: number,
  expectedCorrelationId?: string
): boolean {
  const receipt = status.receipt

  if (!receipt || !receipt.finished_at || !receipt.started_at) {
    return false
  }

  if (
    receipt.outcome !== 'success' &&
    receipt.outcome !== 'partial' &&
    receipt.outcome !== 'failed' &&
    receipt.outcome !== 'refused'
  ) {
    return false
  }

  if (expectedCorrelationId) {
    if (receipt.correlation_id) {
      return receipt.correlation_id === expectedCorrelationId
    }

    if (status.action_id?.trim() !== expectedCorrelationId) {
      return false
    }
  }

  const startedMs = Date.parse(receipt.started_at)

  return Number.isFinite(startedMs) && startedMs >= applyStartedAtMs - 60_000
}

function legacyBackendReachedTarget(
  status: BackendUpdateCheckResponse,
  targetSha: string | undefined,
  previousVersion: string | undefined
): boolean {
  if (status.behind === 0) {
    return true
  }

  if (previousVersion && status.current_version !== previousVersion) {
    return true
  }

  return !!targetSha && !!status.commits?.length && !status.commits.some(commit => commit.sha === targetSha)
}

const backendUpdatesInFlight = new Map<string, Promise<DesktopUpdateApplyResult>>()

function activeManagedSshConnection(authority: BackendAuthority) {
  const connectionId = authority.scope?.connectionId

  if (!connectionId) {
    return null
  }

  const connection = $connectionsRegistry.get()?.connections.find(candidate => candidate.id === connectionId)

  return connection?.kind === 'ssh' ? connection : null
}

async function runManagedSshBackendUpdate(authority: BackendAuthority): Promise<DesktopUpdateApplyResult> {
  const connection = activeManagedSshConnection(authority)
  const updateManaged = window.hermesDesktop?.connections?.updateManaged

  if (!connection || !updateManaged) {
    const message = !connection
      ? 'No Desktop-managed SSH backend is active.'
      : 'This Desktop version cannot safely update a managed SSH backend.'

    if (backendAuthorityIsCurrent(authority)) {
      $backendUpdateApply.set({
        ...IDLE,
        applying: false,
        stage: 'error',
        error: 'managed-ssh-unavailable',
        message
      })
    }

    return { ok: false, error: 'managed-ssh-unavailable', message }
  }

  if (backendAuthorityIsCurrent(authority)) {
    $backendUpdateApply.set({
      ...IDLE,
      applying: true,
      stage: 'pull',
      message: translateNow('updates.applyStatus.pulling')
    })
  }

  const result = await updateManaged(connection.id)
  const message = result.message || result.error || result.receipt?.stopReason || 'Managed SSH update failed.'

  if (result.ok) {
    return finishBackendApply(authority)
  }

  const partial = result.updateOk && !result.restoreOk
  if (backendAuthorityIsCurrent(authority)) {
    $backendUpdateApply.set({
      ...IDLE,
      applying: false,
      stage: 'error',
      error: partial ? 'partial' : 'apply-failed',
      message
    })
  }

  return { ok: false, error: partial ? 'partial' : 'apply-failed', message }
}

async function runBackendUpdate(authority: BackendAuthority): Promise<DesktopUpdateApplyResult> {
  dismissNotification(UPDATE_TOAST_IDS.backend)
  if (backendAuthorityIsCurrent(authority)) {
    $backendUpdateApply.set({
      ...IDLE,
      applying: true,
      stage: 'prepare',
      message: translateNow('updates.applyStatus.preparing')
    })
  }

  try {
    // A registry-backed SSH serve process is owned by Electron. Posting the
    // generic HTTP updater into that live process can replace its own venv
    // (and fails outright on Windows). Route every active-backend affordance
    // through the same drain/update/restore transaction as the fleet rows.
    if (activeManagedSshConnection(authority)) {
      return await runManagedSshBackendUpdate(authority)
    }

    const previousStatus = $backendUpdateStatus.get()
    const requestedTargetSha = previousStatus?.commits?.at(0)?.sha

    const previousVersion = previousStatus?.targetSha?.startsWith('backend:')
      ? previousStatus.targetSha.slice('backend:'.length)
      : undefined

    const started = await updateBackendFor(authority)
    const applyStartedAtMs = Date.now()

    if (!started.ok) {
      const message = (started as { message?: string }).message || translateNow('updates.applyStatus.notAvailable')
      const command = (started as { update_command?: string }).update_command || 'hermes update'
      if (backendAuthorityIsCurrent(authority)) {
        $backendUpdateApply.set({ ...IDLE, applying: false, stage: 'manual', message, command })
      }

      return { ok: false, error: 'manual', manual: true, message, command }
    }

    if (backendAuthorityIsCurrent(authority)) {
      $backendUpdateApply.set({
        ...IDLE,
        applying: true,
        stage: 'pull',
        message: translateNow('updates.applyStatus.pulling')
      })
    }

    let last: Awaited<ReturnType<typeof getActionStatus>> | null = null
    // Backups, dependency repair, and builds can legitimately take several
    // minutes. Keep the generous cap only as a guard against a stuck action.
    const actionDeadline = Date.now() + BACKEND_ACTION_MAX_MS
    let deadline = actionDeadline
    let reconnecting = false

    while (Date.now() < deadline) {
      await new Promise(resolve => globalThis.setTimeout(resolve, BACKEND_ACTION_POLL_MS))

      try {
        last = await backendActionStatusFor(authority, started.name, 2000)
        ingestBackendActionStatus(authority, last)
      } catch {
        if (!reconnecting) {
          reconnecting = true
          deadline = Date.now() + BACKEND_RETURN_MAX_MS
          if (backendAuthorityIsCurrent(authority)) {
            $backendUpdateApply.set({
              ...$backendUpdateApply.get(),
              applying: true,
              stage: 'restart',
              message: translateNow('updates.applyStatus.restarting')
            })
          }
        }

        continue
      }

      if (last.running) {
        if (reconnecting) {
          reconnecting = false
          deadline = actionDeadline
          if (backendAuthorityIsCurrent(authority)) {
            $backendUpdateApply.set({
              ...$backendUpdateApply.get(),
              applying: true,
              stage: 'pull',
              message: translateNow('updates.applyStatus.pulling')
            })
          }
        }

        continue
      }

      // #91277 bullet 3: the backend now attaches the durable update
      // receipt to the status. A receipt whose run STARTED after we kicked
      // this update off is authoritative — read its outcome instead of
      // inferring from log markers or timing out across the restart gap.
      const expectedCorrelationId = started.correlation_id?.trim() || started.action_id?.trim() || undefined

      if (receiptProvesOutcome(last, applyStartedAtMs, expectedCorrelationId)) {
        if (last.receipt!.outcome === 'refused') {
          const message = last.receipt!.refusal?.message || translateNow('updates.applyStatus.notAvailable')
          const command = last.receipt!.refusal?.update_command || 'hermes update'

          if (backendAuthorityIsCurrent(authority)) {
            $backendUpdateApply.set({ ...IDLE, applying: false, stage: 'manual', message, command })
          }

          return { ok: false, error: 'manual', manual: true, message, command }
        }

        if (last.receipt!.outcome === 'partial') {
          const message = translateNow('updates.applyStatus.partial')

          if (backendAuthorityIsCurrent(authority)) {
            $backendUpdateApply.set({
              ...IDLE,
              applying: false,
              stage: 'error',
              error: 'partial',
              message
            })
          }

          return { ok: false, error: 'partial', message }
        }

        if (last.receipt!.outcome === 'failed') {
          const message = last.receipt!.stop_reason?.trim() || translateNow('updates.applyStatus.failed')

          if (backendAuthorityIsCurrent(authority)) {
            $backendUpdateApply.set({
              ...IDLE,
              applying: false,
              stage: 'error',
              error: 'apply-failed',
              message
            })
          }

          return { ok: false, error: 'apply-failed', message }
        }

        return finishBackendApply(authority)
      }

      const statusMatchesStarted = !expectedCorrelationId || last.action_id?.trim() === expectedCorrelationId

      if (
        statusMatchesStarted &&
        (last.exit_code === 0 || (last.exit_code === null && completedAfterRestart(last, started.action_id)))
      ) {
        return finishBackendApply(authority)
      }

      if (expectedCorrelationId && !statusMatchesStarted) {
        continue
      }

      if (!started.action_id && last.exit_code === null) {
        try {
          const status = await checkBackendFor(authority, true)

          if (legacyBackendReachedTarget(status, requestedTargetSha, previousVersion)) {
            return finishBackendApply(authority)
          }
        } catch {
          continue
        }
      }

      if (last.exit_code !== null) {
        break
      }
    }

    if (backendAuthorityIsCurrent(authority)) {
      $backendUpdateApply.set({
        ...$backendUpdateApply.get(),
        applying: false,
        stage: 'error',
        error: 'apply-failed',
        message: translateNow('updates.applyStatus.failed')
      })
    }

    return { ok: false, error: 'apply-failed', message: 'Backend update failed.' }
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)
    if (backendAuthorityIsCurrent(authority)) {
      $backendUpdateApply.set({
        ...$backendUpdateApply.get(),
        applying: false,
        stage: 'error',
        error: 'apply-failed',
        message
      })
    }

    return { ok: false, error: 'apply-failed', message }
  }
}

export function applyBackendUpdate(): Promise<DesktopUpdateApplyResult> {
  const authority = activeBackendAuthority()

  if (!authority) {
    return Promise.resolve({ ok: false, error: 'unavailable', message: 'No remote backend is active.' })
  }

  const existing = backendUpdatesInFlight.get(authority.key)

  if (existing) {
    return existing
  }

  const update = runBackendUpdate(authority).finally(() => {
    if (backendUpdatesInFlight.get(authority.key) === update) {
      backendUpdatesInFlight.delete(authority.key)
    }
  })
  backendUpdatesInFlight.set(authority.key, update)

  return update
}

const backendRestartsInFlight = new Map<string, Promise<DesktopUpdateApplyResult>>()

async function runBackendGatewayRestart(authority: BackendAuthority): Promise<DesktopUpdateApplyResult> {
  const profile = $backendUpdateStatus.get()?.gatewayProfile?.trim()
  const scope = authority.scope
    ? { ...authority.scope, ...(profile ? { profile } : {}) }
    : profile
      ? { profile }
      : undefined

  if (backendAuthorityIsCurrent(authority)) {
    $backendUpdateApply.set({
      ...IDLE,
      applying: true,
      stage: 'restart',
      message: translateNow('updates.applyStatus.restartingSkewedGateway')
    })
  }

  try {
    const started = await restartGateway(scope)

    if (!started.ok) {
      throw new Error(started.message?.trim() || translateNow('updates.applyStatus.restartFailed'))
    }

    const expectedId = started.correlation_id?.trim() || started.action_id?.trim() || undefined
    const deadline = Date.now() + BACKEND_RETURN_MAX_MS

    while (Date.now() < deadline) {
      await new Promise(resolve => globalThis.setTimeout(resolve, BACKEND_ACTION_POLL_MS))

      let action: Awaited<ReturnType<typeof getActionStatus>>

      try {
        action = await getActionStatus(started.name, 2_000, scope)
      } catch {
        // The explicitly scoped route may disappear while its supervisor
        // replaces the gateway. Retry until it returns with current code.
        continue
      }

      if (action.running) {
        continue
      }

      if (expectedId && action.action_id?.trim() !== expectedId) {
        continue
      }

      if (action.exit_code !== null && action.exit_code !== 0) {
        throw new Error(action.lines.at(-1) || translateNow('updates.applyStatus.restartFailed'))
      }

      try {
        const runtime = await (scope ? getScopedStatus(scope) : getStatus())

        if (runtime.gateway_restart_required === true) {
          continue
        }

        const check = await checkBackendFor(authority, true)
        const status = mapBackendCheck(check, runtime)

        if (backendAuthorityIsCurrent(authority)) {
          $backendUpdateStatus.set(status)
          $backendUpdateApply.set(IDLE)
        }

        return { ok: true, message: translateNow('updates.applyStatus.restartComplete') }
      } catch {
        continue
      }
    }

    throw new Error(translateNow('updates.applyStatus.restartNoReturn'))
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error)

    if (backendAuthorityIsCurrent(authority)) {
      $backendUpdateApply.set({
        ...IDLE,
        applying: false,
        stage: 'error',
        error: 'gateway-restart-failed',
        message
      })
    }

    return { ok: false, error: 'gateway-restart-failed', message }
  }
}

/** Restart only the active backend gateway/profile when the backend explicitly
 * reports code skew. Human-readable messages never activate this path. */
export function restartBackendGatewayForSkew(): Promise<DesktopUpdateApplyResult> {
  const authority = activeBackendAuthority()

  if (!authority || $backendUpdateStatus.get()?.gatewayRestartRequired !== true) {
    return Promise.resolve({ ok: false, error: 'unavailable', message: 'No skewed backend gateway is active.' })
  }

  const key = `${authority.key}::${$backendUpdateStatus.get()?.gatewayProfile || 'default'}`
  const existing = backendRestartsInFlight.get(key)

  if (existing) {
    return existing
  }

  const restart = runBackendGatewayRestart(authority).finally(() => {
    if (backendRestartsInFlight.get(key) === restart) {
      backendRestartsInFlight.delete(key)
    }
  })
  backendRestartsInFlight.set(key, restart)

  return restart
}

// ── Update everything: the client + every registered backend in one action ──
//
// Remote-mode installs update on two (or more) clocks: the GUI app on this
// machine, the connected backend, and any other registered sources. Each has
// its own updater, and before this flow existed every remote-mode affordance
// targeted only the backend — so users "updated" and stayed on a stale GUI.
// This orchestration drives all of them:
//   1. The ACTIVE backend (remote mode) through the detailed-progress path.
//   2. Every OTHER eligible registered connection via the Electron fan-out
//      (cloud rows are platform-managed and report as skipped).
//   3. The local client LAST — its apply relaunches or hands off the app, so
//      it must not preempt the dispatches above.

const CLIENT_BEHIND_TOAST_ID = 'client-update-after-backend'

/** After a successful backend update, tell the user when the desktop app
 *  itself is still behind, with a one-click client update. Silent when the
 *  client is current, so aligned installs never see it. */
async function maybeNudgeClientAfterBackendUpdate(): Promise<void> {
  if (typeof window === 'undefined') {
    return
  }

  const status = (await checkUpdates().catch(() => null)) ?? $updateStatus.get()

  if (!status || status.error || (!status.updateAvailable && (status.behind ?? 0) <= 0)) {
    return
  }

  notify({
    action: {
      label: translateNow('updates.clientAlsoBehindAction'),
      onClick: () => {
        dismissNotification(CLIENT_BEHIND_TOAST_ID)
        $updateOverlayTarget.set('client')
        $updateOverlayOpen.set(true)
        void applyUpdates()
      }
    },
    durationMs: 0,
    id: CLIENT_BEHIND_TOAST_ID,
    kind: 'warning',
    message: translateNow('updates.clientAlsoBehindMessage'),
    title: translateNow('updates.clientAlsoBehindTitle')
  })
}

export interface UpdateEverythingState {
  running: boolean
}

export const $updateEverything = atom<UpdateEverythingState>({ running: false })

/** True when this install has more than one update target — a remote-mode
 *  window (backend + client) or a multi-connection registry. Gates the
 *  "Update everything" affordance so single-machine installs keep the
 *  one-button experience. */
export function hasMultipleUpdateTargets(): boolean {
  return isRemoteMode() || ($connectionsRegistry.get()?.connections.length ?? 0) > 1
}

let updateEverythingInFlight: Promise<void> | null = null

export function applyEverythingUpdate(): Promise<void> {
  if (updateEverythingInFlight) {
    return updateEverythingInFlight
  }

  updateEverythingInFlight = runEverythingUpdate().finally(() => {
    updateEverythingInFlight = null
  })

  return updateEverythingInFlight
}

async function runEverythingUpdate(): Promise<void> {
  $updateEverything.set({ running: true })

  try {
    // 1. Preflight every backend through the keyed fleet coordinator, then
    //    wait for one truthful terminal outcome per physical install. This
    //    replaces Electron's old fire-and-forget "dispatched" fan-out.
    const registry = $connectionsRegistry.get() ?? (await refreshConnectionsRegistry().catch(() => null))
    const registeredBackends = (registry?.connections ?? []).filter(connection => connection.kind !== 'local')
    let fleetResults: FleetUpdateResult[] = []

    if (registeredBackends.length > 0) {
      fleetResults = await applyFleetUpdates().catch(error => {
        notify({
          kind: 'warning',
          title: translateNow('updates.everythingFanoutFailedTitle'),
          message: error instanceof Error ? error.message : String(error)
        })

        return []
      })
    } else if (isRemoteMode()) {
      // Compatibility with a pre-registry Electron main: retain the detailed
      // active-backend path, which captures its legacy ambient route once.
      $updateOverlayTarget.set('backend')
      await applyBackendUpdate().catch(() => null)
    }

    for (const result of fleetResults) {
      const label =
        registeredBackends.find(connection => connection.id === result.connectionId)?.label ?? result.connectionId
      const title = translateNow('updates.everythingBackendTitle', label)

      if (result.outcome === 'success') {
        notify({ kind: 'success', title, message: translateNow('updates.everythingBackendUpdated') })
      } else if (result.outcome === 'restarted') {
        notify({ kind: 'success', title, message: translateNow('updates.everythingBackendRestarted') })
      } else if (result.outcome === 'partial') {
        notify({ kind: 'warning', title, message: translateNow('updates.everythingBackendPartial') })
      } else if (result.outcome === 'manual') {
        notify({
          kind: 'warning',
          title,
          message: result.command
            ? translateNow('updates.everythingBackendManualCommand', result.command)
            : result.message || translateNow('updates.everythingBackendManual')
        })
      } else if (result.outcome === 'managed') {
        notify({ title, message: translateNow('updates.everythingBackendManaged') })
      } else if (result.outcome === 'failed') {
        notify({
          kind: 'warning',
          title,
          message: result.message || translateNow('updates.everythingRowFailed')
        })
      }
    }

    // 2. The client last — its apply relaunches or hands off the app, so it
    //    must come after every dispatch above. Skipped when already current.
    const clientStatus = (await checkUpdates()) ?? $updateStatus.get()

    if ((clientStatus?.behind ?? 0) > 0 || clientStatus?.updateAvailable) {
      $updateOverlayTarget.set('client')
      $updateOverlayOpen.set(true)
      await applyUpdates()
    }
  } finally {
    $updateEverything.set({ running: false })
  }
}

function ingestProgress(payload: DesktopUpdateProgress): void {
  const current = $updateApply.get()
  const log = [...current.log, { stage: payload.stage, message: payload.message, at: payload.at }].slice(-50)

  const terminal =
    payload.stage === 'error' ||
    payload.stage === 'restart' ||
    payload.stage === 'manual' ||
    payload.stage === 'guiSkew'

  $updateApply.set({
    applying: !terminal,
    stage: payload.stage,
    message: payload.message,
    // Streamed log lines carry percent: null; keep the last milestone percent
    // (10/60/…) instead of resetting the bar to indeterminate on every line.
    percent: payload.percent ?? current.percent,
    error: payload.error,
    // 'manual' carries the command to run in its message field.
    command: payload.stage === 'manual' ? payload.message : current.command,
    log
  })
}

let pollerStarted = false
let backgroundTimer: ReturnType<typeof setInterval> | null = null
let lastFocusAt = 0
let connectionUnsub: (() => void) | null = null
let lastBackendAuthorityKey: string | null | undefined

/** Wire up background polling + progress streaming. Idempotent. */
export function startUpdatePoller(): void {
  if (pollerStarted || typeof window === 'undefined') {
    return
  }

  const bridge = window.hermesDesktop?.updates

  if (!bridge) {
    return
  }

  pollerStarted = true
  void checkUpdates()
  void checkBackendUpdates()
  void refreshDesktopVersion()
  bridge.onProgress(ingestProgress)

  // The poller starts at mount, before the gateway connects — so the first
  // backend check above sees mode≠remote and no-ops. Re-check once the
  // connection resolves to remote.
  lastBackendAuthorityKey = activeBackendAuthority()?.key ?? null
  connectionUnsub = $connection.subscribe(() => {
    const authorityKey = activeBackendAuthority()?.key ?? null

    if (authorityKey === lastBackendAuthorityKey) {
      return
    }

    lastBackendAuthorityKey = authorityKey

    if (authorityKey) {
      void checkBackendUpdates()
    }
  })

  window.addEventListener('focus', onFocus)
  backgroundTimer = setInterval(
    () => {
      void checkUpdates()
      void checkBackendUpdates()
    },
    30 * 60 * 1000
  )
}

export function stopUpdatePoller(): void {
  if (backgroundTimer !== null) {
    clearInterval(backgroundTimer)
    backgroundTimer = null
  }

  connectionUnsub?.()
  connectionUnsub = null
  lastBackendAuthorityKey = undefined
  window.removeEventListener('focus', onFocus)
  pollerStarted = false
}

function onFocus() {
  const now = Date.now()

  if (now - lastFocusAt < 5 * 60 * 1000) {
    return
  }

  lastFocusAt = now
  void checkUpdates()
  void checkBackendUpdates()
  void refreshDesktopVersion()
}
