import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
  globalThis.ResizeObserver = class {
    disconnect() {}
    observe() {}
    unobserve() {}
  }
})

const createProfile = vi.fn()
const getGlobalModelInfo = vi.fn()
const getGlobalModelOptions = vi.fn()
const getHermesConfigRecord = vi.fn()
const getOpenRouterEndpoints = vi.fn()
const saveHermesConfig = vi.fn()
const setMainModelAssignment = vi.fn()
const updateProfileSoul = vi.fn()

vi.mock('@/hermes', () => ({
  createProfile: (body: unknown) => createProfile(body),
  getGlobalModelInfo: (profile?: string | null) => getGlobalModelInfo(profile),
  getGlobalModelOptions: (opts?: unknown, profile?: string | null) => getGlobalModelOptions(opts, profile),
  getHermesConfigRecord: (profile?: string | null) => getHermesConfigRecord(profile),
  getOpenRouterEndpoints: (model: string, opts?: unknown) => getOpenRouterEndpoints(model, opts),
  saveHermesConfig: (config: unknown, profile?: string | null) => saveHermesConfig(config, profile),
  updateProfileSoul: (name: string, content: string) => updateProfileSoul(name, content)
}))

vi.mock('@/store/cron-model-impact', () => ({
  setMainModelAssignment: (request: unknown, profile?: string | null) => setMainModelAssignment(request, profile)
}))

import { CreateProfileDialog } from './create-profile-dialog'

const OPENROUTER_OPTIONS = {
  providers: [
    {
      authenticated: true,
      models: ['deepseek/deepseek-v4-flash', 'openai/gpt-5.4'],
      name: 'OpenRouter',
      slug: 'openrouter'
    },
    { authenticated: true, models: ['gpt-5.4'], name: 'OpenAI', slug: 'openai' }
  ]
}

const BASE_CONFIG = {
  provider_routing: {
    model_overrides: {
      openrouter: {}
    }
  }
}

function realClick(element: HTMLElement) {
  fireEvent.click(element)
}

async function chooseSelect(index: number, option: string) {
  realClick(screen.getAllByRole('combobox')[index])
  fireEvent.click(await screen.findByRole('option', { name: option }))
}

async function renderDialog() {
  await act(async () => {
    render(
      <CreateProfileDialog
        onClose={vi.fn()}
        onCreated={vi.fn()}
        open
        profiles={[
          {
            has_env: true,
            is_default: true,
            model: 'deepseek/deepseek-v4-flash',
            name: 'default',
            path: '/profiles/default',
            provider: 'openrouter',
            skill_count: 0
          }
        ]}
      />
    )
  })
}

