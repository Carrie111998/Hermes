import { atom } from 'nanostores'

import type { DesktopConnectionKind, DesktopManagedConnectionUpdateResult, DesktopRegistryConnection } from '@/global'
import { checkHermesUpdate, getActionStatus, getScopedStatus, restartGateway, updateHermes } from '@/hermes'
import { translateNow } from '@/i18n'
import { $activeConnectionId, $connectionsRegistry, refreshConnectionsRegistry } from '@/store/connections'
import type { ActionResponse, BackendUpdateCheckResponse, StatusResponse, UpdateReceiptSummary } from '@/types/hermes'

export type FleetDeploymentKind =
  'cloud' | 'desktop' | 'external' | 'git-venv' | 'image' | 'launchd' | 'mutable' | 'package' | 'systemd' | 'unknown'
export type FleetUpdateAction = 'apply' | 'managed' | 'manual' | 'none' | 'restart' | 'retry'
export type FleetUpdateOutcome =
  | 'available'
  | 'checking'
  | 'current'
  | 'failed'
  | 'idle'
  | 'managed'
  | 'manual'
  | 'partial'
  | 'running'
  | 'restart-required'
  | 'restarted'
  | 'success'

export interface FleetUpdateRow {
  action: FleetUpdateAction
  /** null means the backend could not prove either current or behind. */
  availability: boolean | null
  /** Bot Mode context is display-only. It never creates additional mutation
   * targets: every action remains keyed/deduplicated by the install row. */
  botPlatforms?: string[]
  botProfiles?: string[]
  canApply: boolean
  checkedAt: number | null
  connectionId: string
  connectionKind: DesktopConnectionKind
  currentVersion: string | null
  deploymentKind: FleetDeploymentKind
  error: string | null
  generation: number
  gatewayProfile: string | null
  gatewayRestartRequired: boolean
  installId: string | null
  installMethod: string | null
  label: string
  message: string | null
  outcome: FleetUpdateOutcome
  updateCommand: string | null
}

export interface FleetUpdateResult {
  command?: string | null
  connectionId: string
  installId: string | null
  message?: string | null
  outcome: 'current' | 'failed' | 'managed' | 'manual' | 'partial' | 'restarted' | 'success'
}

export interface FleetRefreshOptions {
  /** Background refreshes leave inactive SSH sources alone. A user-clicked
   * refresh/update-all passes true and is the only path that dials them. */
  includeInactiveSsh?: boolean
  force?: boolean
}

export const $fleetUpdates = atom<Record<string, FleetUpdateRow>>({})
export const $fleetUpdatesRefreshing = atom(false)

const ACTION_POLL_MS = 1_500
const ACTION_MAX_MS = 6 * 60 * 1_000
const RETURN_MAX_MS = 4 * 60 * 1_000

let fleetGeneration = 0
const rowGenerations = new Map<string, number>()
const connectionMutations = new Map<string, Promise<FleetUpdateResult>>()
const mutations = new Map<string, Promise<FleetUpdateResult>>()

function nextRowGeneration(connectionId: string): number {
  const generation = (rowGenerations.get(connectionId) ?? 0) + 1
  rowGenerations.set(connectionId, generation)

  return generation
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error)
}

function fleetMessage(key: string, ...args: unknown[]): string {
  return translateNow(`settings.fleetUpdates.${key}`, ...args)
}

function exactUpdateCommand(check: BackendUpdateCheckResponse): string | null {
  const command = check.update_command?.trim()

  // Older managed-runtime backends used this prose sentinel in the command
  // field. It is guidance, not something a user can execute or safely copy.
  return command && command.toLowerCase() !== 'managed outside dashboard' ? command : null
}

function connectionScope(
  connection: DesktopRegistryConnection,
  profile?: string | null
): { connectionId: string; profile?: string } {
  const normalizedProfile = profile?.trim()

  return { connectionId: connection.id, ...(normalizedProfile ? { profile: normalizedProfile } : {}) }
}

function currentConnection(connectionId: string): DesktopRegistryConnection | null {
  return $connectionsRegistry.get()?.connections.find(connection => connection.id === connectionId) ?? null
}

