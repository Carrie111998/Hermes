import { beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionsRegistry } from '@/global'
import type { BackendUpdateCheckResponse } from '@/types/hermes'

const { atom } = await import('nanostores')
const $registry = atom<DesktopConnectionsRegistry | null>(null)
const $activeConnectionId = atom<null | string>('local')

vi.mock('@/store/connections', () => ({
  $activeConnectionId,
  $connectionsRegistry: $registry,
  refreshConnectionsRegistry: () => Promise.resolve($registry.get())
}))

const getScopedStatus = vi.fn()
const checkHermesUpdate = vi.fn()
const updateHermes = vi.fn()
const getActionStatus = vi.fn()
const restartGateway = vi.fn()

vi.mock('@/hermes', () => ({
  checkHermesUpdate: (...args: unknown[]) => checkHermesUpdate(...args),
  getActionStatus: (...args: unknown[]) => getActionStatus(...args),
  getScopedStatus: (...args: unknown[]) => getScopedStatus(...args),
  restartGateway: (...args: unknown[]) => restartGateway(...args),
  updateHermes: (...args: unknown[]) => updateHermes(...args)
}))

const {
  $fleetUpdates,
  _resetFleetUpdatesForTests,
  applyFleetUpdate,
  applyFleetUpdates,
  fleetAvailability,
  fleetDeploymentKind,
  refreshFleetUpdates
} = await import('./fleet-updates')

function registry(
  connections: Array<{ id: string; installId?: string; kind?: 'cloud' | 'local' | 'remote' | 'ssh'; label?: string }>
): DesktopConnectionsRegistry {
  return {
    connections: connections.map(connection => ({
      id: connection.id,
      installId: connection.installId,
      kind: connection.kind ?? 'remote',
      label: connection.label ?? connection.id,
      tokenPreview: null,
      tokenSet: false
    })),
    primary: 'local',
    secureTokenStorage: true,
    version: 2
  }
}

function status(version: string, installId: string) {
  return {
    active_sessions: 0,
    config_path: '',
    config_version: 1,
    env_path: '',
    gateway_exit_reason: null,
    gateway_health_url: null,
    gateway_pid: 1,
    gateway_platforms: {},
    gateway_running: true,
    gateway_state: 'running',
    gateway_updated_at: null,
    hermes_home: '',
    install_id: installId,
    latest_config_version: 1,
    release_date: '',
    version
  }
}

function check(over: Partial<BackendUpdateCheckResponse> = {}): BackendUpdateCheckResponse {
  return {
    behind: 2,
    can_apply: true,
    current_version: '0.9.0',
    install_method: 'git',
    message: null,
    update_available: true,
    update_command: null,
    ...over
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>(next => {
    resolve = next
  })

  return { promise, resolve }
}

beforeEach(() => {
  _resetFleetUpdatesForTests()
  $activeConnectionId.set('local')
  $registry.set(registry([{ id: 'local', kind: 'local' }]))
  getScopedStatus.mockReset().mockResolvedValue(status('0.9.0', 'install-a'))
  checkHermesUpdate.mockReset().mockResolvedValue(check())
  updateHermes.mockReset().mockResolvedValue({ action_id: 'action-a', name: 'hermes-update', ok: true, pid: 12 })
  restartGateway.mockReset().mockResolvedValue({ action_id: 'restart-a', name: 'gateway-restart', ok: true, pid: 12 })
  getActionStatus.mockReset().mockResolvedValue({
    action_id: 'action-a',
    exit_code: 0,
    lines: [],
    name: 'hermes-update',
    pid: null,
    running: false
  })
  ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
    api: vi.fn(),
    getAgentRoster: vi.fn().mockResolvedValue({ agents: [], sources: [] })
  }
})

