import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopConnectionsRegistry } from '@/global'
import { $notifications } from '@/store/notifications'
import { $connection } from '@/store/session'

import {
  ConnectionsRegistrySection,
  findDuplicateConnection,
  isConnectionAuthRejection,
  normalizeGatewayUrl,
  sameBackendPeerLabel,
  sshCompositeKey
} from './connections-registry'

const list = vi.fn()
const save = vi.fn()
const remove = vi.fn()
const setLaunchMode = vi.fn()
const setPrimary = vi.fn()
const test = vi.fn()
const createDraft = vi.fn()
const authProbe = vi.fn()
const authLogin = vi.fn()
const authVerify = vi.fn()
const authStatus = vi.fn()
const authClear = vi.fn()

const registry: DesktopConnectionsRegistry = {
  connections: [
    { id: 'local', kind: 'local', label: 'This device', tokenPreview: null, tokenSet: false },
    {
      authMode: 'token',
      id: 'homelab',
      kind: 'remote',
      label: 'Homelab',
      tokenPreview: '...abc123',
      tokenSet: true,
      url: 'http://homelab.lan:9119'
    }
  ],
  primary: 'local',
  secureTokenStorage: true,
  version: 2
}

beforeEach(() => {
  $notifications.set([])
  $connection.set({
    baseUrl: 'http://homelab.lan:9119',
    connectionId: 'homelab',
    isFullscreen: false,
    logs: [],
    mode: 'remote',
    nativeOverlayWidth: 0,
    token: 'test-token',
    windowButtonPosition: null,
    wsUrl: 'ws://homelab.lan:9119/ws'
  })
  list.mockResolvedValue(registry)
  save.mockResolvedValue({ connection: registry.connections[1], ok: true, registry })
  remove.mockResolvedValue({ ok: true, registry: { ...registry, connections: [registry.connections[0]] } })
  setLaunchMode.mockResolvedValue({ ok: true, registry: { ...registry, launchMode: 'last-used' } })
  setPrimary.mockResolvedValue({ ok: true, registry: { ...registry, primary: 'homelab' } })
  test.mockResolvedValue({ ok: true, reachable: true })

  createDraft.mockResolvedValue({ ok: true, scope: 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512' })
  authProbe.mockResolvedValue({
    authMode: 'token',
    baseUrl: 'http://homelab.lan:9119',
    error: null,
    providers: [],
    reachable: true,
    scope: 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512',
    version: '0.20.4'
  })
  authLogin.mockResolvedValue({ baseUrl: 'http://homelab.lan:9119', connected: true, ok: true })
  authVerify.mockResolvedValue({ baseUrl: 'http://homelab.lan:9119', ok: true, version: '0.20.4' })
  authStatus.mockResolvedValue({ baseUrl: 'http://homelab.lan:9119', connected: false, ok: true })
  authClear.mockResolvedValue({ baseUrl: 'http://homelab.lan:9119', connected: false, ok: true })
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      connections: {
        auth: { clear: authClear, createDraft, login: authLogin, probe: authProbe, status: authStatus, verify: authVerify },
        list,
        remove,
        save,
        setLaunchMode,
        setPrimary,
        test
      }
    }
  })
})

afterEach(() => {
  $connection.set(null)
  cleanup()
  vi.clearAllMocks()
})