function blankRow(connection: DesktopRegistryConnection, generation: number): FleetUpdateRow {
  return {
    action: 'none',
    availability: null,
    botPlatforms: undefined,
    botProfiles: undefined,
    canApply: false,
    checkedAt: null,
    connectionId: connection.id,
    connectionKind: connection.kind,
    currentVersion: null,
    deploymentKind: connection.kind === 'cloud' ? 'cloud' : 'unknown',
    error: null,
    generation,
    gatewayProfile: null,
    gatewayRestartRequired: false,
    installId: connection.installId?.trim() || null,
    installMethod: null,
    label: connection.label,
    message: null,
    outcome: 'idle',
    updateCommand: null
  }
}

export function fleetAvailability(check: BackendUpdateCheckResponse): boolean | null {
  if (check.update_available === true) {
    return true
  }

  if (typeof check.behind === 'number') {
    return check.behind > 0
  }

  return null
}

export function fleetDeploymentKind(
  connectionKind: DesktopConnectionKind,
  check: Pick<BackendUpdateCheckResponse, 'can_apply' | 'deployment_class' | 'deployment_kind' | 'install_method'>
): FleetDeploymentKind {
  if (connectionKind === 'cloud') {
    return 'cloud'
  }

  const declared = String(check.deployment_kind ?? '')
    .trim()
    .toLowerCase()

  if (
    declared === 'desktop' ||
    declared === 'external' ||
    declared === 'git-venv' ||
    declared === 'image' ||
    declared === 'launchd' ||
    declared === 'mutable' ||
    declared === 'package' ||
    declared === 'systemd' ||
    declared === 'unknown'
  ) {
    return declared
  }

  const declaredClass = String(check.deployment_class ?? '')
    .trim()
    .toLowerCase()

  if (
    declaredClass === 'mutable' ||
    declaredClass === 'package' ||
    declaredClass === 'image' ||
    declaredClass === 'external'
  ) {
    return declaredClass
  }

  const method = String(check.install_method ?? '')
    .trim()
    .toLowerCase()

  if (method.includes('docker') || method.includes('container') || method.includes('image')) {
    return 'image'
  }

  if (/(?:apt|brew|deb|nix|package|pipx|rpm|snap|uv)/.test(method)) {
    return 'package'
  }

  if (method === 'git' || check.can_apply) {
    return 'mutable'
  }

  return method ? 'external' : 'unknown'
}

function managedSshUpdater(): ((connectionId: string) => Promise<DesktopManagedConnectionUpdateResult>) | null {
  if (typeof window === 'undefined') {
    return null
  }

  return window.hermesDesktop?.connections?.updateManaged ?? null
}

function actionFor(
  connectionKind: DesktopConnectionKind,
  check: BackendUpdateCheckResponse,
  availability: boolean | null
): FleetUpdateAction {
  if (connectionKind === 'cloud') {
    return 'managed'
  }

  const deploymentKind = fleetDeploymentKind(connectionKind, check)

  if (availability === false) {
    return 'none'
  }

  // A source check that could not establish current-vs-behind must stay
  // retryable. `can_apply` describes the install shape, not the availability
  // of a newer revision, so it must never turn "unknown" into an Update button.
  if (availability === null) {
    // Image/package/external runtimes are operator-managed regardless of
    // whether Hermes can compare their upstream version. Their exact command
    // is remediation, not a claim that an in-place update is available.
    if (deploymentKind === 'image' || deploymentKind === 'package' || deploymentKind === 'external') {
      return exactUpdateCommand(check) ? 'manual' : 'managed'
    }

    return 'retry'
  }

  if (check.can_apply) {
    return 'apply'
  }

  if (exactUpdateCommand(check)) {
    return 'manual'
  }

  if (deploymentKind === 'image' || deploymentKind === 'package' || deploymentKind === 'external') {
    return 'managed'
  }

  return 'retry'
}

