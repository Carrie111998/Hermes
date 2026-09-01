import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, waitFor } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { ModelPickerDialog } from './model-picker'

// cmdk observes its list with ResizeObserver / scrollIntoView; jsdom implements
// neither, so stub them to let the command list mount in tests.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  globalThis.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  } as unknown as typeof ResizeObserver
})

// The picker truncates each row (`min-w-0 flex-1 truncate`) so long provider
// model ids fit on one line for keyboard nav + cmdk highlight. The escape
// hatch that keeps the full id reachable is a native `title` attribute on the
// row. This test pins that contract: a future refactor that drops `title`
// (the reason the fix exists) must fail here.

const requestModelOptions = vi.fn()

vi.mock('@/lib/model-options', () => ({
  modelOptionsQueryKey: (profile: null | string | undefined, sessionId?: null | string) => [
    'model-options',
    profile ?? 'default',
    sessionId ?? null
  ],
  requestModelOptions: () => requestModelOptions()
}))

vi.mock('@/store/onboarding', () => ({
  startManualOnboarding: vi.fn()
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { cancel: 'Cancel' },
      modelPicker: {
        title: 'Select model',
        current: 'Current:',
        unknown: 'unknown',
        search: 'Search models',
        noModels: 'No models',
        addProvider: 'Add provider',
        pro: 'PRO',
        proNeedsSubscription: 'Pro needs subscription',
        free: 'FREE',
        freeTier: 'Free tier',
        priceTitle: 'In / Out $/Mtok',
        wasPrice: 'was',
        loadFailed: 'Load failed',
        noAuthenticatedProviders: 'No authenticated providers'
      }
    }
  })
}))

// Two ids that share the full `ri.language-model-service..language-model.`
// prefix — the exact real-world case that motivated the tooltip.
const LONG_OPUS = 'ri.language-model-service..language-model.anthropic-claude-4-8-opus'
const LONG_SONNET = 'ri.language-model-service..language-model.anthropic-claude-4-6-sonnet'

function renderPicker() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>{children}</QueryClientProvider>
  )
  return render(
    <ModelPickerDialog
      currentModel={LONG_OPUS}
      currentProvider="palantir-claude"
      onOpenChange={() => {}}
      onSelect={() => {}}
      open
    />,
    { wrapper }
  )
}

beforeEach(() => {
  requestModelOptions.mockResolvedValue({
    providers: [
      {
        name: 'Palantir Claude',
        slug: 'palantir-claude',
        models: [LONG_OPUS, LONG_SONNET],
        authenticated: true
      }
    ]
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('ModelPickerDialog row tooltip', () => {
  it('exposes the full model id via a native title attribute on each row', async () => {
    renderPicker()

    // cmdk lowercases data-value; match rows by their title attribute instead,
    // which is exactly the contract under test.
    const opusRow = await waitFor(() => {
      const el = document.querySelector<HTMLElement>(`[title="${LONG_OPUS}"]`)
      if (!el) {
        throw new Error('opus row not rendered yet')
      }
      return el
    })
    const sonnetRow = document.querySelector<HTMLElement>(`[title="${LONG_SONNET}"]`)

    expect(opusRow).not.toBeNull()
    expect(sonnetRow).not.toBeNull()
    // Finding each row by its [title="<full id>"] selector *is* the assertion:
    // the whole id is recoverable on hover even though the visible text truncates.
    expect(opusRow.getAttribute('title')).toBe(LONG_OPUS)
    expect(sonnetRow?.getAttribute('title')).toBe(LONG_SONNET)
  })
})