describe('ConnectionsRegistrySection', () => {
  it('distinguishes the current connection from the registry primary', async () => {
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    // Label and the managed pill share the copy, so expect both instances.
    expect(screen.getAllByText('This device').length).toBeGreaterThan(0)
    expect(screen.getByText('Current')).toBeTruthy()
    expect(screen.getAllByText('Primary').length).toBeGreaterThan(0)
    expect(list).toHaveBeenCalledTimes(1)
  })

  it('creates one draft and keeps a new remote disabled until current discovery and readiness succeed', async () => {
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    await waitFor(() => expect(createDraft).toHaveBeenCalledTimes(1))

    const saveButton = screen.getByText('Save connection').closest('button') as HTMLButtonElement
    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'Spark box' } })
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'http://spark.lan:9119' }
    })

    expect(saveButton.disabled).toBe(true)
    expect(save).not.toHaveBeenCalled()
  })

  it('shows an actionable unreachable discovery error and keeps save disabled', async () => {
    authProbe.mockResolvedValueOnce({
      authMode: 'token',
      baseUrl: 'https://offline.example.test',
      error: 'Connection refused. Check the gateway URL and network access.',
      providers: [],
      reachable: false,
      scope: 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512',
      version: null
    })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'Offline' } })
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'https://offline.example.test' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))

    expect(await screen.findByText('Connection refused. Check the gateway URL and network access.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Save connection' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('shows a malformed URL discovery error and keeps save disabled', async () => {
    authProbe.mockResolvedValueOnce({ error: 'Enter a valid HTTP or HTTPS gateway URL.', ok: false })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'Malformed' } })
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'not-a-url' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))

    expect(await screen.findByText('Enter a valid HTTP or HTTPS gateway URL.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Save connection' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('keeps save disabled when login connects but websocket readiness fails', async () => {
    authProbe.mockResolvedValueOnce({
      authMode: 'oauth',
      baseUrl: 'https://gateway.example.test',
      error: null,
      providers: [{ name: 'nous', displayName: 'Nous Research', supportsPassword: false }],
      reachable: true,
      scope: 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512',
      version: '0.20.4'
    })
    authVerify.mockResolvedValueOnce({
      error: 'The authenticated gateway WebSocket readiness check failed: upgrade rejected',
      kind: 'transport-error',
      ok: false
    })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'Nous gateway' } })
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'https://gateway.example.test' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in with Nous Research' }))

    expect(
      await screen.findByText('The authenticated gateway WebSocket readiness check failed: upgrade rejected')
    ).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Save connection' }) as HTMLButtonElement).disabled).toBe(true)
    expect($connection.get()?.connectionId).toBe('homelab')
  })

  it('detects password authentication and requires sign-in before saving a remote gateway', async () => {
    authProbe.mockResolvedValueOnce({
      authMode: 'oauth',
      baseUrl: 'http://100.110.110.95:9119',
      error: null,
      providers: [{ name: 'basic', displayName: 'Username & Password', supportsPassword: true }],
      reachable: true,
      version: '0.20.4'
    })
    authLogin.mockResolvedValueOnce({
      baseUrl: 'http://100.110.110.95:9119',
      connected: true,
      ok: true
    })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'PopCorn VPS' } })
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'http://100.110.110.95:9119' }
    })

    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))

    await waitFor(() => expect(authProbe).toHaveBeenCalledWith(expect.objectContaining({ url: 'http://100.110.110.95:9119' })))
    expect(screen.queryByPlaceholderText('Paste session token')).toBeNull()
    const signIn = screen.getByRole('button', { name: 'Sign in' })
    const saveButton = screen.getByRole('button', { name: 'Save connection' }) as HTMLButtonElement
    expect(saveButton.disabled).toBe(true)

    fireEvent.click(signIn)

    await waitFor(() => expect(authLogin).toHaveBeenCalledWith(expect.objectContaining({ url: 'http://100.110.110.95:9119' })))
    await waitFor(() => expect(saveButton.disabled).toBe(false))
    fireEvent.click(saveButton)

    await waitFor(() =>
      expect(save).toHaveBeenCalledWith(
        expect.objectContaining({
          authMode: 'oauth',
          kind: 'remote',
          label: 'PopCorn VPS',
          url: 'http://100.110.110.95:9119'
        })
      )
    )
  })

  it('uses provider-generic OAuth copy without changing the active local connection', async () => {
    $connection.set({ ...$connection.get()!, connectionId: 'local', mode: 'local' })
    authProbe.mockResolvedValueOnce({
      authMode: 'oauth',
      baseUrl: 'https://gateway.example.test',
      error: null,
      providers: [{ name: 'nous', displayName: 'Nous Research', supportsPassword: false }],
      reachable: true,
      version: '0.20.4'
    })
    authLogin.mockResolvedValueOnce({
      baseUrl: 'https://gateway.example.test',
      connected: true,
      ok: true
    })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'Nous gateway' } })
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'https://gateway.example.test' }
    })
    expect($connection.get()?.connectionId).toBe('local')

    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))
    const signIn = await screen.findByRole('button', { name: 'Sign in with Nous Research' })
    expect($connection.get()?.connectionId).toBe('local')

    fireEvent.click(signIn)
    const saveButton = screen.getByRole('button', { name: 'Save connection' }) as HTMLButtonElement
    await waitFor(() => expect(saveButton.disabled).toBe(false))
    expect($connection.get()?.connectionId).toBe('local')

    fireEvent.click(saveButton)
    await waitFor(() => expect(save).toHaveBeenCalledTimes(1))
    expect($connection.get()?.connectionId).toBe('local')
    expect(setPrimary).not.toHaveBeenCalled()
  })

  it('uses every backend-provided display name for mixed OAuth providers', async () => {
    authProbe.mockResolvedValueOnce({
      authMode: 'oauth',
      baseUrl: 'https://gateway.example.test',
      error: null,
      providers: [
        { name: 'password', displayName: 'Password', supportsPassword: true },
        { name: 'enterprise-oidc', displayName: 'Enterprise SSO', supportsPassword: false }
      ],
      reachable: true,
      version: '0.20.4'
    })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'https://gateway.example.test' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))

    expect(await screen.findByRole('button', { name: 'Sign in with Password / Enterprise SSO' })).toBeTruthy()
  })

  it('keeps a cancelled OAuth login unsigned-in and warns without saving', async () => {
    $connection.set({ ...$connection.get()!, connectionId: 'local', mode: 'local' })
    authProbe.mockResolvedValueOnce({
      authMode: 'oauth',
      baseUrl: 'https://gateway.example.test',
      error: null,
      providers: [{ name: 'nous', displayName: 'Nous Research', supportsPassword: false }],
      reachable: true,
      version: '0.20.4'
    })
    authLogin.mockResolvedValueOnce({ connected: false, ok: true })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'Nous gateway' } })
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'https://gateway.example.test' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))

    const signIn = await screen.findByRole('button', { name: 'Sign in with Nous Research' })
    const saveButton = screen.getByRole('button', { name: 'Save connection' }) as HTMLButtonElement
    fireEvent.click(signIn)

    await waitFor(() => expect(authLogin).toHaveBeenCalledWith(expect.objectContaining({ url: 'https://gateway.example.test' })))
    await waitFor(() => expect(screen.getByRole('button', { name: 'Sign in with Nous Research' })).toBeTruthy())
    expect(saveButton.disabled).toBe(true)
    expect(save).not.toHaveBeenCalled()
    expect($connection.get()?.connectionId).toBe('local')
    expect($notifications.get()).toEqual([
      expect.objectContaining({
        kind: 'warning',
        message: 'The login window closed before authentication finished.',
        title: 'Sign-in incomplete'
      })
    ])
  })

  it('ignores a stale auth probe after the editor URL changes', async () => {
    $connection.set({ ...$connection.get()!, connectionId: 'local', mode: 'local' })
    let resolveProbe!: (result: Awaited<ReturnType<typeof window.hermesDesktop.connections.auth.probe>>) => void
    authProbe.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveProbe = resolve
        })
    )
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    const urlInput = screen.getByPlaceholderText('http://homelab.lan:9119') as HTMLInputElement
    fireEvent.change(urlInput, { target: { value: 'https://gateway-a.example.test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))
    await waitFor(() => expect(authProbe).toHaveBeenCalledWith(expect.objectContaining({ url: 'https://gateway-a.example.test' })))

    fireEvent.change(urlInput, { target: { value: 'https://gateway-b.example.test' } })
    await act(async () => {
      resolveProbe({
        authMode: 'oauth',
        baseUrl: 'https://gateway-a.example.test',
        error: null,
        providers: [{ name: 'nous', displayName: 'Nous Research', supportsPassword: false }],
        reachable: true,
        scope: 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512',
        version: '0.20.4'
      })
    })

    expect(urlInput.value).toBe('https://gateway-b.example.test')
    expect(screen.getByRole('button', { name: 'Detect authentication' })).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Sign in with Nous Research' })).toBeNull()
    expect($connection.get()?.connectionId).toBe('local')
  })

  it('ignores a stale successful login after the editor URL changes', async () => {
    $connection.set({ ...$connection.get()!, connectionId: 'local', mode: 'local' })
    authProbe.mockResolvedValueOnce({
      authMode: 'oauth',
      baseUrl: 'https://gateway-a.example.test',
      error: null,
      providers: [{ name: 'nous', displayName: 'Nous Research', supportsPassword: false }],
      reachable: true,
      version: '0.20.4'
    })
    let resolveLogin!: (result: Awaited<ReturnType<typeof window.hermesDesktop.connections.auth.login>>) => void
    authLogin.mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveLogin = resolve
        })
    )
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'Nous gateway' } })
    const urlInput = screen.getByPlaceholderText('http://homelab.lan:9119') as HTMLInputElement
    fireEvent.change(urlInput, { target: { value: 'https://gateway-a.example.test' } })
    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in with Nous Research' }))
    await waitFor(() => expect(authLogin).toHaveBeenCalledWith(expect.objectContaining({ url: 'https://gateway-a.example.test' })))

    fireEvent.change(urlInput, { target: { value: 'https://gateway-b.example.test' } })
    await act(async () => {
      resolveLogin({ baseUrl: 'https://gateway-a.example.test', connected: true, ok: true })
    })

    expect(urlInput.value).toBe('https://gateway-b.example.test')
    expect((screen.getByRole('button', { name: 'Save connection' }) as HTMLButtonElement).disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Detect authentication' })).toBeTruthy()
    expect(save).not.toHaveBeenCalled()
    expect($connection.get()?.connectionId).toBe('local')
  })

  it('requires candidate-token websocket readiness before enabling save', async () => {
    list.mockResolvedValueOnce({
      ...registry,
      connections: [
        registry.connections[0],
        { ...registry.connections[1], authMode: 'oauth', tokenPreview: null, tokenSet: false }
      ]
    })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))

    await waitFor(() => expect(authProbe).toHaveBeenCalledWith(expect.objectContaining({ url: 'http://homelab.lan:9119' })))
    const saveButton = screen.getByRole('button', { name: 'Save connection' }) as HTMLButtonElement
    expect(saveButton.disabled).toBe(true)

    fireEvent.change(screen.getByPlaceholderText('Paste session token'), { target: { value: 'new-token' } })
    expect(saveButton.disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))
    const verifyButton = await screen.findByRole('button', { name: 'Verify connection' })
    fireEvent.click(verifyButton)

    await waitFor(() =>
      expect(authVerify).toHaveBeenCalledWith(
        expect.objectContaining({ authMode: 'token', scope: 'homelab', token: 'new-token' })
      )
    )
    await waitFor(() => expect(saveButton.disabled).toBe(false))
  })

  it('invalidates an existing OAuth sign-in when its URL changes', async () => {
    list.mockResolvedValueOnce({
      ...registry,
      connections: [
        registry.connections[0],
        { ...registry.connections[1], authMode: 'oauth', tokenPreview: null, tokenSet: false }
      ]
    })
    authProbe.mockResolvedValueOnce({
      authMode: 'oauth',
      baseUrl: 'http://new-homelab.lan:9119',
      error: null,
      providers: [{ name: 'basic', displayName: 'Username & Password', supportsPassword: true }],
      reachable: true,
      version: '0.20.4'
    })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'http://new-homelab.lan:9119' }
    })
    const saveButton = screen.getByRole('button', { name: 'Save connection' }) as HTMLButtonElement
    expect(saveButton.disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))

    await waitFor(() => expect(authProbe).toHaveBeenCalledWith(expect.objectContaining({ url: 'http://new-homelab.lan:9119' })))
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy()
    expect(saveButton.disabled).toBe(true)

    fireEvent.click(screen.getByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(authLogin).toHaveBeenCalledWith(expect.objectContaining({ url: 'http://new-homelab.lan:9119' })))
    await waitFor(() => expect(saveButton.disabled).toBe(false))
  })

  it('offers every kind on create and disables Local while the managed entry exists', async () => {
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))

    const localKind = screen.getByRole('button', { name: 'Local' }) as HTMLButtonElement
    expect(localKind.disabled).toBe(true)
    expect(screen.getByRole('button', { name: 'Hermes Cloud' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Remote gateway' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'SSH' })).toBeTruthy()
  })

  it('rejects a duplicate gateway URL in the save path with an inline error', async () => {
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))

    fireEvent.change(screen.getByPlaceholderText('Homelab'), { target: { value: 'Homelab twin' } })
    // Same URL modulo case + trailing slash: normalized-dupe of the existing entry.
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'HTTP://HOMELAB.LAN:9119/' }
    })
    fireEvent.change(screen.getByPlaceholderText('Paste session token'), { target: { value: 'candidate' } })
    fireEvent.click(screen.getByRole('button', { name: 'Detect authentication' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Verify connection' }))
    await waitFor(() => expect(authVerify).toHaveBeenCalled())
    fireEvent.click(screen.getByText('Save connection').closest('button')!)

    await waitFor(() =>
      expect(screen.getByText('A connection to this gateway URL already exists (“Homelab”).')).toBeTruthy()
    )
    expect(save).not.toHaveBeenCalled()
  })

  it('keeps the primary fallback configurable while last-used restore is enabled', async () => {
    list.mockResolvedValueOnce({ ...registry, launchMode: 'last-used' })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    const makePrimary = screen.getByText('Make primary').closest('button')!

    expect(makePrimary.disabled).toBe(false)
    fireEvent.click(makePrimary)

    await waitFor(() => expect(setPrimary).toHaveBeenCalledWith('homelab'))
  })

  it('lets users opt into restoring the last-used source', async () => {
    render(<ConnectionsRegistrySection />)

    const launchSetting = await screen.findByText('At startup, return to Sessions on the last-used gateway')
    const addConnection = screen.getByText('Add connection')

    expect(addConnection.compareDocumentPosition(launchSetting) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
    fireEvent.click(screen.getByRole('switch', { name: 'At startup, return to Sessions on the last-used gateway' }))

    await waitFor(() => expect(setLaunchMode).toHaveBeenCalledWith('last-used'))
  })

  it('keeps the launch preference out of the way for a single source', async () => {
    list.mockResolvedValueOnce({ ...registry, connections: [registry.connections[0]] })

    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(list).toHaveBeenCalledTimes(1))
    expect(screen.queryByText('At startup, return to Sessions on the last-used gateway')).toBeNull()
  })

  it('keeps search out of the way for a small registry', async () => {
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    expect(screen.queryByRole('searchbox', { name: 'Search gateways…' })).toBeNull()
  })

  it('sorts a large registry and searches names and endpoints', async () => {
    const largeRegistry: DesktopConnectionsRegistry = {
      ...registry,
      connections: [
        {
          authMode: 'token',
          id: 'zulu',
          kind: 'remote',
          label: 'Zulu',
          tokenPreview: null,
          tokenSet: false,
          url: 'https://zulu.example.test'
        },
        registry.connections[0],
        ...Array.from({ length: 6 }, (_, index) => ({
          authMode: 'token' as const,
          id: `gateway-${index}`,
          kind: 'remote' as const,
          label: index === 0 ? 'Alpha' : `Gateway ${index}`,
          tokenPreview: null,
          tokenSet: false,
          url:
            index === 4
              ? 'https://studio.example.test'
              : index === 5
                ? 'https://studio-archive.example.test'
                : `https://gateway-${index}.example.test`
        }))
      ]
    }

    list.mockResolvedValueOnce(largeRegistry)
    render(
      <div data-testid="settings-scroller" style={{ height: 400, overflowY: 'auto' }}>
        <ConnectionsRegistrySection />
      </div>
    )

    const search = await screen.findByRole('searchbox', { name: 'Search gateways…' })
    expect(search.parentElement?.className).toContain('mt-3')
    expect(search.parentElement?.className).toContain('mb-0')
    const settingsScroller = screen.getByTestId('settings-scroller')
    settingsScroller.scrollTop = 200
    vi.spyOn(search, 'getBoundingClientRect')
      .mockReturnValueOnce({
        bottom: 152,
        height: 32,
        left: 0,
        right: 0,
        top: 120,
        width: 0,
        x: 0,
        y: 120,
        toJSON: () => ({})
      })
      .mockReturnValueOnce({
        bottom: 152,
        height: 32,
        left: 0,
        right: 0,
        top: 120,
        width: 0,
        x: 0,
        y: 120,
        toJSON: () => ({})
      })
      .mockReturnValueOnce({
        bottom: 152,
        height: 32,
        left: 0,
        right: 0,
        top: 120,
        width: 0,
        x: 0,
        y: 120,
        toJSON: () => ({})
      })
      .mockReturnValue({
        bottom: 182,
        height: 32,
        left: 0,
        right: 0,
        top: 150,
        width: 0,
        x: 0,
        y: 150,
        toJSON: () => ({})
      })
    const alpha = screen.getByText('Alpha')
    const zulu = screen.getByText('Zulu')
    expect(alpha.compareDocumentPosition(zulu) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()

    fireEvent.change(search, { target: { value: 'studio' } })

    expect(settingsScroller.scrollTop).toBe(200)
    expect(screen.getByText('Gateway 4')).toBeTruthy()
    expect(screen.getByText('Gateway 5')).toBeTruthy()
    expect(screen.queryByText('Alpha')).toBeNull()

    settingsScroller.scrollTop = 260
    fireEvent.change(search, { target: { value: 'studio.example' } })
    expect(settingsScroller.scrollTop).toBe(290)
    expect(screen.getByText('Gateway 4')).toBeTruthy()
    expect(screen.queryByText('Gateway 5')).toBeNull()

    fireEvent.change(search, { target: { value: '' } })
    expect(search.closest<HTMLElement>('.border-t')?.style.minHeight).toBe('')
  })

  it('uses durable status and scopes reauthentication and sign-out to the edited connection', async () => {
    $connection.set({ ...$connection.get()!, connectionId: 'local', mode: 'local' })
    list.mockResolvedValueOnce({
      ...registry,
      connections: [
        registry.connections[0],
        { ...registry.connections[1], authMode: 'oauth', tokenPreview: null, tokenSet: false }
      ]
    })
    test.mockResolvedValueOnce({ error: 'Authentication required', kind: 'auth-required', ok: false })
    authStatus.mockResolvedValueOnce({ baseUrl: 'http://homelab.lan:9119', connected: true, ok: true })
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getAllByText('Test')[1])

    await waitFor(() => expect(test).toHaveBeenCalledWith('homelab'))
    expect(screen.getByText('Homelab')).toBeTruthy()
    expect($connection.get()?.connectionId).toBe('local')

    fireEvent.click(screen.getByRole('button', { name: 'Edit' }))
    await waitFor(() =>
      expect(authStatus).toHaveBeenCalledWith(expect.objectContaining({ scope: 'homelab' }))
    )
    expect(await screen.findByRole('button', { name: 'Reauthenticate' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Sign out' })).toBeTruthy()
    expect($connection.get()?.connectionId).toBe('local')

    fireEvent.click(screen.getByRole('button', { name: 'Reauthenticate' }))
    await waitFor(() =>
      expect(authLogin).toHaveBeenCalledWith(expect.objectContaining({ scope: 'homelab' }))
    )
    await waitFor(() =>
      expect(authVerify).toHaveBeenCalledWith(expect.objectContaining({ authMode: 'oauth', scope: 'homelab' }))
    )

    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }))
    await waitFor(() =>
      expect(authClear).toHaveBeenCalledWith(expect.objectContaining({ scope: 'homelab' }))
    )
    expect($connection.get()?.connectionId).toBe('local')
  })

  it('clears its owned draft scope when a new remote editor is cancelled', async () => {
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getByText('Add connection'))
    await waitFor(() => expect(createDraft).toHaveBeenCalledTimes(1))
    fireEvent.change(screen.getByPlaceholderText('http://homelab.lan:9119'), {
      target: { value: 'https://draft.example.test' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    await waitFor(() =>
      expect(authClear).toHaveBeenCalledWith(
        expect.objectContaining({
          scope: 'draft-76f9d14d-2f10-4ccb-9bb8-089935501512',
          url: 'https://draft.example.test'
        })
      )
    )
  })

  it('tests a connection through the bridge', async () => {
    render(<ConnectionsRegistrySection />)

    await waitFor(() => expect(screen.getByText('Homelab')).toBeTruthy())
    fireEvent.click(screen.getAllByText('Test')[0])

    await waitFor(() => expect(test).toHaveBeenCalled())
  })
})

describe('connection auth rejection classification', () => {
  it('matches direct and Electron-serialized 401/403 errors only', () => {
    expect(isConnectionAuthRejection({ statusCode: 401 })).toBe(true)
    expect(isConnectionAuthRejection({ statusCode: 403 })).toBe(true)
    expect(
      isConnectionAuthRejection(
        new Error("Error invoking remote method 'hermes:connections:test': Error: 401: Authentication required")
      )
    ).toBe(true)
    expect(
      isConnectionAuthRejection(
        new Error("Error invoking remote method 'hermes:connections:test': Error: 403: Forbidden")
      )
    ).toBe(true)
    expect(
      isConnectionAuthRejection(
        new Error(
          "Error invoking remote method 'hermes:connections:test': Error: Reached the gateway over HTTP, but the OAuth session was rejected while minting a WebSocket ticket. Open Settings → Gateway and sign in again."
        )
      )
    ).toBe(true)
    expect(
      isConnectionAuthRejection(
        new Error("Error invoking remote method 'hermes:connections:test': Error: 500: unavailable")
      )
    ).toBe(false)
    expect(
      isConnectionAuthRejection(
        new Error(
          "Error invoking remote method 'hermes:connections:test': Error: 500: upstream echoed: OAuth session was rejected while minting a WebSocket ticket"
        )
      )
    ).toBe(false)
    expect(isConnectionAuthRejection(new Error('Timed out connecting to Hermes backend after 8000ms'))).toBe(false)
  })
})

describe('dedupe helpers', () => {
  it('normalizes gateway URLs (trim, trailing slashes, lowercase)', () => {
    expect(normalizeGatewayUrl(' HTTP://Homelab.LAN:9119// ')).toBe('http://homelab.lan:9119')
  })

  it('normalizes ssh composites and defaults the port', () => {
    expect(sshCompositeKey('alice@Box')).toBe('alice@box:22')
    expect(sshCompositeKey('alice@box:22')).toBe('alice@box:22')
    expect(sshCompositeKey('box:2222')).toBe('@box:2222')
    expect(sshCompositeKey('  ')).toBe('')
  })

  it('finds at most one local entry', () => {
    expect(
      findDuplicateConnection({ host: '', id: null, kind: 'local', remoteProfile: '', url: '' }, registry.connections)
    ).toMatchObject({ id: 'local' })
    // Editing the local entry itself is not a self-collision.
    expect(
      findDuplicateConnection(
        { host: '', id: 'local', kind: 'local', remoteProfile: '', url: '' },
        registry.connections
      )
    ).toBeNull()
  })

  it('keys remote/cloud dupes on the normalized URL across both kinds', () => {
    expect(
      findDuplicateConnection(
        { host: '', id: null, kind: 'cloud', remoteProfile: '', url: 'http://HOMELAB.lan:9119/' },
        registry.connections
      )
    ).toMatchObject({ id: 'homelab' })
    expect(
      findDuplicateConnection(
        { host: '', id: null, kind: 'remote', remoteProfile: '', url: 'http://other.lan:9119' },
        registry.connections
      )
    ).toBeNull()
    // Editing the entry itself is not a self-collision.
    expect(
      findDuplicateConnection(
        { host: '', id: 'homelab', kind: 'remote', remoteProfile: '', url: 'http://homelab.lan:9119' },
        registry.connections
      )
    ).toBeNull()
  })

  it('keys ssh dupes on user@host:port + remote profile', () => {
    const connections = [
      ...registry.connections,
      {
        host: 'box',
        id: 'box',
        kind: 'ssh' as const,
        label: 'Box',
        port: 22,
        remoteProfile: 'work',
        tokenPreview: null,
        tokenSet: false,
        user: 'alice'
      }
    ]

    expect(
      findDuplicateConnection(
        { host: 'alice@box:22', id: null, kind: 'ssh', remoteProfile: 'work', url: '' },
        connections
      )
    ).toMatchObject({ id: 'box' })
    // Different profile on the same host is a distinct agent source.
    expect(
      findDuplicateConnection(
        { host: 'alice@box:22', id: null, kind: 'ssh', remoteProfile: 'other', url: '' },
        connections
      )
    ).toBeNull()
  })

  it('hints "Same backend as" only on later rows sharing an install_id', () => {
    const spark = { id: 'spark', installId: 'aaa', label: 'Spark' }
    const sparkTs = { id: 'spark-ts', installId: 'aaa', label: 'Spark TS' }
    const mini = { id: 'mini', installId: 'bbb', label: 'Mini' }
    const legacy = { id: 'old', label: 'Old box' }
    const connections = [spark, sparkTs, mini, legacy]

    // The first occurrence carries no hint; the later duplicate points back.
    expect(sameBackendPeerLabel(spark, connections)).toBeNull()
    expect(sameBackendPeerLabel(sparkTs, connections)).toBe('Spark')
    // Unique ids and id-less (older backend) rows never hint.
    expect(sameBackendPeerLabel(mini, connections)).toBeNull()
    expect(sameBackendPeerLabel(legacy, connections)).toBeNull()
  })
})
