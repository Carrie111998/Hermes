import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $statusbarVisible } from '@/store/statusbar-prefs'

import { StatusbarVisibilitySetting } from './statusbar-visibility-setting'

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      common: { off: 'Off', on: 'On' },
      settings: {
        appearance: {
          statusBarDesc: 'Keep the bottom status surface visible.',
          statusBarTitle: 'Status Bar'
        }
      }
    }
  })
}))

vi.mock('@/lib/haptics', () => ({ triggerHaptic: vi.fn() }))

beforeEach(() => {
  $statusbarVisible.set(false)
})

afterEach(cleanup)

describe('StatusbarVisibilitySetting', () => {
  it('exposes the existing status-bar preference in Appearance settings', () => {
    render(<StatusbarVisibilitySetting />)

    expect(screen.getByText('Status Bar')).toBeTruthy()
    expect(screen.getByText('Keep the bottom status surface visible.')).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'On' }))
    expect($statusbarVisible.get()).toBe(true)
  })
})
