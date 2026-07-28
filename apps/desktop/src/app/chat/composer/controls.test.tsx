import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter, Route, Routes, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ChatBarState } from '@/app/chat/composer/types'
import { I18nProvider } from '@/i18n'

import { ComposerControls } from './controls'

vi.mock('./model-pill', () => ({ ModelPill: () => <button aria-label="Switch model" type="button" /> }))

const state: ChatBarState = {
  model: { canSwitch: false, model: '', provider: '' },
  tools: { enabled: false, label: '' },
  voice: { active: false, enabled: false }
}

function renderControls(overrides: Partial<React.ComponentProps<typeof ComposerControls>> = {}) {
  return render(
    <MemoryRouter initialEntries={['/']}>
      <I18nProvider configClient={null} initialLocale="en">
        <Routes>
          <Route
            element={
              <>
                <ComposerControls
                  autoSpeak={false}
                  busy={false}
                  busyAction="stop"
                  canSubmit={true}
                  conversation={{
                    active: false,
                    level: 0,
                    muted: false,
                    onEnd: vi.fn(),
                    onStart: vi.fn(),
                    onStopTurn: vi.fn(),
                    onToggleMute: vi.fn(),
                    status: 'idle'
                  }}
                  disabled={false}
                  hasComposerPayload={true}
                  onDictate={vi.fn()}
                  onQueue={vi.fn()}
                  onToggleAutoSpeak={vi.fn()}
                  state={state}
                  voiceStatus="idle"
                  {...overrides}
                />
                <LocationProbe />
              </>
            }
            path="*"
          />
        </Routes>
      </I18nProvider>
    </MemoryRouter>
  )
}

function LocationProbe() {
  const location = useLocation()

  return <div data-testid="location">{`${location.pathname}${location.search}`}</div>
}

async function expectShortcutTooltip(label: string, shortcut: string) {
  fireEvent.pointerMove(screen.getByLabelText(label), { pointerType: 'mouse' })

  const tooltip = await screen.findByRole('tooltip')

  expect(tooltip.textContent).toContain(label)
  expect(tooltip.textContent).toContain(shortcut)
}

afterEach(() => {
  cleanup()
})

describe('ComposerControls shortcut tooltips', () => {
  it('shows Enter for Send', async () => {
    renderControls()

    await expectShortcutTooltip('Send', '↵')
  })

  it('shows Enter for Steer', async () => {
    renderControls({ busy: true, busyAction: 'steer' })

    await expectShortcutTooltip('Steer the current run', '↵')
  })

  it('shows Ctrl+Enter for Queue', async () => {
    renderControls({ busy: true, busyAction: 'queue' })

    await expectShortcutTooltip('Queue message', 'Ctrl+↵')
  })
})

describe('ComposerControls Atlas shortcuts', () => {
  it('routes Tools, Skills, and Artifacts through React Router', () => {
    renderControls()

    fireEvent.click(screen.getByRole('link', { name: 'Tools' }))
    expect(screen.getByTestId('location').textContent).toBe('/skills?tab=toolsets')

    fireEvent.click(screen.getByRole('link', { name: 'Skills' }))
    expect(screen.getByTestId('location').textContent).toBe('/skills')

    fireEvent.click(screen.getByRole('link', { name: 'Artifacts' }))
    expect(screen.getByTestId('location').textContent).toBe('/artifacts')
  })

  it('keeps important composer controls visible alongside shortcuts', () => {
    renderControls()

    expect(screen.getByRole('button', { name: 'Switch model' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Voice dictation' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Read replies aloud' })).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Send' })).toBeTruthy()
  })
})
