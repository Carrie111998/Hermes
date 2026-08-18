import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

// Radix Select calls scrollIntoView on its items when the content opens; jsdom
// doesn't implement it (nor hasPointerCapture / releasePointerCapture), so stub
// them to let the dropdown open in tests.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

const getGlobalModelInfo = vi.fn()
const getGlobalModelOptions = vi.fn()
const getAuxiliaryModels = vi.fn()
const getMoaModels = vi.fn()
const getOpenRouterEndpoints = vi.fn()
const setModelAssignment = vi.fn()
const getRecommendedDefaultModel = vi.fn()
const saveMoaModels = vi.fn()
const setEnvVar = vi.fn()
const getHermesConfigRecord = vi.fn()
const saveHermesConfig = vi.fn()
const startManualLocalEndpoint = vi.fn()
const startManualOnboarding = vi.fn()
const startManualProviderOAuth = vi.fn()
let profileSwitchHandler: (() => void) | null = null

vi.mock('@/hermes', () => ({
  getGlobalModelInfo: () => getGlobalModelInfo(),
  getGlobalModelOptions: () => getGlobalModelOptions(),
  getAuxiliaryModels: () => getAuxiliaryModels(),
  getApiRequestProfile: () => 'default',
  getMoaModels: () => getMoaModels(),
  getOpenRouterEndpoints: (model: string, opts?: unknown) =>
    opts === undefined ? getOpenRouterEndpoints(model) : getOpenRouterEndpoints(model, opts),
  setModelAssignment: (body: unknown) => setModelAssignment(body),
  getRecommendedDefaultModel: (slug: string) => getRecommendedDefaultModel(slug),
  saveMoaModels: (body: unknown) => saveMoaModels(body),
  setEnvVar: (key: string, value: string) => setEnvVar(key, value),
  getHermesConfigRecord: (profile?: string | null) => getHermesConfigRecord(profile),
  saveHermesConfig: (config: unknown) => saveHermesConfig(config),
  setApiRequestProfile: () => {}
}))

vi.mock('@/store/onboarding', () => ({
  startManualLocalEndpoint: () => startManualLocalEndpoint(),
  startManualOnboarding: () => startManualOnboarding(),
  startManualProviderOAuth: (slug: string) => startManualProviderOAuth(slug)
}))

vi.mock('../hooks/use-on-profile-switch', () => ({
  useOnProfileSwitch: (handler: () => void) => {
    profileSwitchHandler = handler
  }
}))