async function probeConnection(
  connection: DesktopRegistryConnection,
  force: boolean,
  profile?: string | null
): Promise<FleetUpdateRow> {
  const generation = rowGenerations.get(connection.id) ?? nextRowGeneration(connection.id)
  const scope = connectionScope(connection, profile)
  const [statusResult, checkResult] = await Promise.allSettled([
    getScopedStatus(scope),
    checkHermesUpdate(force, scope)
  ])

  const status: StatusResponse | null = statusResult.status === 'fulfilled' ? statusResult.value : null
  const check: BackendUpdateCheckResponse | null = checkResult.status === 'fulfilled' ? checkResult.value : null

  if (!status && !check) {
    if (connection.kind === 'cloud') {
      return {
        ...blankRow(connection, generation),
        action: 'managed',
        checkedAt: Date.now(),
        outcome: 'managed'
      }
    }

    const failures = [statusResult, checkResult]
      .filter((result): result is PromiseRejectedResult => result.status === 'rejected')
      .map(result => errorMessage(result.reason))

    return {
      ...blankRow(connection, generation),
      action: 'retry',
      checkedAt: Date.now(),
      error: failures.join('\n') || fleetMessage('checkFailed'),
      message: failures.at(-1) ?? null,
      outcome: 'failed'
    }
  }

  if (!check) {
    const restartRequired = connection.kind !== 'cloud' && status?.gateway_restart_required === true

    return {
      ...blankRow(connection, generation),
      action: restartRequired ? 'restart' : connection.kind === 'cloud' ? 'managed' : 'retry',
      checkedAt: Date.now(),
      currentVersion: status?.version ?? null,
      error: checkResult.status === 'rejected' ? errorMessage(checkResult.reason) : fleetMessage('checkUnavailable'),
      gatewayProfile: status?.gateway_profile?.trim() || null,
      gatewayRestartRequired: restartRequired,
      installId: status?.install_id?.trim() || connection.installId?.trim() || null,
      message: checkResult.status === 'rejected' ? errorMessage(checkResult.reason) : null,
      outcome: restartRequired ? 'restart-required' : connection.kind === 'cloud' ? 'managed' : 'failed'
    }
  }

  const availability = fleetAvailability(check)
  const restartRequired = connection.kind !== 'cloud' && status?.gateway_restart_required === true
  // A Desktop SSH source is a long-lived `hermes serve` process owned by the
  // SSH lifecycle. It may only be mutated through Electron's transactional
  // drain/update/restore bridge. Older desktop clients without that bridge
  // remain fail-closed with the backend's exact manual command.
  const sshOwnedServe = connection.kind === 'ssh'
  const sshManaged = sshOwnedServe && Boolean(managedSshUpdater())
  const effectiveCheck: BackendUpdateCheckResponse =
    sshOwnedServe && !sshManaged ? { ...check, can_apply: false } : check
  const action = restartRequired ? 'restart' : actionFor(connection.kind, effectiveCheck, availability)
  const sshManual = sshOwnedServe && !sshManaged && availability === true && !restartRequired
  const updateCommand = sshManual ? exactUpdateCommand(check) || 'hermes update' : exactUpdateCommand(check)

  return {
    action: sshManual ? 'manual' : action,
    availability,
    botPlatforms: status
      ? [
          ...new Set(
            Object.keys(status.gateway_platforms ?? {})
              .map(platform => platform.split(':').at(-1)?.toLowerCase())
              .filter(
                (platform): platform is 'discord' | 'telegram' => platform === 'discord' || platform === 'telegram'
              )
          )
        ]
      : undefined,
    canApply: effectiveCheck.can_apply,
    checkedAt: Date.now(),
    connectionId: connection.id,
    connectionKind: connection.kind,
    currentVersion: status?.version || check.current_version || null,
    deploymentKind: fleetDeploymentKind(connection.kind, check),
    error: null,
    generation,
    gatewayProfile: status?.gateway_profile?.trim() || null,
    gatewayRestartRequired: restartRequired,
    installId: status?.install_id?.trim() || connection.installId?.trim() || null,
    installMethod: check.install_method || null,
    label: connection.label,
    message: sshManual ? fleetMessage('sshOwnedServe', connection.label) : (check.message ?? null),
    outcome:
      action === 'restart'
        ? 'restart-required'
        : action === 'managed'
          ? 'managed'
          : sshManual || action === 'manual'
            ? 'manual'
            : availability === true
              ? 'available'
              : availability === false
                ? 'current'
                : 'idle',
    updateCommand
  }
}

