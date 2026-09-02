import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSub,
  DropdownMenuSubTrigger
} from '@/components/ui/dropdown-menu'

import { type FastControl, ModelEditSubmenu } from './model-edit-submenu'

// Radix calls these on open; jsdom doesn't implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// Render the submenu inside an open menu/sub so its content (switches) mounts.
function renderSubmenu(opts: {
  defaultEffort?: string
  effort?: string
  fastControl: FastControl
  isActive?: boolean
  onSelectModel?: (model: string) => void
  onSetOptions: (patch: { effort?: string; fast?: boolean }) => void
  reasoning: boolean
  supportedEfforts?: string[]
}) {
  return render(
    <DropdownMenu open>
      <DropdownMenuContent>
        <DropdownMenuSub open>
          <DropdownMenuSubTrigger>edit</DropdownMenuSubTrigger>
          <ModelEditSubmenu
            defaultEffort={opts.defaultEffort ?? 'medium'}
            effort={opts.effort ?? 'medium'}
            fastControl={opts.fastControl}
            isActive={opts.isActive ?? true}
            model="m1"
            onSelectModel={opts.onSelectModel ?? vi.fn()}
            onSetOptions={opts.onSetOptions}
            provider="p1"
            reasoning={opts.reasoning}
            supportedEfforts={opts.supportedEfforts}
          />
        </DropdownMenuSub>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// The submenu is PURE: it reports edits and never writes to a session, a
// preset store, or the gateway. That's the invariant that lets the same
// component drive a live chat session AND a detached per-task override — if it
// ever writes directly again, picking an effort for a kanban card would reach
// over and change the user's live chat.
describe('ModelEditSubmenu reports edits without performing them', () => {
  it('param fast: reports the toggle', () => {
    const onSetOptions = vi.fn()
    renderSubmenu({ fastControl: { kind: 'param', on: true }, onSetOptions, reasoning: false })

    fireEvent.click(screen.getByRole('switch'))

    expect(onSetOptions).toHaveBeenCalledWith({ fast: false })
  })

  it('thinking: toggling off reports the none level', () => {
    const onSetOptions = vi.fn()
    renderSubmenu({ fastControl: { kind: 'none' }, onSetOptions, reasoning: true })

    // Thinking starts on (medium); toggling it off reports 'none'.
    fireEvent.click(screen.getByRole('switch'))

    expect(onSetOptions).toHaveBeenCalledWith({ effort: 'none' })
  })

  it('thinking: toggling back on restores the row level, not the hardcoded default', () => {
    const onSetOptions = vi.fn()
    renderSubmenu({
      defaultEffort: 'high',
      effort: 'none',
      fastControl: { kind: 'none' },
      onSetOptions,
      reasoning: true
    })

    fireEvent.click(screen.getByRole('switch'))

    expect(onSetOptions).toHaveBeenCalledWith({ effort: 'high' })
  })

  it('variant fast: swaps the model only when the row is active', () => {
    const onSelectModel = vi.fn()
    const onSetOptions = vi.fn()

    renderSubmenu({
      fastControl: { baseId: 'm1', fastId: 'm1-fast', kind: 'variant', on: false },
      isActive: false,
      onSelectModel,
      onSetOptions,
      reasoning: false
    })

    fireEvent.click(screen.getByRole('switch'))

    // Inactive rows stay preference-only — no model switch.
    expect(onSetOptions).toHaveBeenCalledWith({ fast: true })
    expect(onSelectModel).not.toHaveBeenCalled()
  })

  it('variant fast: active row swaps to the -fast sibling', () => {
    const onSelectModel = vi.fn()
    const onSetOptions = vi.fn()

    renderSubmenu({
      fastControl: { baseId: 'm1', fastId: 'm1-fast', kind: 'variant', on: false },
      onSelectModel,
      onSetOptions,
      reasoning: false
    })

    fireEvent.click(screen.getByRole('switch'))

    expect(onSelectModel).toHaveBeenCalledWith('m1-fast')
  })

  it('supportedEfforts restricts the effort radio to declared levels', () => {
    const onSetOptions = vi.fn()
    renderSubmenu({
      fastControl: { kind: 'none' },
      onSetOptions,
      reasoning: true,
      supportedEfforts: ['high', 'max']
    })

    // GLM-5.2-style: only high and max are offered, medium (the default) is not.
    const radios = screen.getAllByRole('menuitemradio') as HTMLElement[]
    const levels = radios.map(r => r.textContent?.trim() ?? '')

    expect(levels).toEqual(['High', 'Max'])
    expect(levels).not.toContain('Medium')
    expect(levels).not.toContain('XHigh')
  })

  it('absent supportedEfforts shows the full effort ladder', () => {
    const onSetOptions = vi.fn()
    renderSubmenu({
      fastControl: { kind: 'none' },
      onSetOptions,
      reasoning: true
    })

    const radios = screen.getAllByRole('menuitemradio') as HTMLElement[]
    const levels = radios.map(r => r.textContent?.trim() ?? '')

    expect(levels.length).toBeGreaterThan(4)
    expect(levels).toContain('Medium')
    expect(levels).toContain('Max')
    expect(levels).toContain('Minimal')
  })

  it('clamps a persisted effort excluded by the restriction (review #1)', () => {
    // User set `medium`, then the model declared only high/max. The radio
    // must converge onto an allowed level (not render blank) and report the
    // correction once so the store stops sending the stale invalid effort.
    const onSetOptions = vi.fn()
    renderSubmenu({
      defaultEffort: 'medium',
      effort: 'medium',
      fastControl: { kind: 'none' },
      onSetOptions,
      reasoning: true,
      supportedEfforts: ['high', 'max']
    })

    // Radio shows High selected (the clamped level), not a blank selection.
    const radios = screen.getAllByRole('menuitemradio') as HTMLElement[]
    const selected = radios.filter(r => r.getAttribute('aria-checked') === 'true')
    expect(selected.map(r => r.textContent?.trim())).toEqual(['High'])

    // The correction is reported once.
    expect(onSetOptions).toHaveBeenCalledWith({ effort: 'high' })
  })

  it('thinking off is not clamped by a restriction (review #1)', () => {
    // effort='none' (thinking off) resolves to '' — the radio stays clear
    // and no correction is reported.
    const onSetOptions = vi.fn()
    renderSubmenu({
      defaultEffort: 'medium',
      effort: 'none',
      fastControl: { kind: 'none' },
      onSetOptions,
      reasoning: true,
      supportedEfforts: ['high', 'max']
    })

    const radios = screen.getAllByRole('menuitemradio') as HTMLElement[]
    expect(radios.filter(r => r.getAttribute('aria-checked') === 'true')).toHaveLength(0)
    expect(onSetOptions).not.toHaveBeenCalled()
  })

  it('normalizes case of supportedEfforts at the boundary (review #3)', () => {
    // A producer sending `["High"]` must not silently empty the radio.
    const onSetOptions = vi.fn()
    renderSubmenu({
      defaultEffort: 'medium',
      effort: 'max',
      fastControl: { kind: 'none' },
      onSetOptions,
      reasoning: true,
      supportedEfforts: ['High', 'MAX']
    })

    const radios = screen.getAllByRole('menuitemradio') as HTMLElement[]
    const levels = radios.map(r => r.textContent?.trim() ?? '')
    expect(levels).toEqual(['High', 'Max'])
  })
})
