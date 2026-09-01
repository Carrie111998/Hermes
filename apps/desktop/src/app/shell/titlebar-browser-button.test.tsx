import { cleanup, fireEvent, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import { closeRightRail, $previewTabs } from '@/store/preview'

import { TitlebarControls } from './titlebar-controls'

function renderTitlebar() {
  return render(
    <MemoryRouter initialEntries={['/chat']}>
      <I18nProvider configClient={null} initialLocale="en">
        <TitlebarControls onOpenSettings={vi.fn()} />
      </I18nProvider>
    </MemoryRouter>
  )
}

beforeEach(() => {
  closeRightRail()
})

afterEach(() => {
  cleanup()
})

describe('titlebar Browser button', () => {
  // The whole point of the change: browsing has a permanent door, not one you
  // have to know a hotkey to find.
  it('is always present in the system cluster', () => {
    const rendered = renderTitlebar()

    expect(rendered.getByRole('button', { name: 'Browser' })).toBeTruthy()
  })

  it('opens a Browser tab when clicked', () => {
    const rendered = renderTitlebar()

    expect($previewTabs.get()).toHaveLength(0)

    fireEvent.click(rendered.getByRole('button', { name: 'Browser' }))

    const tabs = $previewTabs.get()

    expect(tabs).toHaveLength(1)
    expect(tabs[0]?.target.kind).toBe('url')
  })

  // It calls the same `openBrowserTab` the keybind and the palette call, so it
  // must behave the same: re-front, never stack duplicates.
  it('re-fronts the Browser it already has instead of stacking', () => {
    const rendered = renderTitlebar()
    const button = rendered.getByRole('button', { name: 'Browser' })

    fireEvent.click(button)
    fireEvent.click(button)
    fireEvent.click(button)

    expect($previewTabs.get()).toHaveLength(1)
  })
})