describe('fleet update classification', () => {
  it('keeps availability tri-state and preserves old install_method fallbacks', () => {
    expect(fleetAvailability(check({ behind: null, update_available: false }))).toBeNull()
    expect(fleetAvailability(check({ behind: 0, update_available: false }))).toBe(false)
    expect(fleetAvailability(check({ behind: null, update_available: true }))).toBe(true)

    expect(fleetDeploymentKind('remote', check({ deployment_kind: 'image', install_method: 'git' }))).toBe('image')
    expect(fleetDeploymentKind('remote', check({ deployment_kind: 'systemd', install_method: 'git' }))).toBe('systemd')
    expect(fleetDeploymentKind('remote', check({ deployment_kind: 'launchd', install_method: 'git' }))).toBe('launchd')
    expect(fleetDeploymentKind('remote', check({ deployment_kind: 'desktop', install_method: 'git' }))).toBe('desktop')
    expect(fleetDeploymentKind('remote', check({ deployment_kind: 'git-venv', install_method: 'git' }))).toBe(
      'git-venv'
    )
    expect(fleetDeploymentKind('remote', check({ deployment_class: 'mutable', install_method: '' }))).toBe('mutable')
    expect(fleetDeploymentKind('remote', check({ can_apply: false, install_method: 'docker' }))).toBe('image')
    expect(fleetDeploymentKind('remote', check({ can_apply: false, install_method: 'apt' }))).toBe('package')
    expect(fleetDeploymentKind('remote', check({ install_method: 'git' }))).toBe('mutable')
    expect(
      fleetDeploymentKind('remote', check({ can_apply: false, deployment_kind: 'external', install_method: 'git' }))
    ).toBe('external')
    expect(fleetDeploymentKind('cloud', check())).toBe('cloud')
  })
})

describe('refreshFleetUpdates', () => {
  it('does not dial an inactive SSH source until an explicit refresh', async () => {
    $registry.set(
      registry([
        { id: 'local', kind: 'local' },
        { id: 'ssh-a', kind: 'ssh' }
      ])
    )

    await refreshFleetUpdates()

    expect(getScopedStatus).not.toHaveBeenCalled()
    expect(checkHermesUpdate).not.toHaveBeenCalled()
    expect($fleetUpdates.get()['ssh-a']).toMatchObject({ action: 'retry', outcome: 'idle' })

    await refreshFleetUpdates({ force: true, includeInactiveSsh: true })

    expect(getScopedStatus).toHaveBeenCalledWith({ connectionId: 'ssh-a' })
    expect(checkHermesUpdate).toHaveBeenCalledWith(true, { connectionId: 'ssh-a' })

    getScopedStatus.mockClear()
    checkHermesUpdate.mockClear()
    await refreshFleetUpdates()

    expect(getScopedStatus).not.toHaveBeenCalled()
    expect(checkHermesUpdate).not.toHaveBeenCalled()
    expect($fleetUpdates.get()['ssh-a']).toMatchObject({ currentVersion: '0.9.0', installId: 'install-a' })
  })

  it('clears removed backend rows when the registry becomes local-only', async () => {
    $registry.set(registry([{ id: 'remote-a' }]))
    await refreshFleetUpdates({ force: true })
    expect($fleetUpdates.get()['remote-a']).toBeDefined()

    $registry.set(registry([{ id: 'local', kind: 'local' }]))
    await refreshFleetUpdates()

    expect($fleetUpdates.get()).toEqual({})
  })

  it('keeps unknown availability retry-only instead of offering an update', async () => {
    $registry.set(registry([{ id: 'offline-box' }]))
    checkHermesUpdate.mockResolvedValue(
      check({ behind: null, can_apply: true, message: 'Update source is offline.', update_available: false })
    )

    await refreshFleetUpdates({ force: true })

    expect($fleetUpdates.get()['offline-box']).toMatchObject({ action: 'retry', availability: null, outcome: 'idle' })
    await expect(applyFleetUpdate('offline-box')).resolves.toMatchObject({ outcome: 'failed' })
    expect(updateHermes).not.toHaveBeenCalled()
  })

  it('publishes bot profiles/platform presence as optional row context', async () => {
    $registry.set(registry([{ id: 'bot-box' }]))
    getScopedStatus.mockResolvedValue({
      ...status('1.0.0', 'bot-install'),
      gateway_platforms: {
        'research:discord': { state: 'running', updated_at: '' },
        'work:telegram': { state: 'running', updated_at: '' },
        telegram: { state: 'running', updated_at: '' }
      }
    })
    ;(window.hermesDesktop!.getAgentRoster as ReturnType<typeof vi.fn>).mockResolvedValue({
      agents: [
        {
          connectionId: 'bot-box',
          connectionKind: 'remote',
          connectionLabel: 'Bot box',
          handle: '@default-bot-box',
          profile: 'default'
        }
      ],
      sources: []
    })

    await refreshFleetUpdates({ force: true })

    expect($fleetUpdates.get()['bot-box']).toMatchObject({
      botPlatforms: ['discord', 'telegram'],
      botProfiles: ['default'],
      installId: 'bot-install'
    })
  })

  it('ignores an old A response after a newer B generation has published', async () => {
    $registry.set(registry([{ id: 'remote-a' }]))
    const oldStatus = deferred<ReturnType<typeof status>>()
    const oldCheck = deferred<ReturnType<typeof check>>()

    getScopedStatus.mockReturnValueOnce(oldStatus.promise).mockResolvedValue(status('2.0.0', 'new-install'))
    checkHermesUpdate
      .mockReturnValueOnce(oldCheck.promise)
      .mockResolvedValue(check({ behind: 0, current_version: '2.0.0', update_available: false }))

    const oldRefresh = refreshFleetUpdates({ force: true })
    await vi.waitFor(() => expect(getScopedStatus).toHaveBeenCalledTimes(1))
    await refreshFleetUpdates({ force: true })

    oldStatus.resolve(status('1.0.0', 'old-install'))
    oldCheck.resolve(check({ current_version: '1.0.0' }))
    await oldRefresh

    expect($fleetUpdates.get()['remote-a']).toMatchObject({
      availability: false,
      currentVersion: '2.0.0',
      installId: 'new-install',
      outcome: 'current'
    })
  })
})

