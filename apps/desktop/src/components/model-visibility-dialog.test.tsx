import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $visibleModels } from '@/store/model-visibility'
import { $collapsedProviders } from '@/store/provider-collapse'

import { ModelVisibilityDialog } from './model-visibility-dialog'

const getGlobalModelOptions = vi.fn()
const getHermesConfigRecord = vi.fn()
const saveHermesConfig = vi.fn()

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: (...args: unknown[]) => getGlobalModelOptions(...args),
  getHermesConfigRecord: () => getHermesConfigRecord(),
  saveHermesConfig: (config: unknown) => saveHermesConfig(config),
  setApiRequestProfile: vi.fn()
}))

const notifyError = vi.fn()

vi.mock('@/store/notifications', () => ({
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

const PROVIDERS = [
  { models: ['deepseek-v4-pro', 'deepseek-chat'], name: 'DeepSeek', slug: 'deepseek' },
  { models: ['gemini-3.1-pro'], name: 'Google', slug: 'google' }
]

beforeEach(() => {
  $visibleModels.set(null)
  $collapsedProviders.set([])
  getGlobalModelOptions.mockResolvedValue({ providers: PROVIDERS })
  getHermesConfigRecord.mockResolvedValue({ agent: { reasoning_effort: 'high' } })
  saveHermesConfig.mockResolvedValue({ ok: true })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

function renderDialog() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(
    <QueryClientProvider client={client}>
      <ModelVisibilityDialog onOpenChange={vi.fn()} onOpenProviders={vi.fn()} open />
    </QueryClientProvider>
  )
}

describe('ModelVisibilityDialog provider switch', () => {
  it('writes the provider into model_catalog.excluded_providers so every picker drops it', async () => {
    const content = renderDialog()

    const toggle = await content.findByRole('switch', { name: 'Enable DeepSeek' })
    fireEvent.click(toggle)

    // Config write, not a desktop-local preference — the backend builds the
    // catalog for the TUI and `hermes model` from this same key.
    await waitFor(() => {
      expect(saveHermesConfig).toHaveBeenCalledWith({
        agent: { reasoning_effort: 'high' },
        model_catalog: { excluded_providers: ['deepseek'] }
      })
    })

    // Optimistic: the model rows fold away without waiting for a refetch.
    expect(content.queryByText('Deepseek V4 Pro')).toBeNull()
    // Only that provider is off; the others keep their rows.
    expect(content.queryByText('Gemini 3.1 pro')).not.toBeNull()
  })

  it('keeps a row for an excluded provider the catalog no longer returns, so it can be switched back on', async () => {
    // The backend drops excluded providers from the payload — the only trace of
    // `copilot` is the config list.
    getHermesConfigRecord.mockResolvedValue({ model_catalog: { excluded_providers: ['copilot'] } })
    const content = renderDialog()

    const toggle = await content.findByRole('switch', { name: 'Enable copilot' })
    expect(toggle.getAttribute('data-state')).toBe('unchecked')

    fireEvent.click(toggle)

    // An explicit empty list, not a dropped key: PUT /api/config deep-merges,
    // so omitting it would leave `copilot` excluded on disk.
    await waitFor(() => {
      expect(saveHermesConfig).toHaveBeenCalledWith({ model_catalog: { excluded_providers: [] } })
    })
  })

  it('rolls the switch back and reports the failure when the config write fails', async () => {
    saveHermesConfig.mockRejectedValue(new Error('read-only managed install'))
    const content = renderDialog()

    const toggle = await content.findByRole('switch', { name: 'Enable DeepSeek' })
    fireEvent.click(toggle)

    await waitFor(() => {
      expect(notifyError).toHaveBeenCalled()
    })

    expect(content.getByRole('switch', { name: 'Enable DeepSeek' }).getAttribute('data-state')).toBe('checked')
    expect(content.queryByText('Deepseek V4 Pro')).not.toBeNull()
  })

  it('hides the select-all checkbox of a disabled provider (nothing to curate)', async () => {
    getHermesConfigRecord.mockResolvedValue({ model_catalog: { excluded_providers: ['deepseek'] } })
    const content = renderDialog()

    await content.findByText('DeepSeek')

    // One checkbox left: the enabled provider's select-all.
    expect(content.queryAllByRole('checkbox')).toHaveLength(1)
  })
})