function publishRow(row: FleetUpdateRow, epoch: number, expectedRowGeneration: number): boolean {
  if (
    epoch !== fleetGeneration ||
    rowGenerations.get(row.connectionId) !== expectedRowGeneration ||
    !currentConnection(row.connectionId)
  ) {
    return false
  }

  $fleetUpdates.set({ ...$fleetUpdates.get(), [row.connectionId]: row })

  return true
}

/** Refresh every registered backend. Local is represented by the separate
 * Desktop client update row; including it here would mutate one install twice. */
export async function refreshFleetUpdates(options: FleetRefreshOptions = {}): Promise<Record<string, FleetUpdateRow>> {
  // Claim the refresh before the first await. A slower, earlier roster or
  // registry read must not become "newer" merely because it resumed last.
  const epoch = ++fleetGeneration
  const registry = $connectionsRegistry.get() ?? (await refreshConnectionsRegistry().catch(() => null))
  const connections = (registry?.connections ?? []).filter(connection => connection.kind !== 'local')

  if (epoch !== fleetGeneration) {
    return $fleetUpdates.get()
  }

  if (connections.length === 0) {
    $fleetUpdates.set({})
    $fleetUpdatesRefreshing.set(false)

    return {}
  }

  const roster = await window.hermesDesktop?.getAgentRoster?.().catch(() => null)
  const botProfiles = new Map<string, string[]>()

  if (epoch !== fleetGeneration) {
    return $fleetUpdates.get()
  }

  for (const agent of roster?.agents ?? []) {
    const profiles = botProfiles.get(agent.connectionId) ?? []

    if (!profiles.includes(agent.profile)) {
      profiles.push(agent.profile)
    }

    botProfiles.set(agent.connectionId, profiles)
  }
  const activeId = $activeConnectionId.get()
  const previousRows = $fleetUpdates.get()
  const seeded: Record<string, FleetUpdateRow> = {}

  for (const connection of connections) {
    const generation = nextRowGeneration(connection.id)
    const inactiveSsh = connection.kind === 'ssh' && connection.id !== activeId && options.includeInactiveSsh !== true
    const previous = previousRows[connection.id]
    const row =
      inactiveSsh && previous
        ? {
            ...previous,
            connectionKind: connection.kind,
            generation,
            label: connection.label
          }
        : blankRow(connection, generation)
    row.botProfiles = botProfiles.get(connection.id)

    if (inactiveSsh) {
      if (!previous) {
        row.action = 'retry'
      }
    } else {
      row.outcome = 'checking'
    }

    seeded[connection.id] = row
  }

  $fleetUpdates.set(seeded)
  $fleetUpdatesRefreshing.set(true)

  await Promise.all(
    connections.map(async connection => {
      const generation = rowGenerations.get(connection.id)!

      if (connection.kind === 'ssh' && connection.id !== activeId && options.includeInactiveSsh !== true) {
        return
      }

      const row = await probeConnection(connection, options.force === true)
      publishRow({ ...row, botProfiles: botProfiles.get(connection.id), generation }, epoch, generation)
    })
  )

  if (epoch === fleetGeneration) {
    $fleetUpdatesRefreshing.set(false)
  }

  return $fleetUpdates.get()
}

export async function refreshFleetConnection(connectionId: string, force = true): Promise<FleetUpdateRow | null> {
  const connection = currentConnection(connectionId)

  if (!connection || connection.kind === 'local') {
    return null
  }

  const epoch = fleetGeneration
  const generation = nextRowGeneration(connectionId)
  const previous = $fleetUpdates.get()[connectionId] ?? blankRow(connection, generation)
  $fleetUpdates.set({
    ...$fleetUpdates.get(),
    [connectionId]: { ...previous, error: null, generation, outcome: 'checking' }
  })

  const row = { ...(await probeConnection(connection, force)), generation }
  publishRow(row, epoch, generation)

  return row
}