describe('applyFleetUpdate', () => {
  it('deduplicates concurrent aliases by installId and waits for terminal success', async () => {
    $registry.set(
      registry([
        { id: 'alias-a', installId: 'shared' },
        { id: 'alias-b', installId: 'shared' }
      ])
    )
    getScopedStatus.mockResolvedValue(status('0.9.0', 'shared'))
    await refreshFleetUpdates({ force: true })

    const first = applyFleetUpdate('alias-a')
    const second = applyFleetUpdate('alias-b')
    await Promise.all([first, second])

    expect(updateHermes).toHaveBeenCalledTimes(1)
    expect(updateHermes).toHaveBeenCalledWith({ connectionId: 'alias-a' })
    expect(getActionStatus).toHaveBeenCalledWith('hermes-update', 2_000, { connectionId: 'alias-a' })
    expect($fleetUpdates.get()['alias-a'].outcome).toBe('success')
    expect($fleetUpdates.get()['alias-b'].outcome).toBe('success')
    expect($fleetUpdates.get()['alias-b'].connectionId).toBe('alias-b')
  })

  it('re-keys concurrent aliases by the authoritative preflight installId before mutating', async () => {
    $registry.set(registry([{ id: 'alias-a' }, { id: 'alias-b' }]))
    let statusCalls = 0
    getScopedStatus.mockImplementation(() => {
      statusCalls += 1

      return Promise.resolve({
        ...status('0.9.0', 'shared-after-preflight'),
        install_id: statusCalls <= 2 ? undefined : 'shared-after-preflight'
      })
    })
    await refreshFleetUpdates({ force: true })
    expect($fleetUpdates.get()['alias-a'].installId).toBeNull()
    expect($fleetUpdates.get()['alias-b'].installId).toBeNull()

    await Promise.all([applyFleetUpdate('alias-a'), applyFleetUpdate('alias-b')])

    expect(updateHermes).toHaveBeenCalledTimes(1)
    expect($fleetUpdates.get()['alias-a'].installId).toBe('shared-after-preflight')
    expect($fleetUpdates.get()['alias-b'].installId).toBe('shared-after-preflight')
  })

  it('does not join two installs solely because their remembered installId is stale', async () => {
    $registry.set(
      registry([
        { id: 'former-alias-a', installId: 'stale-shared' },
        { id: 'former-alias-b', installId: 'stale-shared' }
      ])
    )
    let statusCalls = 0
    getScopedStatus.mockImplementation((scope: { connectionId: string }) => {
      statusCalls += 1

      if (statusCalls <= 2) {
        return Promise.reject(new Error('status temporarily unavailable'))
      }

      return Promise.resolve(status('0.9.0', `fresh-${scope.connectionId}`))
    })
    await refreshFleetUpdates({ force: true })
    expect($fleetUpdates.get()['former-alias-a'].installId).toBe('stale-shared')
    expect($fleetUpdates.get()['former-alias-b'].installId).toBe('stale-shared')

    await Promise.all([applyFleetUpdate('former-alias-a'), applyFleetUpdate('former-alias-b')])

    expect(updateHermes).toHaveBeenCalledTimes(2)
    expect($fleetUpdates.get()['former-alias-a'].installId).toBe('fresh-former-alias-a')
    expect($fleetUpdates.get()['former-alias-b'].installId).toBe('fresh-former-alias-b')
  })

  it('preflights every update-all row before deduplicating remembered aliases', async () => {
    $registry.set(
      registry([
        { id: 'former-alias-a', installId: 'stale-shared' },
        { id: 'former-alias-b', installId: 'stale-shared' }
      ])
    )
    let statusCalls = 0
    getScopedStatus.mockImplementation((scope: { connectionId: string }) => {
      statusCalls += 1

      if (statusCalls <= 2) {
        return Promise.reject(new Error('status temporarily unavailable'))
      }

      return Promise.resolve(status('0.9.0', `fresh-${scope.connectionId}`))
    })

    const results = await applyFleetUpdates()

    expect(updateHermes).toHaveBeenCalledTimes(2)
    expect(results).toHaveLength(2)
    expect(results.map(result => result.installId).sort()).toEqual(['fresh-former-alias-a', 'fresh-former-alias-b'])
  })

  it('surfaces an exact manual image command without posting an update', async () => {
    $registry.set(registry([{ id: 'image-box' }]))
    checkHermesUpdate.mockResolvedValue(
      check({
        can_apply: false,
        deployment_kind: 'image',
        install_method: 'docker',
        update_command: 'docker compose pull hermes && docker compose up -d hermes'
      })
    )
    await refreshFleetUpdates({ force: true })

    const result = await applyFleetUpdate('image-box')

    expect(result).toMatchObject({
      command: 'docker compose pull hermes && docker compose up -d hermes',
      outcome: 'manual'
    })
    expect(updateHermes).not.toHaveBeenCalled()
  })

  it('surfaces the literal image-managed response even when upstream availability is unknowable', async () => {
    $registry.set(registry([{ id: 'image-box' }]))
    checkHermesUpdate.mockResolvedValue(
      check({
        behind: null,
        can_apply: false,
        deployment_kind: 'image',
        install_method: 'docker',
        message: 'Image-managed runtime — pull and recreate it through its deployment owner.',
        update_available: false,
        update_command: 'docker compose pull hermes && docker compose up -d hermes'
      })
    )
    await refreshFleetUpdates({ force: true })

    expect($fleetUpdates.get()['image-box']).toMatchObject({
      action: 'manual',
      availability: null,
      deploymentKind: 'image',
      outcome: 'manual'
    })

    const result = await applyFleetUpdate('image-box')

    expect(result).toMatchObject({
      command: 'docker compose pull hermes && docker compose up -d hermes',
      outcome: 'manual'
    })
    expect(updateHermes).not.toHaveBeenCalled()
  })

  it('renders the production managed-runtime payload as managed without a bogus copy command', async () => {
    $registry.set(registry([{ id: 'hosted-image' }]))
    checkHermesUpdate.mockResolvedValue(
      check({
        behind: null,
        can_apply: false,
        deployment_class: 'image',
        deployment_kind: 'image',
        install_method: 'managed-runtime',
        message: 'Hermes updates are managed outside this dashboard in containerized environments.',
        update_available: false,
        update_command: null
      })
    )
    await refreshFleetUpdates({ force: true })

    expect($fleetUpdates.get()['hosted-image']).toMatchObject({
      action: 'managed',
      availability: null,
      deploymentKind: 'image',
      outcome: 'managed',
      updateCommand: null
    })
    await expect(applyFleetUpdate('hosted-image')).resolves.toMatchObject({ outcome: 'managed' })
    expect(updateHermes).not.toHaveBeenCalled()
  })

  it('does not expose the legacy managed-runtime prose sentinel as a command', async () => {
    $registry.set(registry([{ id: 'legacy-hosted-image' }]))
    checkHermesUpdate.mockResolvedValue(
      check({
        behind: null,
        can_apply: false,
        deployment_kind: 'image',
        install_method: 'managed-runtime',
        update_available: false,
        update_command: 'managed outside dashboard'
      })
    )
    await refreshFleetUpdates({ force: true })

    expect($fleetUpdates.get()['legacy-hosted-image']).toMatchObject({
      action: 'managed',
      outcome: 'managed',
      updateCommand: null
    })
  })

  it('renders a behind external install with no safe command as externally managed', async () => {
    $registry.set(registry([{ id: 'operator-box' }]))
    checkHermesUpdate.mockResolvedValue(
      check({
        can_apply: false,
        deployment_kind: 'external',
        install_method: 'operator-managed',
        update_command: null
      })
    )
    await refreshFleetUpdates({ force: true })

    expect($fleetUpdates.get()['operator-box']).toMatchObject({
      action: 'managed',
      availability: true,
      deploymentKind: 'external',
      outcome: 'managed'
    })
    await expect(applyFleetUpdate('operator-box')).resolves.toMatchObject({ outcome: 'managed' })
    expect(updateHermes).not.toHaveBeenCalled()
  })

  it('keeps an externally managed install actionable when it supplies an exact command', async () => {
    $registry.set(registry([{ id: 'operator-box' }]))
    checkHermesUpdate.mockResolvedValue(
      check({
        can_apply: false,
        deployment_kind: 'external',
        install_method: 'operator-managed',
        update_command: 'operatorctl upgrade hermes'
      })
    )
    await refreshFleetUpdates({ force: true })

    await expect(applyFleetUpdate('operator-box')).resolves.toMatchObject({
      command: 'operatorctl upgrade hermes',
      outcome: 'manual'
    })
    expect(updateHermes).not.toHaveBeenCalled()
  })

  it('keeps a production SSH-owned serve runtime manual instead of blindly mutating it', async () => {
    $registry.set(registry([{ id: 'windows-ssh', kind: 'ssh' }]))
    // Production backends still describe the checkout itself as mutable git;
    // the Desktop connection kind is the ownership signal that makes the live
    // SSH serve process unsafe to mutate in place (#63206).
    checkHermesUpdate.mockResolvedValue(
      check({ can_apply: true, install_method: 'git', update_command: 'hermes update' })
    )
    await refreshFleetUpdates({ force: true, includeInactiveSsh: true })

    const result = await applyFleetUpdate('windows-ssh')

    expect(result).toMatchObject({
      command: 'hermes update',
      outcome: 'manual'
    })
    expect($fleetUpdates.get()['windows-ssh']).toMatchObject({
      action: 'manual',
      deploymentKind: 'mutable'
    })
    expect(updateHermes).not.toHaveBeenCalled()
  })

  it('routes a mutable SSH install through the managed drain/update/restore bridge', async () => {
    const updateManaged = vi.fn().mockResolvedValue({
      connectionId: 'linux-ssh',
      correlationId: '0c44e2da-993e-4d96-ab45-2d0f73365d61',
      exitCode: 0,
      ok: true,
      outcome: 'updated',
      receipt: { correlationId: '0c44e2da-993e-4d96-ab45-2d0f73365d61', outcome: 'success' },
      restoreOk: true,
      scopes: [{ profile: 'default', restored: true }],
      updateOk: true
    })
    ;(window as { hermesDesktop?: unknown }).hermesDesktop = {
      api: vi.fn(),
      connections: { updateManaged },
      getAgentRoster: vi.fn().mockResolvedValue({ agents: [], sources: [] })
    }
    $registry.set(registry([{ id: 'linux-ssh', kind: 'ssh' }]))
    checkHermesUpdate.mockResolvedValue(
      check({ can_apply: true, deployment_kind: 'systemd', install_method: 'git', update_command: 'hermes update' })
    )
    await refreshFleetUpdates({ force: true, includeInactiveSsh: true })

    expect($fleetUpdates.get()['linux-ssh']).toMatchObject({
      action: 'apply',
      deploymentKind: 'systemd'
    })

    const result = await applyFleetUpdate('linux-ssh')

    expect(result).toMatchObject({ outcome: 'success' })
    expect(updateManaged).toHaveBeenCalledWith('linux-ssh')
    expect(updateHermes).not.toHaveBeenCalled()
  })

  it('treats a current correlated refused receipt as terminal manual guidance', async () => {
    $registry.set(registry([{ id: 'image-box' }]))
    await refreshFleetUpdates({ force: true })
    updateHermes.mockResolvedValue({ correlation_id: 'run-new', name: 'hermes-update', ok: true, pid: 12 })
    getActionStatus.mockResolvedValue({
      exit_code: null,
      lines: [],
      name: 'hermes-update',
      pid: null,
      receipt: {
        correlation_id: 'run-new',
        finished_at: new Date().toISOString(),
        fleet_states: [],
        outcome: 'refused',
        post_sha: null,
        post_version: null,
        pre_sha: null,
        refusal: {
          code: 'image_managed',
          message: 'Pull and recreate this image.',
          update_command: 'docker compose pull hermes && docker compose up -d hermes'
        },
        started_at: new Date().toISOString(),
        stop_reason: 'image_managed'
      },
      running: false
    })

    const result = await applyFleetUpdate('image-box')

    expect(result).toMatchObject({
      command: 'docker compose pull hermes && docker compose up -d hermes',
      message: 'Pull and recreate this image.',
      outcome: 'manual'
    })
  })

  it('preserves partial as a truthful terminal outcome', async () => {
    $registry.set(registry([{ id: 'partial-box' }]))
    await refreshFleetUpdates({ force: true })
    updateHermes.mockResolvedValue({ correlation_id: 'run-partial', name: 'hermes-update', ok: true, pid: 12 })
    getActionStatus.mockResolvedValue({
      exit_code: null,
      lines: [],
      name: 'hermes-update',
      pid: null,
      receipt: {
        correlation_id: 'run-partial',
        finished_at: new Date().toISOString(),
        fleet_states: ['partial'],
        outcome: 'partial',
        post_sha: null,
        post_version: null,
        pre_sha: null,
        started_at: new Date().toISOString()
      },
      running: false
    })

    await expect(applyFleetUpdate('partial-box')).resolves.toMatchObject({ outcome: 'partial' })
    expect($fleetUpdates.get()['partial-box'].outcome).toBe('partial')
  })

  it('ignores a stale successful action marker until this correlated action finishes', async () => {
    vi.useFakeTimers()
    try {
      $registry.set(registry([{ id: 'remote-a' }]))
      await refreshFleetUpdates({ force: true })
      updateHermes.mockResolvedValue({ action_id: 'run-new', name: 'hermes-update', ok: true, pid: 12 })
      getActionStatus
        .mockResolvedValueOnce({
          action_id: 'run-old',
          exit_code: 0,
          lines: ['=== hermes-update completed run-old ==='],
          name: 'hermes-update',
          pid: null,
          running: false
        })
        .mockResolvedValueOnce({
          action_id: 'run-new',
          exit_code: 0,
          lines: ['=== hermes-update completed run-new ==='],
          name: 'hermes-update',
          pid: null,
          running: false
        })

      const result = applyFleetUpdate('remote-a')
      await vi.advanceTimersByTimeAsync(0)
      expect(getActionStatus).toHaveBeenCalledTimes(1)

      await vi.advanceTimersByTimeAsync(1_500)
      await expect(result).resolves.toMatchObject({ outcome: 'success' })
      expect(getActionStatus).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not accept an uncorrelated receipt by timestamp when action ownership disagrees', async () => {
    vi.useFakeTimers()
    try {
      $registry.set(registry([{ id: 'remote-a' }]))
      await refreshFleetUpdates({ force: true })
      updateHermes.mockResolvedValue({ action_id: 'run-new', name: 'hermes-update', ok: true, pid: 12 })
      const receipt = {
        finished_at: new Date().toISOString(),
        fleet_states: [],
        outcome: 'success',
        post_sha: null,
        post_version: null,
        pre_sha: null,
        started_at: new Date().toISOString()
      }
      getActionStatus
        .mockResolvedValueOnce({
          action_id: 'run-old',
          exit_code: null,
          lines: [],
          name: 'hermes-update',
          pid: null,
          receipt,
          running: false
        })
        .mockResolvedValueOnce({
          action_id: 'run-new',
          exit_code: null,
          lines: [],
          name: 'hermes-update',
          pid: null,
          receipt,
          running: false
        })

      const result = applyFleetUpdate('remote-a')
      await vi.advanceTimersByTimeAsync(0)
      expect(getActionStatus).toHaveBeenCalledTimes(1)

      await vi.advanceTimersByTimeAsync(1_500)
      await expect(result).resolves.toMatchObject({ outcome: 'success' })
      expect(getActionStatus).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('restarts only the skewed gateway profile, waits for proof, and re-probes', async () => {
    vi.useFakeTimers()
    try {
      $registry.set(registry([{ id: 'remote-a', installId: 'install-a' }]))
      const skewed = {
        ...status('1.0.0', 'install-a'),
        checkout_code_sha: 'new',
        gateway_code_sha: 'old',
        gateway_profile: 'work',
        gateway_restart_required: true
      }
      const current = {
        ...skewed,
        gateway_code_sha: 'new',
        gateway_restart_required: false
      }
      getScopedStatus.mockResolvedValueOnce(skewed).mockResolvedValueOnce(skewed).mockResolvedValueOnce(current)
      getActionStatus.mockResolvedValue({
        action_id: 'restart-a',
        exit_code: 0,
        lines: [],
        name: 'gateway-restart',
        pid: null,
        running: false
      })
      await refreshFleetUpdates({ force: true })

      expect($fleetUpdates.get()['remote-a']).toMatchObject({
        action: 'restart',
        gatewayProfile: 'work',
        outcome: 'restart-required'
      })

      const result = applyFleetUpdate('remote-a')
      await vi.advanceTimersByTimeAsync(1_500)
      await expect(result).resolves.toMatchObject({ outcome: 'restarted' })

      expect(restartGateway).toHaveBeenCalledWith({ connectionId: 'remote-a', profile: 'work' })
      expect(getActionStatus).toHaveBeenCalledWith('gateway-restart', 2_000, {
        connectionId: 'remote-a',
        profile: 'work'
      })
      expect(getScopedStatus).toHaveBeenNthCalledWith(3, { connectionId: 'remote-a', profile: 'work' })
      expect(updateHermes).not.toHaveBeenCalled()
      expect($fleetUpdates.get()['remote-a']).toMatchObject({
        action: 'apply',
        gatewayRestartRequired: false,
        outcome: 'restarted'
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('keeps same-install profile restart outcomes isolated', async () => {
    vi.useFakeTimers()
    try {
      $registry.set(
        registry([
          { id: 'default-alias', installId: 'shared-install' },
          { id: 'research-alias', installId: 'shared-install' }
        ])
      )
      getScopedStatus.mockImplementation((scope: { connectionId: string; profile?: string }) =>
        Promise.resolve({
          ...status('1.0.0', 'shared-install'),
          checkout_code_sha: 'new',
          gateway_code_sha: scope.profile ? 'new' : 'old',
          gateway_profile: scope.connectionId === 'default-alias' ? 'default' : 'research',
          gateway_restart_required: !scope.profile
        })
      )
      restartGateway.mockImplementation((scope: { profile?: string }) =>
        Promise.resolve({
          action_id: `restart-${scope.profile}`,
          name: 'gateway-restart',
          ok: true,
          pid: 12
        })
      )
      getActionStatus.mockImplementation((_name: string, _lines: number, scope: { profile?: string }) =>
        Promise.resolve({
          action_id: `restart-${scope.profile}`,
          exit_code: scope.profile === 'research' ? 1 : 0,
          lines: scope.profile === 'research' ? ['research restart failed'] : [],
          name: 'gateway-restart',
          pid: null,
          running: false
        })
      )
      await refreshFleetUpdates({ force: true })

      const results = Promise.all([applyFleetUpdate('default-alias'), applyFleetUpdate('research-alias')])
      await vi.advanceTimersByTimeAsync(1_500)
      await expect(results).resolves.toEqual([
        { connectionId: 'default-alias', gatewayProfile: 'default', installId: 'shared-install', outcome: 'restarted' },
        {
          connectionId: 'research-alias',
          gatewayProfile: 'research',
          installId: 'shared-install',
          message: 'research restart failed',
          outcome: 'failed'
        }
      ])

      expect($fleetUpdates.get()['default-alias']).toMatchObject({
        gatewayProfile: 'default',
        outcome: 'restarted'
      })
      expect($fleetUpdates.get()['research-alias']).toMatchObject({
        error: 'research restart failed',
        gatewayProfile: 'research',
        outcome: 'failed'
      })
    } finally {
      vi.useRealTimers()
    }
  })
})
