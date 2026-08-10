import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $layoutEditMode } from '@/components/pane-shell/edit-mode'
import { TreeEditBar } from '@/components/pane-shell/tree/renderer/edit-bar'

import { RanModeSetting } from './ran-mode-setting'
import { StatusbarVisibilitySetting } from './statusbar-visibility-setting'

import { SettingsView } from './index'

const windowKind = vi.hoisted(() => ({ auxiliary: false }))

vi.mock('@/store/windows', () => ({
  isAuxiliaryWindow: () => windowKind.auxiliary,
  isHudWindow: () => windowKind.auxiliary,
  isSecondaryWindow: () => windowKind.auxiliary
}))

vi.mock('@/i18n', () => ({
  translateNow: (key: string) => key,
  useI18n: () => ({
    t: {
      common: { done: 'Done', off: 'Off', on: 'On' },
      settings: {
        appearance: {
          ranModeDesc: 'Keep chat first.',
          ranModeTitle: 'Ran Mode',
          statusBarDesc: 'Show the global status bar.',
          statusBarTitle: 'Status Bar'
        }
      },
      zones: {
        editHint: 'Arrange panes.',
        editTitle: 'Layouts',
        reset: 'Reset layout'
      }
    }
  })
}))

vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))

beforeEach(() => {
  windowKind.auxiliary = false
  $layoutEditMode.set(false)
})

afterEach(() => {
  cleanup()
  $layoutEditMode.set(false)
})

describe('Ran Mode auxiliary presentation isolation', () => {
  it('keeps the Appearance toggle available on the primary renderer', () => {
    render(<RanModeSetting />)

    expect(screen.getByTestId('ran-mode-toggle')).toBeTruthy()
  })

  it('omits the Appearance toggle from auxiliary and HUD renderers', () => {
    windowKind.auxiliary = true
    render(<RanModeSetting />)

    expect(screen.queryByTestId('ran-mode-toggle')).toBeNull()
  })

  it('omits the primary-owned status-bar preference from auxiliary settings', () => {
    windowKind.auxiliary = true
    render(<StatusbarVisibilitySetting />)

    expect(screen.queryByText('Status Bar')).toBeNull()
  })

  it('fails closed when an auxiliary renderer reaches the Settings route directly', () => {
    windowKind.auxiliary = true
    const { container } = render(<SettingsView onClose={vi.fn()} />)

    expect(container.firstChild).toBeNull()
    expect(screen.queryByText('Tool Call Display')).toBeNull()
  })


  it('omits the reset action and layout picker from auxiliary edit surfaces', () => {
    windowKind.auxiliary = true
    $layoutEditMode.set(true)
    render(<TreeEditBar />)

    expect(screen.queryByText('Layouts')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Reset layout' })).toBeNull()
  })
})