function receiptOutcome(
  receipt: UpdateReceiptSummary | undefined,
  expectedCorrelationId: string | undefined,
  actionId: string | null | undefined,
  startedAtMs: number
): UpdateReceiptSummary | null {
  if (!receipt || !receipt.finished_at || receipt.outcome === 'running') {
    return null
  }

  if (expectedCorrelationId) {
    if (receipt.correlation_id) {
      return receipt.correlation_id === expectedCorrelationId ? receipt : null
    }

    // Older receipts had no correlation field. Keep that compatibility rung
    // only when the action status itself is pinned to the action we started;
    // time proximity alone can otherwise attribute another fleet run's latest
    // receipt to this request.
    if (actionId?.trim() !== expectedCorrelationId) {
      return null
    }
  }

  // Compatibility with pre-correlation backends: only accept a receipt whose
  // own start time overlaps this invocation (60s absorbs clock skew).
  const receiptStartedAt = Date.parse(receipt.started_at ?? '')

  return Number.isFinite(receiptStartedAt) && receiptStartedAt >= startedAtMs - 60_000 ? receipt : null
}

function receiptCommand(receipt: UpdateReceiptSummary | null, fallback: string | null): string | null {
  return receipt?.refusal?.update_command?.trim() || fallback
}

function mutationKey(row: FleetUpdateRow): string {
  if (row.action === 'restart') {
    return `restart:${row.installId || row.connectionId}:${row.gatewayProfile || 'default'}`
  }

  return row.installId ? `install:${row.installId}` : `connection:${row.connectionId}`
}

function updateMutationRows(
  connectionId: string,
  installId: string | null,
  patch: Partial<FleetUpdateRow>,
  epoch: number,
  options: { gatewayProfile?: string | null } = {}
): void {
  if (epoch !== fleetGeneration) {
    return
  }

  const next = { ...$fleetUpdates.get() }
  const {
    botProfiles: _botProfiles,
    connectionId: _connectionId,
    connectionKind: _connectionKind,
    generation: _generation,
    label: _label,
    ...sharedPatch
  } = patch
  const profileScoped = Object.hasOwn(options, 'gatewayProfile')
  const scopedProfile = options.gatewayProfile?.trim() || null

  for (const [id, row] of Object.entries(next)) {
    const sameMutation =
      Boolean(installId && row.installId === installId) &&
      (!profileScoped || (row.gatewayProfile?.trim() || null) === scopedProfile)

    if (id === connectionId || sameMutation) {
      next[id] = { ...row, ...sharedPatch }
    }
  }

  $fleetUpdates.set(next)
}

function resultFromRow(row: FleetUpdateRow): FleetUpdateResult {
  const outcome =
    row.outcome === 'success' ||
    row.outcome === 'partial' ||
    row.outcome === 'manual' ||
    row.outcome === 'managed' ||
    row.outcome === 'restarted' ||
    row.outcome === 'current'
      ? row.outcome
      : 'failed'

  return {
    command: row.updateCommand,
    connectionId: row.connectionId,
    installId: row.installId,
    message: row.message,
    outcome
  }
}

async function runGatewayRestart(
  connection: DesktopRegistryConnection,
  row: FleetUpdateRow,
  epoch: number
): Promise<FleetUpdateResult> {
  const profile = row.gatewayProfile?.trim()
  const scope = { connectionId: connection.id, ...(profile ? { profile } : {}) }
  const updateRestartRows = (patch: Partial<FleetUpdateRow>) =>
    updateMutationRows(connection.id, row.installId, patch, epoch, { gatewayProfile: profile || null })
  updateRestartRows({ action: 'none', error: null, message: null, outcome: 'running' })

  try {
    const started = await restartGateway(scope)

    if (!started.ok) {
      const message = started.message?.trim() || fleetMessage('restartRefused')
      updateRestartRows({ action: 'restart', error: message, message, outcome: 'failed' })

      return { connectionId: connection.id, installId: row.installId, message, outcome: 'failed' }
    }

    const expectedId = started.correlation_id?.trim() || started.action_id?.trim() || undefined
    const deadline = Date.now() + RETURN_MAX_MS

    while (Date.now() < deadline) {
      await new Promise(resolve => globalThis.setTimeout(resolve, ACTION_POLL_MS))

      try {
        const status = await getActionStatus(started.name, 2_000, scope)

        if (status.running) {
          continue
        }

        if (expectedId && status.action_id?.trim() !== expectedId) {
          continue
        }

        if (status.exit_code !== null && status.exit_code !== 0) {
          const message = status.lines.at(-1) || fleetMessage('restartFailed')
          updateRestartRows({ action: 'restart', error: message, message, outcome: 'failed' })

          return { connectionId: connection.id, installId: row.installId, message, outcome: 'failed' }
        }

        const refreshed = await probeConnection(connection, true, profile)

        if (refreshed.gatewayRestartRequired) {
          continue
        }

        const settled = { ...refreshed, action: refreshed.action, outcome: 'restarted' as const }
        updateRestartRows(settled)

        return { connectionId: connection.id, installId: row.installId, outcome: 'restarted' }
      } catch {
        // A profile-scoped gateway is expected to disappear briefly while its
        // supervisor replaces it. Keep polling its explicitly pinned route.
      }
    }
  } catch (error) {
    const message = errorMessage(error)
    updateRestartRows({ action: 'restart', error: message, message, outcome: 'failed' })

    return { connectionId: connection.id, installId: row.installId, message, outcome: 'failed' }
  }

  const message = fleetMessage('restartNoReturn')
  updateRestartRows({ action: 'restart', error: message, message, outcome: 'failed' })

  return { connectionId: connection.id, installId: row.installId, message, outcome: 'failed' }
}