beforeEach(() => {
  getGlobalModelInfo.mockResolvedValue({ provider: 'nous', model: 'hermes-4' })
  getGlobalModelOptions.mockResolvedValue({
    providers: [
      {
        name: 'Nous',
        slug: 'nous',
        models: ['hermes-4', 'hermes-4-mini'],
        authenticated: true,
        capabilities: { 'hermes-4': { reasoning: true, fast: true } }
      }
    ]
  })
  getAuxiliaryModels.mockResolvedValue({
    main: { provider: 'nous', model: 'hermes-4' },
    tasks: [{ task: 'vision', provider: 'auto', model: '', base_url: '' }]
  })
  getMoaModels.mockResolvedValue(null)
  getOpenRouterEndpoints.mockResolvedValue({ model: '', endpoints: [], cached: false })
  setModelAssignment.mockResolvedValue({ ok: true, provider: 'nous', model: 'hermes-4', gateway_tools: [] })
  getRecommendedDefaultModel.mockResolvedValue({ provider: 'nous', model: 'hermes-4', free_tier: null })
  setEnvVar.mockResolvedValue({ ok: true })
  getHermesConfigRecord.mockResolvedValue({ agent: { reasoning_effort: 'medium', service_tier: 'normal' } })
  saveHermesConfig.mockResolvedValue({ ok: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  profileSwitchHandler = null
})

async function renderModelSettings() {
  const { ModelSettings } = await import('./model-settings')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(
    // The aux-task deep-link highlight reads useSearchParams, so the page
    // needs a router context in tests (the app provides HashRouter at root).
    <MemoryRouter>
      <QueryClientProvider client={client}>
        <ModelSettings />
      </QueryClientProvider>
    </MemoryRouter>
  )
}

function setupOpenRouter(overrides: Record<string, unknown> = {}) {
  getGlobalModelInfo.mockResolvedValue({ provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' })
  getGlobalModelOptions.mockResolvedValue({
    providers: [
      {
        name: 'OpenRouter',
        slug: 'openrouter',
        models: ['deepseek/deepseek-v4-flash', 'openai/gpt-5.4'],
        authenticated: true
      }
    ]
  })
  getAuxiliaryModels.mockResolvedValue({
    main: { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' },
    tasks: []
  })
  getHermesConfigRecord.mockResolvedValue({
    agent: { reasoning_effort: 'medium', service_tier: 'normal' },
    provider_routing: { model_overrides: { openrouter: {} } },
    ...overrides
  })
  getOpenRouterEndpoints.mockResolvedValue({
    model: 'deepseek/deepseek-v4-flash',
    endpoints: [
      { provider_name: 'Baidu Qianfan', tag: 'baidu/fp8', quantization: 'fp8', status: 0 },
      { provider_name: 'DigitalOcean', tag: 'digitalocean', quantization: 'unknown', status: 0 }
    ],
    cached: false
  })
  setModelAssignment.mockResolvedValue({
    ok: true,
    provider: 'openrouter',
    model: 'deepseek/deepseek-v4-flash',
    gateway_tools: []
  })
}

describe('ModelSettings', () => {
  it('hides OpenRouter routing controls for non-OpenRouter providers', async () => {
    await renderModelSettings()

    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())
    expect(screen.queryByText('OpenRouter route')).toBeNull()
    expect(getOpenRouterEndpoints).not.toHaveBeenCalled()
  })

  it.each([
    ['openrouter', true],
    ['OpenRouter', true],
    ['OPENROUTER', true],
    ['custom:openrouter', false],
    ['anthropic', false]
  ])('gates the OpenRouter model typeahead on provider slug %s (expected visible=%s)', async (slug, expectVisible) => {
    getGlobalModelInfo.mockResolvedValue({ provider: slug, model: 'deepseek/deepseek-v4-flash' })
    getGlobalModelOptions.mockResolvedValue({
      providers: [
        {
          name: 'OpenRouter',
          slug,
          models: ['deepseek/deepseek-v4-flash', 'openai/gpt-5.4'],
          authenticated: true
        }
      ]
    })
    getHermesConfigRecord.mockResolvedValue({
      agent: { reasoning_effort: 'medium', service_tier: 'normal' },
      provider_routing: { model_overrides: { openrouter: {} } }
    })
    getOpenRouterEndpoints.mockResolvedValue({ model: 'deepseek/deepseek-v4-flash', endpoints: [], cached: false })

    await renderModelSettings()
    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())

    if (expectVisible) {
      expect(await screen.findByRole('combobox', { name: 'OpenRouter model' })).toBeTruthy()
    } else {
      expect(screen.queryByRole('combobox', { name: 'OpenRouter model' })).toBeNull()
    }
  })

  it('lets OpenRouter users type a custom model and discovers endpoints for it', async () => {
    setupOpenRouter()
    await renderModelSettings()

    const input = await screen.findByRole('combobox', { name: 'OpenRouter model' })
    fireEvent.change(input, { target: { value: 'meta-llama/llama-3.3-70b-instruct:free' } })

    await waitFor(() => expect(getOpenRouterEndpoints).toHaveBeenCalledWith('meta-llama/llama-3.3-70b-instruct:free'))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))
    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith(
        expect.objectContaining({ model: 'meta-llama/llama-3.3-70b-instruct:free', provider: 'openrouter' })
      )
    )
  })

  it('filters OpenRouter model suggestions while preserving free text', async () => {
    setupOpenRouter()
    await renderModelSettings()

    const input = await screen.findByRole('combobox', { name: 'OpenRouter model' })
    fireEvent.focus(input)
    fireEvent.change(input, { target: { value: 'gpt-5' } })

    expect(await screen.findByRole('option', { name: 'openai/gpt-5.4' })).toBeTruthy()
    expect(screen.queryByRole('option', { name: 'deepseek/deepseek-v4-flash' })).toBeNull()
  })

  it('shows a non-blocking shape hint for invalid OpenRouter model text', async () => {
    setupOpenRouter()
    await renderModelSettings()

    fireEvent.change(await screen.findByRole('combobox', { name: 'OpenRouter model' }), {
      target: { value: 'not-a-model' }
    })

    expect(await screen.findByText(/author\/slug/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('keeps the closed model select for non-OpenRouter providers', async () => {
    await renderModelSettings()

    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())
    expect(screen.queryByRole('combobox', { name: 'OpenRouter model' })).toBeNull()
    expect(screen.getAllByRole('combobox')[1].tagName).toBe('BUTTON')
  })

  it('discovers and displays endpoints for the selected OpenRouter model', async () => {
    setupOpenRouter()
    await renderModelSettings()

    expect(await screen.findByText('OpenRouter route')).toBeTruthy()
    await waitFor(() => expect(getOpenRouterEndpoints).toHaveBeenCalledWith('deepseek/deepseek-v4-flash'))
    expect(await screen.findByText(/Baidu Qianfan/)).toBeTruthy()
  })

  it('persists a selected endpoint as an exclusive route by default', async () => {
    setupOpenRouter()
    await renderModelSettings()

    fireEvent.click(await screen.findByRole('radio', { name: /Baidu Qianfan/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())
    const saved = saveHermesConfig.mock.calls.at(-1)?.[0] as any
    expect(saved.provider_routing.model_overrides.openrouter['deepseek/deepseek-v4-flash']).toEqual({
      only: ['baidu/fp8'],
      quantizations: ['fp8'],
      allow_fallbacks: false
    })
    expect(screen.getByText('Requests use Baidu Qianfan (fp8) only.')).toBeTruthy()
  })

  it('persists a selected endpoint as preferred when fallback is checked', async () => {
    setupOpenRouter()
    await renderModelSettings()

    fireEvent.click(await screen.findByRole('radio', { name: /DigitalOcean/ }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'Allow fallback providers' }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())
    const saved = saveHermesConfig.mock.calls.at(-1)?.[0] as any
    expect(saved.provider_routing.model_overrides.openrouter['deepseek/deepseek-v4-flash']).toEqual({
      order: ['digitalocean'],
      quantizations: ['unknown'],
      allow_fallbacks: true
    })
  })

  it('Automatic clears a saved selected endpoint', async () => {
    setupOpenRouter({
      provider_routing: {
        model_overrides: {
          openrouter: {
            'deepseek/deepseek-v4-flash': {
              only: ['baidu/fp8'],
              quantizations: ['fp8'],
              allow_fallbacks: false
            }
          }
        }
      }
    })
    await renderModelSettings()

    fireEvent.click(await screen.findByRole('radio', { name: /Automatic/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())
    const saved = saveHermesConfig.mock.calls.at(-1)?.[0] as any
    expect(saved.provider_routing.model_overrides.openrouter['deepseek/deepseek-v4-flash']).toBeUndefined()
  })

  it('keeps blocked rows visible with accessible destructive state and persists ignore', async () => {
    setupOpenRouter()
    await renderModelSettings()

    const block = await screen.findByRole('button', { name: /Block DigitalOcean/ })
    fireEvent.click(block)

    expect(screen.getByText('DigitalOcean')).toBeTruthy()
    expect(screen.getByText('Blocked')).toBeTruthy()
    expect(screen.getByRole('button', { name: /Unblock DigitalOcean/ }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())
    const saved = saveHermesConfig.mock.calls.at(-1)?.[0] as any
    expect(saved.provider_routing.model_overrides.openrouter['deepseek/deepseek-v4-flash']).toEqual({
      ignore: ['digitalocean']
    })
  })

  it('prevents a selected provider from remaining selected when it is blocked', async () => {
    setupOpenRouter()
    await renderModelSettings()

    fireEvent.click(await screen.findByRole('radio', { name: /Baidu Qianfan/ }))
    fireEvent.click(screen.getByRole('button', { name: /Block Baidu Qianfan/ }))

    expect(screen.getByRole('radio', { name: /Automatic/ }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('button', { name: /Unblock Baidu Qianfan/ }).getAttribute('aria-pressed')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())

    const override = (saveHermesConfig.mock.calls.at(-1)?.[0] as any).provider_routing.model_overrides.openrouter[
      'deepseek/deepseek-v4-flash'
    ]

    expect(override).toEqual({ ignore: ['baidu/fp8'] })
    expect(override.only).toBeUndefined()
    expect(override.order).toBeUndefined()
  })

  it('normalizes a padded manual tag before blocking its selected row', async () => {
    setupOpenRouter()
    await renderModelSettings()

    fireEvent.click(await screen.findByRole('radio', { name: /Baidu Qianfan/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Enter provider tag manually' }))
    const tagInput = screen.getByRole('textbox', { name: 'Provider tag' }) as HTMLInputElement
    fireEvent.change(tagInput, { target: { value: '  baidu/fp8  ' } })

    expect(tagInput.value).toBe('baidu/fp8')
    fireEvent.click(screen.getByRole('button', { name: /Block Baidu Qianfan/ }))

    expect(screen.getByRole('radio', { name: /Automatic/ }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('radio', { name: /Baidu Qianfan/ }).getAttribute('aria-checked')).toBe('false')
    expect(screen.getByRole('button', { name: /Unblock Baidu Qianfan/ }).getAttribute('aria-pressed')).toBe('true')
  })

  it('selecting a blocked row selects it and removes its blocked state', async () => {
    setupOpenRouter()
    await renderModelSettings()

    fireEvent.click(await screen.findByRole('button', { name: /Block DigitalOcean/ }))
    fireEvent.click(screen.getByRole('radio', { name: /DigitalOcean/ }))

    expect(screen.getByRole('radio', { name: /DigitalOcean/ }).getAttribute('aria-checked')).toBe('true')
    expect(screen.getByRole('button', { name: /Block DigitalOcean/ }).getAttribute('aria-pressed')).toBe('false')
  })

  it('shows every discovered endpoint instead of truncating after four', async () => {
    setupOpenRouter()
    getOpenRouterEndpoints.mockResolvedValue({
      model: 'deepseek/deepseek-v4-flash',
      endpoints: Array.from({ length: 6 }, (_, index) => ({
        provider_name: `Provider ${index + 1}`,
        tag: `provider-${index + 1}`,
        quantization: 'fp8',
        status: 0
      }))
    })
    await renderModelSettings()

    expect(await screen.findByRole('radio', { name: /Provider 6/ })).toBeTruthy()
  })

  it('never persists an empty only or order from manual routing', async () => {
    setupOpenRouter({
      provider_routing: {
        model_overrides: { openrouter: { 'deepseek/deepseek-v4-flash': { only: [] } } }
      }
    })
    getOpenRouterEndpoints.mockRejectedValue(new Error('offline'))
    await renderModelSettings()

    fireEvent.click(await screen.findByRole('button', { name: 'Enter provider tag manually' }))
    fireEvent.change(screen.getByRole('textbox', { name: 'Provider tag' }), { target: { value: '   ' } })
    expect(screen.getByRole('radio', { name: /Automatic/ }).getAttribute('aria-checked')).toBe('true')
    expect(screen.queryByRole('checkbox', { name: 'Allow fallback providers' })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())
    const saved = saveHermesConfig.mock.calls.at(-1)?.[0] as any
    expect(saved.provider_routing.model_overrides.openrouter['deepseek/deepseek-v4-flash']).toBeUndefined()
  })

  it('ignores a stale endpoint response after the selected model changes', async () => {
    setupOpenRouter()
    let resolveOld!: (value: unknown) => void

    const oldResponse = new Promise(resolve => {
      resolveOld = resolve
    })

    getOpenRouterEndpoints.mockImplementation((model: string) =>
      model === 'deepseek/deepseek-v4-flash'
        ? oldResponse
        : Promise.resolve({
            model,
            endpoints: [{ provider_name: 'New Endpoint', tag: 'new', quantization: 'fp8' }]
          })
    )
    await renderModelSettings()
    await waitFor(() => expect(getOpenRouterEndpoints).toHaveBeenCalledWith('deepseek/deepseek-v4-flash'))

    fireEvent.change(screen.getByRole('combobox', { name: 'OpenRouter model' }), {
      target: { value: 'openai/gpt-5.4' }
    })
    expect(await screen.findByText(/New Endpoint/)).toBeTruthy()

    await act(async () => {
      resolveOld({
        model: 'deepseek/deepseek-v4-flash',
        endpoints: [{ provider_name: 'Stale Endpoint', tag: 'stale', quantization: 'fp8' }]
      })
    })
    expect(screen.queryByText(/Stale Endpoint/)).toBeNull()
  })

  it('ignores endpoint responses from the previous profile epoch', async () => {
    setupOpenRouter()
    let resolveOld!: (value: unknown) => void
    getOpenRouterEndpoints
      .mockImplementationOnce(
        () =>
          new Promise(resolve => {
            resolveOld = resolve
          })
      )
      .mockResolvedValueOnce({
        model: 'openai/gpt-5.4',
        endpoints: [{ provider_name: 'Profile B Endpoint', tag: 'profile-b', quantization: 'fp8' }]
      })
    await renderModelSettings()
    await waitFor(() => expect(getOpenRouterEndpoints).toHaveBeenCalledTimes(1))

    getGlobalModelInfo.mockResolvedValue({ provider: 'openrouter', model: 'openai/gpt-5.4' })
    await act(async () => profileSwitchHandler?.())
    expect(await screen.findByText(/Profile B Endpoint/)).toBeTruthy()

    await act(async () => {
      resolveOld({
        model: 'deepseek/deepseek-v4-flash',
        endpoints: [{ provider_name: 'Profile A Stale', tag: 'profile-a', quantization: 'fp8' }]
      })
    })
    expect(screen.queryByText(/Profile A Stale/)).toBeNull()
  })

  it('keeps model selection usable and exposes manual routing when discovery fails', async () => {
    setupOpenRouter()
    getOpenRouterEndpoints.mockRejectedValue(new Error('discovery offline'))
    await renderModelSettings()

    expect(await screen.findByText(/Endpoint discovery failed/)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Apply' }) as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(screen.getByRole('button', { name: 'Enter provider tag manually' }))
    expect(screen.getByRole('textbox', { name: 'Provider tag' })).toBeTruthy()
    expect(screen.getByRole('textbox', { name: 'Quantization' })).toBeTruthy()
  })

  it('rolls the optimistic routing config back when save fails', async () => {
    setupOpenRouter()
    saveHermesConfig.mockRejectedValueOnce(new Error('route save failed'))
    await renderModelSettings()

    fireEvent.click(await screen.findByRole('radio', { name: /Baidu Qianfan/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    expect(await screen.findByText('route save failed')).toBeTruthy()
    expect(saveHermesConfig).toHaveBeenCalledTimes(1)
  })

  it('re-fetches the authoritative config after the model assignment lands before saving routing, so a concurrent write cannot be clobbered', async () => {
    setupOpenRouter()
    // The query's initial load sees a STALE record (as if fetched before some
    // other write landed server-side). After setMainModelAssignment resolves,
    // applyMainModel must re-fetch the AUTHORITATIVE record and apply the
    // routing delta to THAT — never to the stale snapshot captured at click time.
    getHermesConfigRecord
      .mockReset()
      .mockResolvedValueOnce({
        agent: { reasoning_effort: 'medium', service_tier: 'normal' },
        provider_routing: { model_overrides: { openrouter: {} } },
        _sentinel: 'stale-before-assignment'
      })
      .mockResolvedValueOnce({
        agent: { reasoning_effort: 'medium', service_tier: 'normal' },
        provider_routing: { model_overrides: { openrouter: {} } },
        _sentinel: 'fresh-after-assignment'
      })
    await renderModelSettings()

    fireEvent.click(await screen.findByRole('radio', { name: /Baidu Qianfan/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())
    const saved = saveHermesConfig.mock.calls.at(-1)?.[0] as any

    // The saved record must be built from the FRESH re-fetch, not the stale
    // snapshot the component held when Apply was clicked.
    expect(saved._sentinel).toBe('fresh-after-assignment')
    // The routing delta the user actually selected must still be present on
    // top of that fresh record.
    expect(saved.provider_routing.model_overrides.openrouter['deepseek/deepseek-v4-flash']).toEqual({
      only: ['baidu/fp8'],
      quantizations: ['fp8'],
      allow_fallbacks: false
    })
  })

  it('discards a stale apply when the profile is switched mid-flight, never writing or repainting for the old profile', async () => {
    setupOpenRouter()
    // Hold setMainModelAssignment open so we can bump the profile epoch
    // while the apply is still in flight, simulating a slow response from
    // profile A arriving after the user has already switched to profile B.
    let resolveAssignment!: (value: { ok: true; provider: string; model: string; gateway_tools: never[] }) => void
    setModelAssignment.mockReset().mockImplementationOnce(
      () =>
        new Promise(resolve => {
          resolveAssignment = resolve
        })
    )

    await renderModelSettings()
    fireEvent.click(await screen.findByRole('radio', { name: /Baidu Qianfan/ }))

    const configFetchCountBeforeApply = getHermesConfigRecord.mock.calls.length
    fireEvent.click(screen.getByRole('button', { name: 'Apply' }))

    // Profile switch lands BEFORE the in-flight assignment resolves.
    await act(async () => {
      profileSwitchHandler?.()
    })

    await act(async () => {
      resolveAssignment({
        ok: true,
        provider: 'openrouter',
        model: 'deepseek/deepseek-v4-flash',
        gateway_tools: []
      })
      await Promise.resolve()
      await Promise.resolve()
    })

    // The stale response landing after the switch must not re-fetch config
    // (the epoch guard should short-circuit before the re-fetch) or save
    // routing for the profile that is no longer active.
    expect(getHermesConfigRecord.mock.calls.length).toBe(configFetchCountBeforeApply)
    expect(saveHermesConfig).not.toHaveBeenCalled()
  })

  it('loads the current main model and lists configured providers only', async () => {
    await renderModelSettings()

    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())
    await waitFor(() => expect(getGlobalModelOptions).toHaveBeenCalled())

    // Open the provider Select — only configured providers should be listed.
    const triggers = await screen.findAllByRole('combobox')
    fireEvent.click(triggers[0])

    // "Nous" shows in both the trigger and the open list.
    expect((await screen.findAllByText('Nous')).length).toBeGreaterThan(0)
    expect(screen.queryByText(/DeepSeek/)).toBeNull()
  })

  it.each(['custom', 'local', 'custom:lab'])(
    'opens local endpoint setup when %s has no inventory row',
    async provider => {
      getGlobalModelInfo.mockResolvedValueOnce({ provider, model: '' })
      getGlobalModelOptions.mockResolvedValueOnce({ providers: [] })

      await renderModelSettings()

      const providerSelect = (await screen.findAllByRole('combobox'))[0]

      expect(providerSelect.textContent).toContain(provider)
      expect(screen.queryByText(/undefined/)).toBeNull()
      expect(screen.queryByText(/signs in through your browser/)).toBeNull()

      fireEvent.click(await screen.findByRole('button', { name: 'Set up provider' }))

      expect(startManualLocalEndpoint).toHaveBeenCalledOnce()
      expect(startManualOnboarding).not.toHaveBeenCalled()
      expect(startManualProviderOAuth).not.toHaveBeenCalled()
    }
  )

  it('opens the generic provider picker for an unknown provider with no inventory row', async () => {
    getGlobalModelInfo.mockResolvedValueOnce({ provider: 'retired-provider', model: '' })
    getGlobalModelOptions.mockResolvedValueOnce({ providers: [] })

    await renderModelSettings()

    fireEvent.click(await screen.findByRole('button', { name: 'Set up provider' }))

    expect(startManualOnboarding).toHaveBeenCalledOnce()
    expect(startManualLocalEndpoint).not.toHaveBeenCalled()
    expect(startManualProviderOAuth).not.toHaveBeenCalled()
  })

  it('deep-links a known OAuth provider row into its setup flow', async () => {
    getGlobalModelInfo.mockResolvedValueOnce({ provider: 'anthropic', model: '' })
    getGlobalModelOptions.mockResolvedValueOnce({
      providers: [
        {
          name: 'Anthropic',
          slug: 'anthropic',
          models: [],
          authenticated: false,
          auth_type: 'oauth'
        }
      ]
    })

    await renderModelSettings()

    fireEvent.click(await screen.findByRole('button', { name: 'Set up Anthropic' }))

    expect(startManualProviderOAuth).toHaveBeenCalledWith('anthropic')
    expect(startManualLocalEndpoint).not.toHaveBeenCalled()
    expect(startManualOnboarding).not.toHaveBeenCalled()
  })

  it('replaces the selected provider and model when the active profile changes', async () => {
    getGlobalModelInfo
      .mockResolvedValueOnce({ provider: 'custom', model: 'local-a' })
      .mockResolvedValueOnce({ provider: 'nous', model: 'hermes-4' })
    getGlobalModelOptions
      .mockResolvedValueOnce({
        providers: [
          {
            name: 'Custom A',
            slug: 'custom',
            models: ['local-a'],
            authenticated: true
          }
        ]
      })
      .mockResolvedValueOnce({
        providers: [
          {
            name: 'Nous',
            slug: 'nous',
            models: ['hermes-4'],
            authenticated: true,
            capabilities: { 'hermes-4': { reasoning: true, fast: true } }
          }
        ]
      })

    await renderModelSettings()
    expect((await screen.findAllByRole('combobox'))[0].textContent).toContain('Custom A')

    await act(async () => {
      profileSwitchHandler?.()
    })

    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalledTimes(2))
    await waitFor(() => expect(screen.getAllByRole('combobox')[0].textContent).toContain('Nous'))
    expect(screen.queryByRole('button', { name: 'Set up provider' })).toBeNull()
  })

  it('preserves a user-defined provider endpoint when applying the main model', async () => {
    getGlobalModelOptions.mockResolvedValueOnce({
      providers: [
        {
          name: 'Nous',
          slug: 'nous',
          models: ['hermes-4'],
          authenticated: true
        },
        {
          name: 'Ollama',
          slug: 'local-ollama',
          models: ['qwen3:latest'],
          authenticated: true,
          is_user_defined: true,
          api_url: 'http://localhost:11434/v1'
        }
      ]
    })
    setModelAssignment.mockResolvedValueOnce({
      ok: true,
      provider: 'local-ollama',
      model: 'qwen3:latest',
      gateway_tools: []
    })

    await renderModelSettings()

    const providerSelect = (await screen.findAllByRole('combobox'))[0]
    fireEvent.click(providerSelect)
    fireEvent.click(await screen.findByRole('option', { name: 'Ollama' }))

    const modelSelect = (await screen.findAllByRole('combobox'))[1]
    fireEvent.click(modelSelect)
    fireEvent.click(await screen.findByRole('option', { name: 'qwen3:latest' }))

    fireEvent.click(await screen.findByRole('button', { name: 'Apply' }))

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith({
        model: 'qwen3:latest',
        provider: 'local-ollama',
        scope: 'main',
        base_url: 'http://localhost:11434/v1'
      })
    )
  })

  it('writes the profile default speed (service_tier) when the fast switch is toggled', async () => {
    await renderModelSettings()
    await waitFor(() => expect(getHermesConfigRecord).toHaveBeenCalled())

    const fastSwitch = await screen.findByRole('switch')
    fireEvent.click(fastSwitch)

    await waitFor(() =>
      expect(saveHermesConfig).toHaveBeenCalledWith(
        expect.objectContaining({ agent: expect.objectContaining({ service_tier: 'fast' }) })
      )
    )
  })

  it('hides the reasoning/speed defaults when the main model reports no capabilities', async () => {
    getGlobalModelOptions.mockResolvedValueOnce({
      providers: [
        {
          name: 'Nous',
          slug: 'nous',
          models: ['hermes-4'],
          authenticated: true,
          capabilities: { 'hermes-4': { reasoning: false, fast: false } }
        }
      ]
    })

    await renderModelSettings()
    await waitFor(() => expect(getHermesConfigRecord).toHaveBeenCalled())

    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('renders the auxiliary task rows', async () => {
    await renderModelSettings()

    expect(await screen.findByText('Vision')).toBeTruthy()
    expect(screen.getAllByText('auto · use main model').length).toBeGreaterThan(0)
  })

  it('assigns an auxiliary task to the main model via setModelAssignment', async () => {
    await renderModelSettings()

    // One "Set to main" button per task slot; the first is Vision.
    const setToMainButtons = await screen.findAllByRole('button', { name: 'Set to main' })
    fireEvent.click(setToMainButtons[0])

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith({
        model: 'hermes-4',
        provider: 'nous',
        scope: 'auxiliary',
        task: 'vision'
      })
    )
  })

  it('carries the user-defined endpoint when an aux slot is set to a local main model', async () => {
    getGlobalModelOptions.mockResolvedValueOnce({
      providers: [
        {
          name: 'Ollama',
          slug: 'local-ollama',
          models: ['qwen3:latest'],
          authenticated: true,
          is_user_defined: true,
          api_url: 'http://localhost:11434/v1'
        }
      ]
    })
    getGlobalModelInfo.mockResolvedValueOnce({ provider: 'local-ollama', model: 'qwen3:latest' })
    getAuxiliaryModels.mockResolvedValueOnce({
      main: { provider: 'local-ollama', model: 'qwen3:latest' },
      tasks: [{ task: 'vision', provider: 'auto', model: '', base_url: '' }]
    })

    await renderModelSettings()

    const setToMainButtons = await screen.findAllByRole('button', { name: 'Set to main' })
    fireEvent.click(setToMainButtons[0])

    await waitFor(() =>
      expect(setModelAssignment).toHaveBeenCalledWith({
        model: 'qwen3:latest',
        provider: 'local-ollama',
        scope: 'auxiliary',
        task: 'vision',
        base_url: 'http://localhost:11434/v1'
      })
    )
  })

  it('warns when a main switch leaves auxiliary tasks pinned to another provider', async () => {
    setModelAssignment.mockResolvedValueOnce({
      ok: true,
      provider: 'openrouter',
      model: 'anthropic/claude-opus-4.7',
      gateway_tools: [],
      stale_aux: [{ task: 'compression', provider: 'nous', model: 'hermes-4' }]
    })

    await renderModelSettings()
    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())

    const applyButton = await screen.findByRole('button', { name: 'Apply' })
    fireEvent.click(applyButton)

    // The switch-time notice names the pinned provider and offers a reset.
    expect(await screen.findByText(/still run on/)).toBeTruthy()
    expect(screen.getByText('nous')).toBeTruthy()
  })

  it('shows a persistent banner when a loaded aux slot mismatches the main provider', async () => {
    getAuxiliaryModels.mockResolvedValueOnce({
      main: { provider: 'nous', model: 'hermes-4' },
      tasks: [{ task: 'curator', provider: 'openrouter', model: 'anthropic/claude-opus-4.7', base_url: '' }]
    })

    await renderModelSettings()

    // Banner present on load, no switch required.
    expect(await screen.findByText(/still run on/)).toBeTruthy()
  })
})

describe('ModelSettings MoA preset editor', () => {
  const moaConfig = () => ({
    default_preset: 'default',
    active_preset: '',
    presets: {
      default: {
        reference_models: [
          { provider: 'nous', model: 'hermes-4' },
          { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' }
        ],
        aggregator: { provider: 'openrouter', model: 'anthropic/claude-opus-4.8' },
        reference_temperature: 0,
        aggregator_temperature: 0,
        max_tokens: 4096,
        enabled: true
      }
    },
    reference_models: [
      { provider: 'nous', model: 'hermes-4' },
      { provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' }
    ],
    aggregator: { provider: 'openrouter', model: 'anthropic/claude-opus-4.8' },
    reference_temperature: 0,
    aggregator_temperature: 0,
    max_tokens: 4096,
    enabled: true
  })

  beforeEach(() => {
    getGlobalModelOptions.mockResolvedValue({
      providers: [
        {
          name: 'Nous',
          slug: 'nous',
          models: ['hermes-4', 'hermes-4-mini'],
          authenticated: true,
          capabilities: { 'hermes-4': { reasoning: true, fast: true } }
        },
        {
          name: 'OpenRouter',
          slug: 'openrouter',
          models: ['deepseek/deepseek-v4-pro', 'anthropic/claude-opus-4.8'],
          authenticated: true
        }
      ]
    })
    getMoaModels.mockResolvedValue(moaConfig())
    saveMoaModels.mockImplementation((body: unknown) => Promise.resolve(body))
  })

  async function openReferenceEditor() {
    await renderModelSettings()
    expect(await screen.findByText('Reference 1')).toBeTruthy()
  }

  function slotSelects() {
    // Combobox order in the MoA section (last 7 on the page): preset select,
    // then provider+model per reference (2 refs), then aggregator
    // provider+model. Reference 1's pair is therefore at -6 / -5.
    const all = screen.getAllByRole('combobox')

    return { ref1Provider: all.at(-6)!, ref1Model: all.at(-5)! }
  }

  it('holds the autosave while a slot is half-filled (provider changed, model pending)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(slotSelects().ref1Provider)
      fireEvent.click(await screen.findByRole('option', { name: 'OpenRouter' }))

      // Model was cleared by the provider change → config incomplete → the
      // debounced autosave must NOT fire, even well past the 600ms window.
      await vi.advanceTimersByTimeAsync(2000)
      expect(saveMoaModels).not.toHaveBeenCalled()
    } finally {
      vi.useRealTimers()
    }
  })

  it('saves once the model pick completes the slot', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(slotSelects().ref1Provider)
      fireEvent.click(await screen.findByRole('option', { name: 'OpenRouter' }))
      await vi.advanceTimersByTimeAsync(700)

      fireEvent.click(slotSelects().ref1Model)
      fireEvent.click(await screen.findByRole('option', { name: 'anthropic/claude-opus-4.8' }))
      await vi.advanceTimersByTimeAsync(700)

      expect(saveMoaModels).toHaveBeenCalledTimes(1)
      const sent = saveMoaModels.mock.calls[0][0] as ReturnType<typeof moaConfig>
      expect(sent.presets.default.reference_models[0]).toEqual({
        provider: 'openrouter',
        model: 'anthropic/claude-opus-4.8'
      })
      // The untouched slots ride along unchanged — nothing reverts to defaults.
      expect(sent.presets.default.reference_models[1]).toEqual({
        provider: 'openrouter',
        model: 'deepseek/deepseek-v4-pro'
      })
      expect(sent.presets.default.aggregator).toEqual({
        provider: 'openrouter',
        model: 'anthropic/claude-opus-4.8'
      })
    } finally {
      vi.useRealTimers()
    }
  })

  it('does not clear the model or save when the same provider is re-selected', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(slotSelects().ref1Provider)
      fireEvent.click(await screen.findByRole('option', { name: 'Nous' }))
      await vi.advanceTimersByTimeAsync(700)

      // Radix treats re-picking the current value as a no-op (no
      // onValueChange), so nothing changes: no save, model still shown.
      expect(saveMoaModels).not.toHaveBeenCalled()
      expect(screen.getByText('nous · hermes-4')).toBeTruthy()
    } finally {
      vi.useRealTimers()
    }
  })

  it('autosaves the selected preset when its enabled switch is toggled', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(screen.getByRole('switch', { name: 'Enabled' }))
      await vi.advanceTimersByTimeAsync(700)

      expect(saveMoaModels).toHaveBeenCalledWith(
        expect.objectContaining({
          presets: expect.objectContaining({
            default: expect.objectContaining({ enabled: false })
          })
        })
      )
    } finally {
      vi.useRealTimers()
    }
  })

  it('saves a disabled reference model without removing it (per-slot enabled toggle)', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true })

    try {
      await openReferenceEditor()

      fireEvent.click(screen.getByRole('switch', { name: 'Disable reference 1' }))
      await vi.advanceTimersByTimeAsync(700)

      expect(saveMoaModels).toHaveBeenCalledWith(
        expect.objectContaining({
          presets: expect.objectContaining({
            default: expect.objectContaining({
              reference_models: [
                expect.objectContaining({ provider: 'nous', model: 'hermes-4', enabled: false }),
                expect.objectContaining({ provider: 'openrouter', model: 'deepseek/deepseek-v4-pro' })
              ]
            })
          })
        })
      )
    } finally {
      vi.useRealTimers()
    }
  })
})
