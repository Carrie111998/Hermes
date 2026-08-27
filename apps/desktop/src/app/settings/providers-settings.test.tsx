import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { ConfirmHost } from '@/components/confirm-host'
import { $confirmRequest } from '@/store/confirm'
import type { EnvVarInfo, OAuthProvider } from '@/types/hermes'

const listOAuthProviders = vi.fn()

const getEnvVars = vi.fn()
const getCredentialPool = vi.fn()
const removeCredentialPoolEntry = vi.fn()
const renameCredentialPoolEntry = vi.fn()
const setCredentialPoolStrategy = vi.fn()
const startManualProviderOAuth = vi.fn()
const startManualLocalEndpoint = vi.fn()
const onboarding = atom({ manual: false })

vi.mock('@/hermes', () => ({

  getCredentialPool: () => getCredentialPool(),
  getEnvVars: () => getEnvVars(),
  getProfiles: () => Promise.resolve({ profiles: [{ id: 'default', name: 'Default', current: true }] }),
  listOAuthProviders: () => listOAuthProviders(),
  removeCredentialPoolEntry: (provider: string, id: string) => removeCredentialPoolEntry(provider, id),
  renameCredentialPoolEntry: (provider: string, id: string, label: string) => renameCredentialPoolEntry(provider, id, label),
  setCredentialPoolStrategy: (provider: string, strategy: string) => setCredentialPoolStrategy(provider, strategy),
  setApiRequestProfile: () => {}
}))

vi.mock('@/store/onboarding', () => ({
  $desktopOnboarding: onboarding,
  startManualProviderOAuth: (providerId: string, reason: null | string, profile?: string, label?: string) =>
    startManualProviderOAuth(providerId, reason, profile, label),
  startManualLocalEndpoint: (reason: null | string) => startManualLocalEndpoint(reason)
}))

function provider(id: string, loggedIn: boolean, patch: Partial<OAuthProvider> = {}): OAuthProvider {
  return {
    cli_command: `hermes auth add ${id}`,
    disconnectable: true,
    docs_url: '',
    flow: 'device_code',
    id,
    name: id === 'nous' ? 'Nous Portal' : 'MiniMax',
    status: {
      logged_in: loggedIn
    },
    ...patch
  }
}

// One `/api/env` row (an EnvVarInfo) for the API-keys view. Mirrors the
// `provider()` factory above: a valid base + per-test overrides, typed against
// the real response shape so it can't drift from EnvVarInfo.
function keyVar(patch: Partial<EnvVarInfo> = {}): EnvVarInfo {
  return {
    advanced: false,
    category: 'provider',
    description: '',
    is_password: true,
    is_set: false,
    provider: '',
    provider_label: '',
    redacted_value: null,
    tools: [],
    url: '',
    ...patch
  }
}

beforeEach(() => {
  onboarding.set({ manual: false })
  HTMLElement.prototype.scrollIntoView = vi.fn()
  getEnvVars.mockResolvedValue({})

  listOAuthProviders.mockResolvedValue({
      providers: [provider('nous', true), provider('minimax-oauth', false)]
    })
  getCredentialPool.mockResolvedValue({
    providers: [{
      provider: 'nous',
      entries: [{
        auth_type: 'oauth', has_refresh: true, id: 'personal', index: 1,
        label: 'Personal', last_status: null, priority: 0, request_count: 0,
        source: 'manual:device_code', token_preview: ''
      }]
    }],
    strategies: {}
  })
})

afterEach(() => {
  cleanup()
  $confirmRequest.set(null)
  vi.restoreAllMocks()
  vi.clearAllMocks()
})

// Removal goes through confirm() from @/store/confirm, so the host has to be
// mounted for the prompt to render — same as in the real app shell.
async function renderProvidersSettings() {
  const { ProvidersSettings } = await import('./providers-settings')
  let result: ReturnType<typeof render>
  await act(async () => {
    result = render(
      <>
        <ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="accounts" />
        <ConfirmHost />
      </>
    )
  })

  return result!
}

