import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { DropdownMenu, DropdownMenuContent } from '@/components/ui/dropdown-menu'
import { $pinnedModels, pinnedModelKey, togglePinnedModel } from '@/store/model-pins'
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

vi.mock('@/hermes', () => ({
  getGlobalModelOptions: (...args: unknown[]) => getGlobalModelOptions(...args),
  setApiRequestProfile: vi.fn()
}))

beforeEach(() => {
  window.localStorage.clear()
  $visibleModels.set(null)
  $pinnedModels.set([])
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

// A pin lifts one provider/model pair above the provider groups. The point is
// reach: the model you use every day is the first row, whatever its provider's
// alphabetical position.
describe('pinned models sit above the provider groups', () => {
  const menuRows = () => screen.getAllByRole('menuitem').map(row => row.textContent ?? '')

  it('renders a pinned model as the first row, ahead of the provider heading', async () => {
    togglePinnedModel('google', 'gemini-2.5-flash')

    renderMenu()
    await screen.findByText('Pinned')

    const rows = menuRows()
    const pinnedIndex = rows.findIndex(text => /Gemini 2\.5 Flash/i.test(text))
    const providerIndex = rows.findIndex(text => /^Google/.test(text))

    expect(pinnedIndex).toBeGreaterThanOrEqual(0)
    expect(pinnedIndex).toBeLessThan(providerIndex)
  })

  // Rendering it in both places would double the row's keyboard entry and show
  // two check marks for one active model.
  it('does not also list a pinned model under its provider', async () => {
    togglePinnedModel('google', 'gemini-2.5-flash')

    renderMenu()
    await screen.findByText('Pinned')

    expect(screen.getAllByText(/Gemini 2\.5 Flash/i)).toHaveLength(1)
  })

  it('shows pins in pin order, not catalog order', async () => {
    // Flash is second in the provider's curated list; pinning it first must put
    // it above Pro in the pinned section.
    togglePinnedModel('google', 'gemini-2.5-flash')
    togglePinnedModel('google', 'gemini-3.1-pro')

    renderMenu()
    await screen.findByText('Pinned')

    const rows = menuRows()

    expect(rows.findIndex(text => /Gemini 2\.5 Flash/i.test(text))).toBeLessThan(
      rows.findIndex(text => /Gemini 3\.1 Pro/i.test(text))
    )
  })

  // Curation and pinning are different questions: "which models do I usually
  // want listed" vs "which one do I want first". A pin must win.
  it('shows a pinned model the Edit Models shortlist hides', async () => {
    setVisibleModels(new Set([modelVisibilityKey('google', 'gemini-3.1-pro')]))
    togglePinnedModel('google', 'gemini-2.5-flash')

    renderMenu()

    await screen.findByText(/Gemini 2\.5 Flash/i)
  })

  it('drops a pin whose model the catalog no longer offers, without hiding the rest', async () => {
    $pinnedModels.set([pinnedModelKey('google', 'gemini-1.0-retired'), pinnedModelKey('google', 'gemini-2.5-flash')])

    renderMenu()
    await screen.findByText('Pinned')

    expect(screen.queryByText(/Gemini 1\.0 Retired/i)).toBeNull()
    expect(screen.getAllByText(/Gemini 2\.5 Flash/i)).toHaveLength(1)
  })

  it('filters the pinned section by search like any other row', async () => {
    togglePinnedModel('google', 'gemini-2.5-flash')

    renderMenu()
    await screen.findByText('Pinned')

    fireEvent.change(screen.getByRole('textbox', { name: 'Search models' }), { target: { value: 'gemini-3.1' } })

    await vi.waitFor(() => {
      expect(screen.queryByText('Pinned')).toBeNull()
    })

    expect(screen.queryByText(/Gemini 3\.1 Pro/i)).not.toBeNull()
  })
})

// The pin lives in the row's hover submenu, next to the other per-model
// settings — pinning is a property of the model, not a separate mode. The row
// names the ACTION, so it reads "Pin to top" unpinned and "Unpin" pinned.
describe('pinning from the row submenu', () => {
  it('pins the row it was opened from', async () => {
    renderMenu()

    const row = await screen.findByText(/Gemini 3\.1 Pro/i)

    fireEvent.pointerMove(row.closest('[role="menuitem"]')!, { pointerType: 'mouse' })

    const pin = await screen.findByText('Pin to top')

    fireEvent.click(pin)

    expect($pinnedModels.get()).toEqual([pinnedModelKey('google', 'gemini-3.1-pro')])
  })

  it('offers Unpin once the model is pinned', async () => {
    togglePinnedModel('google', 'gemini-3.1-pro')

    renderMenu()

    const row = await screen.findByText(/Gemini 3\.1 Pro/i)

    fireEvent.pointerMove(row.closest('[role="menuitem"]')!, { pointerType: 'mouse' })

    await screen.findByText('Unpin')
    expect(screen.queryByText('Pin to top')).toBeNull()
  })

  it('Unpin clears the pin', async () => {
    togglePinnedModel('google', 'gemini-3.1-pro')

    renderMenu()

    const row = await screen.findByText(/Gemini 3\.1 Pro/i)

    fireEvent.pointerMove(row.closest('[role="menuitem"]')!, { pointerType: 'mouse' })

    fireEvent.click(await screen.findByText('Unpin'))

    expect($pinnedModels.get()).toEqual([])
  })
})
