import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { DropdownMenu, DropdownMenuContent } from '@/components/ui/dropdown-menu'
import {
  $modelVisibilityOpen,
  $visibleModels,
  modelVisibilityKey,
  setModelVisibilityOpen,
  setVisibleModels
} from '@/store/model-visibility'

import { ModelCatalogMenu, type ModelMenuController } from './model-catalog-menu'

// Radix calls these on open; jsdom doesn't implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

const getGlobalModelOptions = vi.fn()
const startManualProviderOAuth = vi.fn()

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: (...args: unknown[]) => getGlobalModelOptions(...args),
  setApiRequestProfile: vi.fn()
}))

vi.mock('@/store/onboarding', () => ({
  startManualProviderOAuth: (...args: unknown[]) => startManualProviderOAuth(...args)
}))

beforeEach(() => {
  $visibleModels.set(null)
  setModelVisibilityOpen(false)
  getGlobalModelOptions.mockResolvedValue({
    providers: [{ models: ['gemini-3.1-pro', 'gemini-2.5-flash'], name: 'Google', slug: 'google' }]
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// A minimal controller — these tests are about the CATALOG's own behaviour
// (what it lists, what it offers), not about what any host does with a pick.
function renderMenu() {
  const select = vi.fn()

  const controller: ModelMenuController = {
    applyPreset: vi.fn(),
    current: { effort: '', fast: false, model: '', provider: '' },
    presetFor: () => ({}),
    select,
    setOptions: vi.fn()
  }

  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  render(
    <QueryClientProvider client={client}>
      <DropdownMenu open>
        <DropdownMenuContent>
          <ModelCatalogMenu controller={controller} />
        </DropdownMenuContent>
      </DropdownMenu>
    </QueryClientProvider>
  )

  return select
}

// Curation is ONE global preference, so it belongs to the catalog rather than
// to whichever surface mounted it. If a host had to opt in, the composer and
// the kanban board would end up disagreeing about what "my models" means —
// which is exactly the drift extracting this component was meant to prevent.
describe('the catalog owns model curation', () => {
  it('honours the stored Edit Models shortlist', async () => {
    setVisibleModels(new Set([modelVisibilityKey('google', 'gemini-2.5-flash')]))

    renderMenu()

    await screen.findByText(/Gemini 2\.5 Flash/i)
    expect(screen.queryByText(/Gemini 3\.1 Pro/i)).toBeNull()
  })

  it('still finds a hidden model by search — curation narrows the default view, not the catalog', async () => {
    setVisibleModels(new Set([modelVisibilityKey('google', 'gemini-2.5-flash')]))

    renderMenu()
    await screen.findByText(/Gemini 2\.5 Flash/i)

    const input = screen.getByRole('textbox', { name: 'Search models' })

    fireEvent.change(input, { target: { value: 'gemini-3.1' } })

    await vi.waitFor(() => {
      expect(screen.queryByText(/Gemini 3\.1 Pro/i)).not.toBeNull()
    })
  })

  it('offers Edit Models without the host wiring it up', async () => {
    renderMenu()
    await screen.findByText(/Gemini 3\.1 Pro/i)

    fireEvent.click(screen.getByText('Edit Models…'))

    expect($modelVisibilityOpen.get()).toBe(true)
  })
})

// Portal is Hermes' own inference product and the path onboarding already
// steers new users down (`PROVIDER_DISPLAY` gives it order 0, and it renders
// through `FeaturedProviderRow`). This menu was the one model surface that
// ignored that and sorted it wherever the alphabet put it. Deliberate product
// placement rather than a neutral ranking, so it is pinned by a test.
describe('Nous Portal sorts above the other providers', () => {
  const headings = () =>
    screen
      .getAllByRole('menuitem')
      .map(row => row.textContent ?? '')
      .filter(text => /^(Anthropic|Google|Nous Portal|Zebra Labs)/.test(text))

  it('puts Portal first even when its name sorts last', async () => {
    getGlobalModelOptions.mockResolvedValue({
      providers: [
        { models: ['claude-sonnet-5'], name: 'Anthropic', slug: 'anthropic' },
        { models: ['portal-model'], name: 'Nous Portal', slug: 'nous' }
      ]
    })

    renderMenu()
    await screen.findByText(/Portal Model/i)

    expect(headings()[0]).toMatch(/^Nous Portal/)
  })

  // Everything below Portal keeps the previous stable alphabetical order, so
  // no other provider's position moves relative to its peers.
  it('leaves the remaining providers alphabetical', async () => {
    getGlobalModelOptions.mockResolvedValue({
      providers: [
        { models: ['zebra-1'], name: 'Zebra Labs', slug: 'zebra' },
        { models: ['gemini-3.1-pro'], name: 'Google', slug: 'google' },
        { models: ['portal-model'], name: 'Nous Portal', slug: 'nous' },
        { models: ['claude-sonnet-5'], name: 'Anthropic', slug: 'anthropic' }
      ]
    })

    renderMenu()
    await screen.findByText(/Portal Model/i)

    expect(headings().map(h => h.replace(/\s.*/, ''))).toEqual(['Nous', 'Anthropic', 'Google', 'Zebra'])
  })
})

// An unconfigured provider produces NO row at all (the payload is
// `explicit_only`), so a user who has never connected Portal would otherwise
// see every other provider they can reach and no mention of Hermes' own.
describe('the Portal sign-in offer', () => {
  it('appears when Portal is absent from the catalog', async () => {
    renderMenu()

    expect(await screen.findByText('Connect Nous Portal')).toBeTruthy()
  })

  it('starts the Portal OAuth flow when chosen', async () => {
    renderMenu()

    fireEvent.click(await screen.findByText('Connect Nous Portal'))

    expect(startManualProviderOAuth).toHaveBeenCalledWith('nous', null)
  })

  it('stays hidden once Portal is connected', async () => {
    getGlobalModelOptions.mockResolvedValue({
      providers: [{ models: ['portal-model'], name: 'Nous Portal', slug: 'nous' }]
    })

    renderMenu()
    await screen.findByText(/Portal Model/i)

    expect(screen.queryByText('Connect Nous Portal')).toBeNull()
  })

  // A query means "find me a model", not "sell me one".
  it('gets out of the way while searching', async () => {
    renderMenu()
    await screen.findByText('Connect Nous Portal')

    fireEvent.change(screen.getByRole('textbox', { name: 'Search models' }), { target: { value: 'gemini' } })

    await vi.waitFor(() => {
      expect(screen.queryByText('Connect Nous Portal')).toBeNull()
    })
  })
})