describe('ProvidersSettings', () => {
  it('removes a saved subscription account and refreshes the accounts list', async () => {
    await renderProvidersSettings()

    const remove = await screen.findByRole('button', { name: 'Remove Personal' })
    await act(async () => {
      fireEvent.click(remove)
    })

    // Removal is confirmed first — nothing has been deleted yet.
    expect(await screen.findByRole('dialog')).toBeTruthy()
    expect(removeCredentialPoolEntry).not.toHaveBeenCalled()

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Confirm' }))
    })

    await waitFor(() => expect(removeCredentialPoolEntry).toHaveBeenCalledWith('nous', 'personal'))
    expect(listOAuthProviders).toHaveBeenCalledTimes(2)
  })

  it('keeps the saved subscription when the removal prompt is dismissed', async () => {
    await renderProvidersSettings()

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Remove Personal' }))
    })

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Cancel' }))
    })

    expect(removeCredentialPoolEntry).not.toHaveBeenCalled()
  })

  it('adds another account only when the backend confirms independent dashboard persistence', async () => {
    listOAuthProviders.mockResolvedValue({
      providers: [provider('openai-codex', true, { supports_multiple_subscriptions: true })]
    })
    getCredentialPool.mockResolvedValue({
      providers: [{
        provider: 'openai-codex',
        entries: [{
          auth_type: 'oauth',
          has_refresh: true,
          id: 'personal',
          index: 1,
          label: 'Personal',
          last_status: null,
          priority: 0,
          request_count: 0,
          source: 'manual:device_code',
          token_preview: ''
        }]
      }],
      strategies: {}
    })
    await renderProvidersSettings()

    await act(async () => {
      fireEvent.click(await screen.findByRole('button', { name: 'Add another account' }))
    })

    // Picking a provider no longer starts OAuth immediately — it opens the
    // "name your account" prompt so multiple accounts can share one provider.
    const nameInput = await screen.findByLabelText('Account name')
    expect(nameInput).toBeTruthy()
    expect(startManualProviderOAuth).not.toHaveBeenCalled()

    await act(async () => {
      fireEvent.change(nameInput, { target: { value: 'Personal' } })
    })
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Continue' }))
    })

    expect(startManualProviderOAuth).toHaveBeenCalledTimes(1)
    const [id, , , label] = startManualProviderOAuth.mock.calls[0]
    expect(id).toBe('openai-codex')
    expect(label).toBe('Personal')
    expect(removeCredentialPoolEntry).not.toHaveBeenCalled()
  })

  it('does not offer another account for a singleton dashboard OAuth flow', async () => {
    await renderProvidersSettings()

    expect(screen.queryByRole('button', { name: 'Add another account' })).toBeNull()
  })

  it('shows the pool strategy on the provider head, not a separate top-level section', async () => {
    getCredentialPool.mockResolvedValue({
      providers: [{
        provider: 'openai-codex',
        entries: [
          {
            auth_type: 'oauth', has_refresh: true, id: 'personal', index: 1,
            label: 'Personal', last_status: null, priority: 0, request_count: 0,
            source: 'manual:device_code', token_preview: ''
          },
          {
            auth_type: 'oauth', has_refresh: true, id: 'work', index: 2,
            label: 'Work', last_status: null, priority: 1, request_count: 0,
            source: 'manual:device_code', token_preview: ''
          }
        ]
      }],
      strategies: { 'openai-codex': 'no_failover' }
    })

    await renderProvidersSettings()

    // Two subscriptions, ONE provider → exactly one provider head with the
    // strategy control inline (no longer a separate top-level PoolStrategyRows
    // section). The strategy label now lives under the provider head.
    expect(await screen.findByText('Subscriptions')).toBeTruthy()
    expect(screen.getAllByText('openai-codex')).toHaveLength(1)
    const select = screen.getByRole('combobox', { name: 'Pool strategy for openai-codex' })
    expect(select.textContent).toContain('No failover')

    await act(async () => {
      fireEvent.click(select)
    })
    await act(async () => {
      fireEvent.click(await screen.findByRole('option', { name: 'Round robin' }))
    })

    await waitFor(() => expect(setCredentialPoolStrategy).toHaveBeenCalledWith('openai-codex', 'round_robin'))
  })

  it('groups multiple subscriptions under a single provider head', async () => {
    getCredentialPool.mockResolvedValue({
      providers: [{
        provider: 'openai-codex',
        entries: [
          {
            auth_type: 'oauth', has_refresh: true, id: 'personal', index: 1,
            label: 'Personal', last_status: null, priority: 0, request_count: 0,
            source: 'manual:device_code', token_preview: ''
          },
          {
            auth_type: 'oauth', has_refresh: true, id: 'work', index: 2,
            label: 'Work', last_status: null, priority: 1, request_count: 0,
            source: 'manual:device_code', token_preview: ''
          }
        ]
      }],
      strategies: {}
    })

    await renderProvidersSettings()

    // Both subscriptions render under ONE provider head, not two.
    expect(screen.getAllByText('openai-codex')).toHaveLength(1)
    expect(screen.getByText('Personal')).toBeTruthy()
    expect(screen.getByText('Work')).toBeTruthy()
    // Strategy control lives on the provider head.
    expect(screen.getByRole('combobox', { name: 'Pool strategy for openai-codex' })).toBeTruthy()
  })

  it('does not offer removal for externally managed providers', async () => {
    listOAuthProviders.mockResolvedValue({
      providers: [
        provider('qwen-oauth', true, {
          cli_command: 'hermes auth add qwen-oauth',
          disconnect_hint: "Use `hermes auth add qwen-oauth` or that provider's CLI to remove it.",
          disconnectable: false,
          flow: 'external',
          name: 'Qwen (via Qwen CLI)'
        })
      ]
    })

    await renderProvidersSettings()

    expect(await screen.findByText('Qwen Code')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Remove Qwen Code' })).toBeNull()
    expect(screen.getByText(/managed by its own CLI/)).toBeTruthy()
  })

  it('renders a Keys card for a backend-tagged provider with no PROVIDER_GROUPS prefix', async () => {
    // A provider the backend catalog tags (provider/provider_label) but that has
    // no desktop PROVIDER_GROUPS prefix row must still render its own card —
    // this is the GUI/CLI drift fix: membership comes from the backend, not
    // from the hand-maintained prefix list.
    getEnvVars.mockResolvedValue({
      WIDGETAI_API_KEY: keyVar({
        provider: 'widgetai',
        provider_label: 'WidgetAI',
        url: 'https://widgetai.example/keys'
      })
    })
    listOAuthProviders.mockResolvedValue({ providers: [] })

    const { ProvidersSettings } = await import('./providers-settings')
    await act(async () => {
      render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)
    })

    expect(await screen.findByText('WidgetAI')).toBeTruthy()
  })

  it('orders API-key providers by priority then name, and filters them via search', async () => {
    // These three providers have no curated PROVIDER_GROUPS priority, so they
    // share the default priority and fall back to alphabetical among themselves
    // (Acme, Middle, Zebra) — exercising the name tiebreak of the priority sort.
    getEnvVars.mockResolvedValue({
      ZEBRA_API_KEY: keyVar({ provider: 'zebra', provider_label: 'Zebra' }),
      ACME_API_KEY: keyVar({ provider: 'acme', provider_label: 'Acme' }),
      MIDDLE_API_KEY: keyVar({ provider: 'middle', provider_label: 'Middle' })
    })
    listOAuthProviders.mockResolvedValue({ providers: [] })

    const { ProvidersSettings } = await import('./providers-settings')
    render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)

    // Equal priority → alphabetical tiebreak: Acme, Middle, Zebra.
    await screen.findByText('Acme')
    const labels = screen.getAllByText(/Acme|Middle|Zebra/).map(el => el.textContent)
    expect(labels).toEqual(['Acme', 'Middle', 'Zebra'])

    // Typing narrows the list to matching providers only.
    const search = screen.getByPlaceholderText('Search providers…')
    await act(async () => {
      fireEvent.change(search, { target: { value: 'mid' } })
    })

    await waitFor(() => expect(screen.queryByText('Acme')).toBeNull())
    expect(screen.getByText('Middle')).toBeTruthy()
    expect(screen.queryByText('Zebra')).toBeNull()

    // A non-matching query shows the empty-state copy.
    await act(async () => {
      fireEvent.change(search, { target: { value: 'nonesuch-xyz' } })
    })
    expect(await screen.findByText('No providers match your search.')).toBeTruthy()
  })

  it('offers a Local / custom endpoint entry in the API-keys tab that opens the custom-endpoint flow', async () => {
    // Regression: the composer pill and the providers "have an API key"
    // affordance both dead-end on the env-var-driven key catalog, which never
    // lists a custom endpoint — so without this row there is no reachable
    // Desktop GUI path to add one. See issue #62817.
    getEnvVars.mockResolvedValue({})
    listOAuthProviders.mockResolvedValue({ providers: [] })

    const { ProvidersSettings } = await import('./providers-settings')
    render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)

    const row = await screen.findByText('Local / custom endpoint')
    expect(screen.getByText(/OpenAI-compatible endpoint/)).toBeTruthy()

    fireEvent.click(row)

    await waitFor(() => expect(startManualLocalEndpoint).toHaveBeenCalledWith(null))
  })

  it('hides the pool strategy control when a provider has only one subscription', async () => {
    getCredentialPool.mockResolvedValue({
      providers: [{
        provider: 'openai-codex',
        entries: [
          {
            auth_type: 'oauth', has_refresh: true, id: 'personal', index: 1,
            label: 'Personal', last_status: null, priority: 0, request_count: 0,
            source: 'manual:device_code', token_preview: ''
          }
        ]
      }],
      strategies: { 'openai-codex': 'no_failover' }
    })

    await renderProvidersSettings()

    // A single subscription has no pool to strategize — the control is hidden.
    expect(screen.queryByRole('combobox', { name: 'Pool strategy for openai-codex' })).toBeNull()
    expect(screen.getByText('Personal')).toBeTruthy()
  })
})
