import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuSub,
  DropdownMenuSubTrigger
} from '@/components/ui/dropdown-menu'
import type * as HermesApi from '@/hermes'
import { $modelPresets, getModelPreset } from '@/store/model-presets'
import {
  $activeSessionId,
  $currentFastMode,
  $currentReasoningEffort,
  getCurrentModelSource,
  setCurrentFastMode,
  setCurrentModelSource,
  setCurrentReasoningEffort
} from '@/store/session'

import { type FastControl, ModelEditSubmenu } from './model-edit-submenu'

vi.mock('@/hermes', async importOriginal => {
  const actual = await importOriginal<typeof HermesApi>()

  return { ...actual, setApiRequestProfile: vi.fn() }
})

// Radix calls these on open; jsdom doesn't implement them.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

beforeEach(() => {
  $modelPresets.set({})
  $activeSessionId.set(null)
  setCurrentFastMode(false)
  setCurrentModelSource('')
  setCurrentReasoningEffort('')
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

// Render the submenu inside an open menu/sub so its content (switches) mounts.
function renderSubmenu(opts: { fastControl: FastControl; reasoning: boolean; requestGateway: () => Promise<unknown> }) {
  return render(
    <DropdownMenu open>
      <DropdownMenuContent>
        <DropdownMenuSub open>
          <DropdownMenuSubTrigger>edit</DropdownMenuSubTrigger>
          <ModelEditSubmenu
            effort="medium"
            fastControl={opts.fastControl}
            isActive
            model="m1"
            onSelectModel={vi.fn()}
            provider="p1"
            reasoning={opts.reasoning}
            requestGateway={opts.requestGateway as never}
          />
        </DropdownMenuSub>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

// Regression: editing the active row before a live session exists must never
// call the gateway — config.set falls back to global config when no session
// matches. Reasoning stays preset-only; param-fast is draft-only.
describe('ModelEditSubmenu no-session guard', () => {
  it('param fast: is draft-only and skips the gateway without a session', () => {
    const requestGateway = vi.fn().mockResolvedValue({})
    setCurrentFastMode(true)
    renderSubmenu({ fastControl: { kind: 'param', on: true }, reasoning: false, requestGateway })

    fireEvent.click(screen.getByRole('switch'))

    expect(getModelPreset('p1', 'm1').fast).toBeUndefined()
    expect($currentFastMode.get()).toBe(false)
    expect(getCurrentModelSource()).toBe('manual')
    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('reasoning: records the preset but skips the gateway without a session', () => {
    const requestGateway = vi.fn().mockResolvedValue({})
    renderSubmenu({ fastControl: { kind: 'none' }, reasoning: true, requestGateway })

    // Thinking starts on (medium); toggling it off routes through patchReasoning.
    fireEvent.click(screen.getByRole('switch'))

    expect(getModelPreset('p1', 'm1').effort).toBe('none')
    expect($currentReasoningEffort.get()).toBe('none')
    expect(getCurrentModelSource()).toBe('manual')
    expect(requestGateway).not.toHaveBeenCalled()
  })

  it('param fast: pushes to the gateway once a session is active', async () => {
    const requestGateway = vi.fn().mockResolvedValue({})
    $activeSessionId.set('sess1')
    renderSubmenu({ fastControl: { kind: 'param', on: false }, reasoning: false, requestGateway })

    fireEvent.click(screen.getByRole('switch'))

    expect(requestGateway).toHaveBeenCalledWith('config.set', { key: 'fast', session_id: 'sess1', value: 'fast' })
  })
})