beforeEach(() => {
  createProfile.mockReset().mockResolvedValue({ name: 'agent-x', ok: true, path: '/profiles/agent-x' })
  getGlobalModelInfo.mockReset().mockResolvedValue({ provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' })
  getGlobalModelOptions.mockReset().mockResolvedValue(OPENROUTER_OPTIONS)
  getHermesConfigRecord.mockReset().mockResolvedValue(BASE_CONFIG)
  getOpenRouterEndpoints.mockReset().mockResolvedValue({
    model: 'deepseek/deepseek-v4-flash',
    endpoints: [{ provider_name: 'Baidu display', tag: 'baidu/fp8', quantization: 'fp8', status: 0 }]
  })
  saveHermesConfig.mockReset().mockResolvedValue({ ok: true })
  setMainModelAssignment.mockReset().mockResolvedValue({ ok: true })
  updateProfileSoul.mockReset().mockResolvedValue({ ok: true })
})

afterEach(cleanup)

describe('CreateProfileDialog OpenRouter routing', () => {
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
          authenticated: true,
          models: ['deepseek/deepseek-v4-flash', 'openai/gpt-5.4'],
          name: 'OpenRouter',
          slug
        }
      ]
    })

    await renderDialog()
    await waitFor(() => expect(getGlobalModelInfo).toHaveBeenCalled())

    if (expectVisible) {
      expect(await screen.findByRole('combobox', { name: 'OpenRouter model' })).toBeTruthy()
    } else {
      expect(screen.queryByRole('combobox', { name: 'OpenRouter model' })).toBeNull()
    }
  })

  it('types a custom OpenRouter model through discovery and scoped profile persistence', async () => {
    await renderDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'agent-x' } })
    fireEvent.change(await screen.findByRole('combobox', { name: 'OpenRouter model' }), {
      target: { value: 'meta-llama/llama-3.3-70b-instruct:free' }
    })

    await waitFor(() =>
      expect(getOpenRouterEndpoints).toHaveBeenCalledWith(
        'meta-llama/llama-3.3-70b-instruct:free',
        expect.objectContaining({ profile: 'default' })
      )
    )
    fireEvent.click(screen.getByRole('button', { name: 'Create profile' }))
    await waitFor(() =>
      expect(setMainModelAssignment).toHaveBeenCalledWith(
        { provider: 'openrouter', model: 'meta-llama/llama-3.3-70b-instruct:free' },
        'agent-x'
      )
    )
  })

  it('shows routing only for OpenRouter with a selected model', async () => {
    await renderDialog()
    expect(await screen.findByText('OpenRouter route')).toBeTruthy()

    await chooseSelect(1, 'OpenAI')
    expect(screen.queryByText('OpenRouter route')).toBeNull()
  })

  it('persists a locked endpoint tag into the newly-created profile only', async () => {
    await renderDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'agent-x' } })

    fireEvent.click(await screen.findByRole('radio', { name: /Baidu display/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Create profile' }))

    await waitFor(() => expect(createProfile).toHaveBeenCalledWith({ name: 'agent-x', clone_from: 'default' }))
    expect(setMainModelAssignment).toHaveBeenCalledWith(
      { provider: 'openrouter', model: 'deepseek/deepseek-v4-flash' },
      'agent-x'
    )
    expect(saveHermesConfig).toHaveBeenCalledWith(
      expect.objectContaining({
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
      }),
      'agent-x'
    )
    expect(saveHermesConfig).not.toHaveBeenCalledWith(expect.anything(), null)
    expect(saveHermesConfig).not.toHaveBeenCalledWith(expect.anything(), 'default')
  })

  it('fetches the config record AFTER the model assignment lands, never before (no stale-write race)', async () => {
    // create-profile-dialog.tsx already gets this ordering right: it awaits
    // setMainModelAssignment() before calling getHermesConfigRecord(), so the
    // record used to build the routing PUT reflects the just-written model
    // assignment. Lock that ordering in so a future edit can't reintroduce
    // the #5be516088 stale-record race (fixed in model-settings.tsx by
    // re-fetching after assignment for the same reason).
    const callOrder: string[] = []
    setMainModelAssignment.mockImplementation(async () => {
      callOrder.push('setMainModelAssignment')

      return { ok: true }
    })
    getHermesConfigRecord.mockImplementation(async () => {
      callOrder.push('getHermesConfigRecord')

      return BASE_CONFIG
    })

    await renderDialog()
    fireEvent.change(screen.getByLabelText('Name'), { target: { value: 'agent-x' } })
    fireEvent.click(await screen.findByRole('radio', { name: /Baidu display/ }))

    // Clear the mount-time getHermesConfigRecord call (used to pre-fill the
    // routing draft) so only the post-submit ordering is asserted.
    callOrder.length = 0
    fireEvent.click(screen.getByRole('button', { name: 'Create profile' }))

    await waitFor(() => expect(saveHermesConfig).toHaveBeenCalled())

    expect(callOrder).toEqual(['setMainModelAssignment', 'getHermesConfigRecord'])
  })

  it('inherits a matching cloned model route into the draft', async () => {
    getHermesConfigRecord.mockResolvedValue({
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

    await renderDialog()

    expect((await screen.findByRole('radio', { name: /Baidu display/ })).getAttribute('aria-checked')).toBe('true')
  })

  it('ignores stale discovery after the clone profile changes', async () => {
    let resolveOld!: (value: unknown) => void
    getOpenRouterEndpoints
      .mockImplementationOnce(
        () =>
          new Promise(resolve => {
            resolveOld = resolve
          })
      )
      .mockResolvedValueOnce({
        model: 'deepseek/deepseek-v4-flash',
        endpoints: [{ provider_name: 'Current endpoint', tag: 'current', quantization: 'fp8' }]
      })

    await renderDialog()
    await waitFor(() => expect(getOpenRouterEndpoints).toHaveBeenCalledTimes(1))
    await chooseSelect(0, 'None (blank)')
    await waitFor(() => expect(getOpenRouterEndpoints).toHaveBeenCalledTimes(2))
    expect(await screen.findByText('Current endpoint')).toBeTruthy()

    await act(async () => {
      resolveOld({
        model: 'deepseek/deepseek-v4-flash',
        endpoints: [{ provider_name: 'Stale endpoint', tag: 'stale', quantization: 'fp8' }]
      })
    })
    expect(screen.queryByText('Stale endpoint')).toBeNull()
  })

  it('keeps manual routing available after discovery fails', async () => {
    getOpenRouterEndpoints.mockRejectedValue(new Error('discovery offline'))
    await renderDialog()

    expect(await screen.findByText(/Endpoint discovery failed/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Enter provider tag manually' }))
    expect(screen.getByRole('textbox', { name: 'Provider tag' })).toBeTruthy()
  })
})
