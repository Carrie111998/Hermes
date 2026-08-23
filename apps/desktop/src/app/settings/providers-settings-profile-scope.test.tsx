import { act, cleanup, render } from '@testing-library/react'
import { atom } from 'nanostores'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $activeGatewayProfile } from '@/store/profile'
import { $settingsScopeOverride } from '@/store/settings-scope'

import type * as EnvCredentialsModule from './env-credentials'

// Regression for #92662: the Providers settings page saved API keys to the BASE
// profile even when a non-default profile was active, because it called
// `useEnvCredentials()` with no argument. The hook defaults `profile` to `null`,
// which capabilityScoped/profileScoped route to base (only `undefined` follows
// the active profile). The page must scope its credential reads/writes to the
// active (or "Applies to"-selected) profile — the concrete key resolved by
// `$settingsScopeProfile` — the same target the rest of profile-scoped settings use.
//
// This guards the call site: it asserts the profile the page hands to
// `useEnvCredentials` follows the active profile, so a future revert back to a
// bare `useEnvCredentials()` fails here.

const useEnvCredentials = vi.fn()
const listOAuthProviders = vi.fn()

vi.mock('./env-credentials', async importOriginal => ({
  ...(await importOriginal<typeof EnvCredentialsModule>()),
  useEnvCredentials: (profile?: null | string) => useEnvCredentials(profile)
}))

vi.mock('@/hermes', () => ({
  disconnectOAuthProvider: vi.fn(),
  listOAuthProviders: () => listOAuthProviders(),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@/store/onboarding', () => ({
  $desktopOnboarding: atom({ manual: false }),
  startManualLocalEndpoint: vi.fn(),
  startManualProviderOAuth: vi.fn()
}))

beforeEach(() => {
  listOAuthProviders.mockResolvedValue({ providers: [] })
  useEnvCredentials.mockReturnValue({
    rowProps: {
      edits: {},
      onClear: vi.fn(),
      onReveal: vi.fn(),
      onSave: vi.fn(),
      revealed: {},
      saving: null,
      setEdits: vi.fn()
    },
    saveValue: vi.fn(),
    vars: {}
  })
  $settingsScopeOverride.set(null)
})

afterEach(() => {
  cleanup()
  $activeGatewayProfile.set('default')
  $settingsScopeOverride.set(null)
  vi.clearAllMocks()
})

async function renderProvidersSettings() {
  const { ProvidersSettings } = await import('./providers-settings')
  await act(async () => {
    render(<ProvidersSettings onClose={vi.fn()} onViewChange={vi.fn()} view="keys" />)
  })
}

describe('ProvidersSettings profile scoping (#92662)', () => {
  it('scopes credentials to the active profile when one is selected', async () => {
    $activeGatewayProfile.set('foo')

    await renderProvidersSettings()

    // Must NOT be a bare/null call (that routed to base) — it must carry the
    // active profile so the key lands in that profile's .env.
    expect(useEnvCredentials).toHaveBeenCalledWith('foo')
  })

  it('honours the shared "Applies to" override over the active profile', async () => {
    $activeGatewayProfile.set('foo')
    $settingsScopeOverride.set('bar')

    await renderProvidersSettings()

    expect(useEnvCredentials).toHaveBeenCalledWith('bar')
  })

  it('resolves to a concrete key for single-profile users (base, no leak)', async () => {
    $activeGatewayProfile.set('default')

    await renderProvidersSettings()

    // 'default' resolves to the base home on the backend, so single-profile
    // users are unaffected while the call is never a bare null.
    expect(useEnvCredentials).toHaveBeenCalledWith('default')
  })
})