async function runFleetMutation(
  connection: DesktopRegistryConnection,
  initial: FleetUpdateRow,
  ownMutation: () => Promise<FleetUpdateResult>
): Promise<FleetUpdateResult> {
  const epoch = fleetGeneration
  const preflight = await probeConnection(connection, true)
  const installId = preflight.installId ?? initial.installId
  updateMutationRows(
    connection.id,
    installId,
    preflight,
    epoch,
    preflight.gatewayRestartRequired ? { gatewayProfile: preflight.gatewayProfile } : undefined
  )

  // The stored/initial identity is only a hint. Re-key under the authoritative
  // preflight identity before any mutation, then join a sibling alias that got
  // there first. JavaScript resumes these continuations serially, so this is an
  // atomic claim without a second lock.
  const authoritativeKey = mutationKey({ ...preflight, installId })
  const authoritativeMutation = mutations.get(authoritativeKey)

  if (authoritativeMutation && authoritativeMutation !== ownMutation()) {
    return authoritativeMutation
  }

  mutations.set(authoritativeKey, ownMutation())

  if (preflight.gatewayRestartRequired) {
    return runGatewayRestart(connection, { ...preflight, installId }, epoch)
  }

  if (preflight.action === 'managed') {
    return resultFromRow({ ...preflight, outcome: 'managed' })
  }

  if (preflight.availability === false) {
    return resultFromRow({ ...preflight, action: 'none', outcome: 'current' })
  }

  if (preflight.availability === null) {
    if (preflight.action === 'manual' && preflight.updateCommand) {
      const settled: FleetUpdateRow = {
        ...preflight,
        action: 'manual',
        error: null,
        outcome: 'manual'
      }
      updateMutationRows(connection.id, installId, settled, epoch)

      return resultFromRow(settled)
    }

    const message = preflight.message || fleetMessage('availabilityUnknown')
    const settled: FleetUpdateRow = {
      ...preflight,
      action: 'retry',
      error: message,
      message,
      outcome: 'failed'
    }
    updateMutationRows(connection.id, installId, settled, epoch)

    return resultFromRow(settled)
  }

  const updateManaged = preflight.connectionKind === 'ssh' ? managedSshUpdater() : null

  if (updateManaged && preflight.canApply) {
    updateMutationRows(
      connection.id,
      installId,
      { action: 'none', error: null, message: preflight.message, outcome: 'running' },
      epoch
    )

    let managed: DesktopManagedConnectionUpdateResult

    try {
      managed = await updateManaged(connection.id)
    } catch (error) {
      const message = errorMessage(error)
      updateMutationRows(
        connection.id,
        installId,
        { action: 'retry', error: message, message, outcome: 'failed' },
        epoch
      )

      return { connectionId: connection.id, installId, message, outcome: 'failed' }
    }

    const message = managed.message || managed.error || managed.receipt?.stopReason || null

    if (managed.ok) {
      updateMutationRows(connection.id, installId, { action: 'none', error: null, message, outcome: 'success' }, epoch)

      return { connectionId: connection.id, installId, message, outcome: 'success' }
    }

    if (managed.updateOk && !managed.restoreOk) {
      updateMutationRows(
        connection.id,
        installId,
        { action: 'none', error: managed.error || message, message, outcome: 'partial' },
        epoch
      )

      return { connectionId: connection.id, installId, message, outcome: 'partial' }
    }

    const failure = message || fleetMessage('updateFailed')
    updateMutationRows(
      connection.id,
      installId,
      { action: 'retry', error: failure, message: failure, outcome: 'failed' },
      epoch
    )

    return { connectionId: connection.id, installId, message: failure, outcome: 'failed' }
  }

  if (!preflight.canApply) {
    const manual = Boolean(preflight.updateCommand)
    const settled: FleetUpdateRow = {
      ...preflight,
      action: manual ? 'manual' : 'retry',
      error: manual ? null : preflight.message || fleetMessage('cannotApply'),
      outcome: manual ? 'manual' : 'failed'
    }
    updateMutationRows(connection.id, installId, settled, epoch)

    return resultFromRow(settled)
  }

  updateMutationRows(
    connection.id,
    installId,
    { action: 'none', error: null, message: preflight.message, outcome: 'running' },
    epoch
  )

  let started: ActionResponse

  try {
    started = await updateHermes(connectionScope(connection))
  } catch (error) {
    const message = errorMessage(error)
    updateMutationRows(connection.id, installId, { action: 'retry', error: message, message, outcome: 'failed' }, epoch)

    return { connectionId: connection.id, installId, message, outcome: 'failed' }
  }

  if (!started.ok) {
    const command = started.refusal?.update_command?.trim() || started.update_command?.trim() || preflight.updateCommand
    const message = started.refusal?.message?.trim() || started.message?.trim() || preflight.message
    const manual = Boolean(command)
    updateMutationRows(
      connection.id,
      installId,
      {
        action: manual ? 'manual' : 'retry',
        error: manual ? null : message || fleetMessage('updateRefused'),
        message,
        outcome: manual ? 'manual' : 'failed',
        updateCommand: command
      },
      epoch
    )

    return { command, connectionId: connection.id, installId, message, outcome: manual ? 'manual' : 'failed' }
  }

  const applyStartedAt = Date.now()
  const expectedCorrelationId = started.correlation_id?.trim() || started.action_id?.trim() || undefined
  const actionDeadline = applyStartedAt + ACTION_MAX_MS
  let deadline = actionDeadline
  let reconnecting = false

  while (Date.now() < deadline) {
    try {
      const status = await getActionStatus(started.name, 2_000, connectionScope(connection))

      if (status.running) {
        if (reconnecting) {
          reconnecting = false
          deadline = actionDeadline
        }
      } else {
        const receipt = receiptOutcome(status.receipt, expectedCorrelationId, status.action_id, applyStartedAt)

        if (receipt) {
          if (receipt.outcome === 'success' || receipt.outcome === 'partial') {
            const outcome = receipt.outcome
            updateMutationRows(connection.id, installId, { action: 'none', error: null, outcome }, epoch)

            return { connectionId: connection.id, installId, outcome }
          }

          if (receipt.outcome === 'refused') {
            const command = receiptCommand(receipt, preflight.updateCommand)
            const message = receipt.refusal?.message?.trim() || preflight.message
            const manual = Boolean(command)
            updateMutationRows(
              connection.id,
              installId,
              {
                action: manual ? 'manual' : 'retry',
                error: manual ? null : message || fleetMessage('updateRefused'),
                message,
                outcome: manual ? 'manual' : 'failed',
                updateCommand: command
              },
              epoch
            )

            return {
              command,
              connectionId: connection.id,
              installId,
              message,
              outcome: manual ? 'manual' : 'failed'
            }
          }

          const message = receipt.stop_reason || fleetMessage('updateFailed')
          updateMutationRows(
            connection.id,
            installId,
            { action: 'retry', error: message, message, outcome: 'failed' },
            epoch
          )

          return { connectionId: connection.id, installId, message, outcome: 'failed' }
        }

        const completedMarker =
          Boolean(started.action_id) &&
          status.lines.some(line => line === `=== hermes-update completed ${started.action_id} ===`)

        const statusMatchesStarted = !expectedCorrelationId || status.action_id?.trim() === expectedCorrelationId

        if (statusMatchesStarted && (status.exit_code === 0 || (status.exit_code === null && completedMarker))) {
          updateMutationRows(connection.id, installId, { action: 'none', error: null, outcome: 'success' }, epoch)

          return { connectionId: connection.id, installId, outcome: 'success' }
        }

        if (expectedCorrelationId && !statusMatchesStarted) {
          await new Promise(resolve => globalThis.setTimeout(resolve, ACTION_POLL_MS))
          continue
        }

        if (!started.action_id && status.exit_code === null) {
          const legacy = await checkHermesUpdate(true, connectionScope(connection))

          if (legacy.behind === 0 || legacy.current_version !== preflight.currentVersion) {
            updateMutationRows(connection.id, installId, { action: 'none', error: null, outcome: 'success' }, epoch)

            return { connectionId: connection.id, installId, outcome: 'success' }
          }
        }

        if (status.exit_code !== null) {
          const message = status.lines.at(-1) || fleetMessage('updateFailed')
          updateMutationRows(
            connection.id,
            installId,
            { action: 'retry', error: message, message, outcome: 'failed' },
            epoch
          )

          return { connectionId: connection.id, installId, message, outcome: 'failed' }
        }
      }
    } catch {
      if (!reconnecting) {
        reconnecting = true
        deadline = Date.now() + RETURN_MAX_MS
      }
    }

    await new Promise(resolve => globalThis.setTimeout(resolve, ACTION_POLL_MS))
  }

  const message = fleetMessage('updateNoReturn')
  updateMutationRows(connection.id, installId, { action: 'retry', error: message, message, outcome: 'failed' }, epoch)

  return { connectionId: connection.id, installId, message, outcome: 'failed' }
}

