import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'
import {
  $newWindowBackendPolicy,
  setNewWindowBackendPolicy
} from '@/store/window-backend-picker'

import { NewWindowBackendSetting } from './new-window-backend-setting'

describe('NewWindowBackendSetting', () => {
  beforeEach(() => {
    Object.defineProperty(Element.prototype, 'scrollIntoView', {
      configurable: true,
      value: () => {}
    })
    window.localStorage.clear()
    setNewWindowBackendPolicy('inherit')
  })

  afterEach(() => {
    cleanup()
    delete (Element.prototype as { scrollIntoView?: () => void }).scrollIntoView
  })

  it('shows inheritance as the default and persists a different policy', async () => {
    render(
      <I18nProvider>
        <NewWindowBackendSetting />
      </I18nProvider>
    )

    expect(screen.getByText('New window backend')).toBeTruthy()
    expect(screen.getByText('Same as current window')).toBeTruthy()

    fireEvent.click(screen.getByRole('combobox', { name: 'New window backend' }))
    fireEvent.click(await screen.findByRole('option', { name: 'Ask every time' }))

    expect($newWindowBackendPolicy.get()).toBe('ask')
    expect(window.localStorage.getItem('hermes.desktop.newWindowBackendPolicy')).toBe('ask')
  })
})
