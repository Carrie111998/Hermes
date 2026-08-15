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
  efforts?: readonly string[]
  fastControl: FastControl
  isActive?: boolean
  onSelectModel?: (model: string) => void
  onSetOptions: (patch: { effort?: string; fast?: boolean }) => void
  reasoning: boolean
}) {
  return render(
    <DropdownMenu open>
      <DropdownMenuContent>
        <DropdownMenuSub open>
          <DropdownMenuSubTrigger>edit</DropdownMenuSubTrigger>
          <ModelEditSubmenu
            defaultEffort={opts.defaultEffort ?? 'medium'}
            effort={opts.effort ?? 'medium'}
            efforts={opts.efforts}
            fastControl={opts.fastControl}
            isActive={opts.isActive ?? true}
            model="m1"
            onSelectModel={opts.onSelectModel ?? vi.fn()}
            onSetOptions={opts.onSetOptions}
            provider="p1"
            reasoning={opts.reasoning}
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
})

// The backend may narrow the effort ladder per model (capabilities.efforts) —
// e.g. kimi-k3 officially accepts only low/high/max. Offering levels the API
// rejects turns a picker choice into a silent 400, so the submenu must honor
// the enum when one is provided.
describe('ModelEditSubmenu per-model effort enum', () => {
  it('offers only the backend-provided levels', () => {
    renderSubmenu({
      effort: 'high',
      efforts: ['low', 'high', 'max'],
      fastControl: { kind: 'none' },
      onSetOptions: vi.fn(),
      reasoning: true
    })

    expect(screen.getByRole('menuitemradio', { name: 'Low' })).toBeTruthy()
    expect(screen.getByRole('menuitemradio', { name: 'High' })).toBeTruthy()
    expect(screen.getByRole('menuitemradio', { name: 'Max' })).toBeTruthy()
    expect(screen.queryByRole('menuitemradio', { name: 'Medium' })).toBeNull()
    expect(screen.queryByRole('menuitemradio', { name: 'Ultra' })).toBeNull()
  })

  it('hides the Thinking toggle when the enum has no off state', () => {
    renderSubmenu({
      efforts: ['low', 'high', 'max'],
      fastControl: { kind: 'none' },
      onSetOptions: vi.fn(),
      reasoning: true
    })

    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('keeps the Thinking toggle when the enum offers none', () => {
    renderSubmenu({
      efforts: ['none', 'low', 'high'],
      fastControl: { kind: 'none' },
      onSetOptions: vi.fn(),
      reasoning: true
    })

    expect(screen.getByRole('switch')).toBeTruthy()
    // `none` itself stays out of the radio — the toggle owns it.
    expect(screen.queryByRole('menuitemradio', { name: 'none' })).toBeNull()
  })

  it('clamps a configured level the model does not offer to the nearest enum level', () => {
    // medium configured; kimi-style enum → nearest ladder rung is high.
    renderSubmenu({
      effort: 'medium',
      efforts: ['low', 'high', 'max'],
      fastControl: { kind: 'none' },
      onSetOptions: vi.fn(),
      reasoning: true
    })

    expect(screen.getByRole('menuitemradio', { name: 'High', checked: true })).toBeTruthy()
  })

  it('falls back to the full ladder when no enum is provided', () => {
    renderSubmenu({ fastControl: { kind: 'none' }, onSetOptions: vi.fn(), reasoning: true })

    expect(screen.getByRole('menuitemradio', { name: 'Ultra' })).toBeTruthy()
    expect(screen.getByRole('switch')).toBeTruthy()
  })
})