/** Apply one backend update. Aliases that share install_id join the same
 * mutation promise, so one physical checkout is never updated twice. */
export function applyFleetUpdate(connectionId: string): Promise<FleetUpdateResult> {
  const connection = currentConnection(connectionId)
  const row = $fleetUpdates.get()[connectionId]

  if (!connection || connection.kind === 'local' || !row) {
    return Promise.resolve({ connectionId, installId: row?.installId ?? null, outcome: 'failed' })
  }

  // A repeat click on the same row joins immediately. Cross-connection aliases
  // do not join until both have re-probed their authoritative install_id; a
  // remembered ID may be stale after a reinstall or registry edit.
  const existing = connectionMutations.get(connectionId)

  if (existing) {
    return existing
  }

  let mutation!: Promise<FleetUpdateResult>
  mutation = runFleetMutation(connection, row, () => mutation).finally(() => {
    if (connectionMutations.get(connectionId) === mutation) {
      connectionMutations.delete(connectionId)
    }

    for (const [mutationKey, value] of mutations) {
      if (value === mutation) {
        mutations.delete(mutationKey)
      }
    }
  })

  connectionMutations.set(connectionId, mutation)

  return mutation
}

/** User-invoked fleet apply: explicit refresh first, one terminal result per
 * physical install, and no optimistic "dispatched" success. */
export async function applyFleetUpdates(): Promise<FleetUpdateResult[]> {
  const rows = await refreshFleetUpdates({ force: true, includeInactiveSsh: true })
  // Every connection must reach runFleetMutation's authoritative preflight.
  // A remembered install_id is only a hint: two connections that used to be
  // aliases may now point at different installs. Dedupe the terminal results
  // after those preflights have re-keyed/joined the actual mutations.
  const settled = await Promise.all(Object.values(rows).map(row => applyFleetUpdate(row.connectionId)))
  const seen = new Set<string>()
  const results: FleetUpdateResult[] = []

  for (const result of settled) {
    const row = $fleetUpdates.get()[result.connectionId]
    const key = row
      ? mutationKey({ ...row, installId: result.installId ?? row.installId })
      : result.installId
        ? `install:${result.installId}`
        : `connection:${result.connectionId}`

    if (seen.has(key)) {
      continue
    }

    seen.add(key)
    results.push(result)
  }

  return results
}

/** @internal */
export function _resetFleetUpdatesForTests(): void {
  fleetGeneration = 0
  rowGenerations.clear()
  connectionMutations.clear()
  mutations.clear()
  $fleetUpdates.set({})
  $fleetUpdatesRefreshing.set(false)
}
