import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { I18nProvider } from '@/i18n'

import { QuickEntryApp } from './quick-entry-app'

describe('QuickEntryApp', () => {
  afterEach(() => {
    cleanup()
    Reflect.deleteProperty(window, 'hermesDesktop')
  })

  it('renders branded capture copy from the locale catalog', () => {
    ;(window as unknown as { hermesDesktop: object }).hermesDesktop = {
      quickEntry: {
        dismiss: () => undefined,
        onShown: () => () => undefined,
        onState: (callback: (payload: { connected: boolean; sessions: never[] }) => void) => {
          callback({ connected: true, sessions: [] })

          return () => undefined
        },
        submit: () => undefined
      }
    }

    render(
      <I18nProvider configClient={null}>
        <QuickEntryApp />
      </I18nProvider>
    )

    expect(screen.getByRole('textbox', { name: 'Quick Entry' }).getAttribute('placeholder')).toBe('Ask evaOS Agent…')
    expect(screen.getByRole('combobox', { name: 'Target session' }).textContent).toContain('Current chat')
    expect(screen.queryByText(/Hermes/)).toBeNull()
  })
})
