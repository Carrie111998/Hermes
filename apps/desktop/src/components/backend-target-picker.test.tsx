import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { type BackendTargetChoice, BackendTargetPickerDialog } from './backend-target-picker'

// cmdk (the searchable list) wires a ResizeObserver and scrolls the active
// item into view — neither exists in jsdom. Stub them, matching the polyfill
// idiom in language-switcher.test.tsx.
class TestResizeObserver {
  observe() {}
  unobserve() {}
  disconnect() {}
}

vi.stubGlobal('ResizeObserver', TestResizeObserver)

Element.prototype.scrollIntoView = function scrollIntoView() {}

// I18nProvider is only scaffolding for DialogContent's built-in close button;
// every picker string comes from copy props so this component needs no locale
// entries.
const COPY = {
  title: 'Choose backend',
  description: 'New windows connect to the backend you pick.',
  searchPlaceholder: 'Filter backends',
  currentBadge: 'Current',
  emptyLabel: 'No backends configured yet.',
  noMatchLabel: 'No backends match your filter.',
  cancelLabel: 'Cancel'
}

const CHOICES: BackendTargetChoice[] = [
  { id: 'local-default', label: 'Local (default profile)' },
  { description: 'Remote host for the workstation', id: 'remote-workstation', label: 'Workstation' },
  { description: 'Cloud account', id: 'cloud-main', label: 'Hermes Cloud' }
]

interface RenderPickerOptions {
  choices?: BackendTargetChoice[]
  copy?: Partial<typeof COPY>
  currentChoiceId?: string | null
  onOpenChange?: (open: boolean) => void
  onSelect?: (choiceId: string) => void
  open?: boolean
}

function renderPicker(options: RenderPickerOptions = {}) {
  const onOpenChange = options.onOpenChange ?? vi.fn()
  const onSelect = options.onSelect ?? vi.fn()

  const utils = render(
    <I18nProvider configClient={null} initialLocale="en">
      <BackendTargetPickerDialog
        choices={options.choices ?? CHOICES}
        copy={{ ...COPY, ...options.copy }}
        currentChoiceId={options.currentChoiceId ?? null}
        onOpenChange={onOpenChange}
        onSelect={onSelect}
        open={options.open ?? true}
      />
    </I18nProvider>
  )

  return { onOpenChange, onSelect, ...utils }
}

describe('BackendTargetPickerDialog', () => {
  afterEach(() => {
    cleanup()
    vi.restoreAllMocks()
  })

  it('renders nothing while closed', () => {
    renderPicker({ open: false })

    expect(screen.queryByRole('dialog')).toBeNull()
  })

  it('renders the injected copy: title, description, and placeholder', () => {
    renderPicker()

    const dialog = screen.getByRole('dialog')
    expect(dialog.textContent).toContain('Choose backend')
    expect(dialog.textContent).toContain('New windows connect to the backend you pick.')
    expect(screen.getByPlaceholderText('Filter backends')).toBeTruthy()
  })

  it('lists the injected choices in their given order', () => {
    renderPicker()

    expect(screen.getAllByRole('option').map(option => option.textContent)).toEqual([
      expect.stringContaining('Local (default profile)'),
      expect.stringContaining('Workstation'),
      expect.stringContaining('Hermes Cloud')
    ])
  })

  it('returns the opaque choice id without closing before the owner confirms success', () => {
    const { onOpenChange, onSelect } = renderPicker()

    fireEvent.click(screen.getByRole('option', { name: /workstation/i }))

    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect).toHaveBeenCalledWith('remote-workstation')
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('filters by label and description, preserving the injected order', () => {
    renderPicker()

    // The query only matches the Workstation row's description, not any label.
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'remote host' } })
    expect(screen.getAllByRole('option').map(option => option.textContent)).toEqual([
      expect.stringContaining('Workstation')
    ])

    // A label match narrows to the cloud row.
    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'cloud' } })
    expect(screen.getAllByRole('option').map(option => option.textContent)).toEqual([
      expect.stringContaining('Hermes Cloud')
    ])

    // Clearing the filter restores every choice in the original order.
    fireEvent.change(screen.getByRole('combobox'), { target: { value: '' } })
    expect(screen.getAllByRole('option')).toHaveLength(3)
  })

  it('shows the injected empty state and never selects when no choices exist', () => {
    const { onSelect } = renderPicker({ choices: [] })

    expect(screen.getByRole('dialog').textContent).toContain('No backends configured yet.')
    expect(screen.queryAllByRole('option')).toHaveLength(0)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('shows the injected no-match state when the filter excludes everything', () => {
    renderPicker()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzz' } })

    expect(screen.getByRole('dialog').textContent).toContain('No backends match your filter.')
    expect(screen.queryAllByRole('option')).toHaveLength(0)
  })

  it('flags the current choice visually and accessibly', () => {
    renderPicker({ currentChoiceId: 'local-default' })

    const current = screen.getByRole('option', { name: /local \(default profile\)/i })
    expect(current.getAttribute('data-current')).toBe('true')
    expect(current.textContent).toContain('Current')

    for (const option of screen.getAllByRole('option')) {
      if (option !== current) {
        expect(option.getAttribute('data-current')).toBeNull()
      }
    }
  })

  it('selecting the current choice still passes only its id without closing', () => {
    const { onOpenChange, onSelect } = renderPicker({ currentChoiceId: 'local-default' })

    fireEvent.click(screen.getByRole('option', { name: /local \(default profile\)/i }))

    expect(onSelect).toHaveBeenCalledTimes(1)
    expect(onSelect).toHaveBeenCalledWith('local-default')
    expect(onOpenChange).not.toHaveBeenCalled()
  })

  it('cancel closes the dialog without selecting', () => {
    const { onOpenChange, onSelect } = renderPicker()

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }))

    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(onSelect).not.toHaveBeenCalled()
  })

  it('resets the filter when the dialog is reopened', () => {
    const { rerender } = renderPicker()

    fireEvent.change(screen.getByRole('combobox'), { target: { value: 'zzz' } })
    expect(screen.queryAllByRole('option')).toHaveLength(0)

    const renderWithOpen = (open: boolean) =>
      rerender(
        <I18nProvider configClient={null} initialLocale="en">
          <BackendTargetPickerDialog
            choices={CHOICES}
            copy={COPY}
            currentChoiceId={null}
            onOpenChange={vi.fn()}
            onSelect={vi.fn()}
            open={open}
          />
        </I18nProvider>
      )

    renderWithOpen(false)
    renderWithOpen(true)

    expect(screen.getAllByRole('option')).toHaveLength(3)
  })
})
