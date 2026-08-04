import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { act } from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'
import type { ContinueOnPhoneResult } from '@/lib/continue-on-phone'

import { ContinueOnPhoneDialog, ContinueOnPhoneDialogHost } from './continue-on-phone-dialog'
import { closeContinueOnPhone } from './continue-on-phone-state'
import { SessionActionsMenu } from './sidebar/session-actions-menu'

const openExternal = vi.fn().mockResolvedValue(undefined)

beforeEach(() => {
  closeContinueOnPhone()
  openExternal.mockClear()
  window.hermesDesktop = {
    ...(window.hermesDesktop ?? {}),
    openExternal
  } as Window['hermesDesktop']
})

function renderDialog(overrides: Partial<React.ComponentProps<typeof ContinueOnPhoneDialog>> = {}) {
  return render(
    <I18nProvider>
      <ContinueOnPhoneDialog
        generateQr={vi.fn().mockResolvedValue('data:image/png;base64,qr')}
        onOpenChange={vi.fn()}
        open
        profile="work"
        resolveUrl={vi.fn().mockResolvedValue({
          expiresAt: Date.now() + 120_000,
          ok: true,
          url: 'https://hermes.example.com/handoff#ticket=single-use-ticket'
        })}
        sessionId="session-42"
        {...overrides}
      />
    </I18nProvider>
  )
}

describe('ContinueOnPhoneDialog', () => {
  it('is reachable from the session actions menu', async () => {
    render(
      <I18nProvider>
        <SessionActionsMenu sessionId="session-42" title="Research session">
          <button type="button">Session menu</button>
        </SessionActionsMenu>
        <ContinueOnPhoneDialogHost />
      </I18nProvider>
    )

    fireEvent.pointerDown(screen.getByRole('button', { name: 'Session menu' }), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse'
    })
    fireEvent.click(await screen.findByText('Link phone'))

    expect(await screen.findByRole('dialog', { name: 'Link phone' })).toBeTruthy()
  })

  it('shows a scannable continuation link and can open the same URL', async () => {
    const resolveUrl = vi.fn().mockResolvedValue({
      expiresAt: Date.now() + 120_000,
      ok: true,
      url: 'https://hermes.example.com/handoff#ticket=single-use-ticket'
    })

    const generateQr = vi.fn().mockResolvedValue('data:image/png;base64,qr')

    renderDialog({ generateQr, resolveUrl })

    const qr = await screen.findByRole('img', { name: 'QR code for this Hermes session' })
    expect(qr.getAttribute('src')).toBe('data:image/png;base64,qr')
    expect(resolveUrl).toHaveBeenCalledWith('session-42', 'work')
    expect(generateQr).toHaveBeenCalledWith('https://hermes.example.com/handoff#ticket=single-use-ticket')

    fireEvent.click(screen.getByRole('button', { name: 'Open in browser' }))

    await waitFor(() =>
      expect(openExternal).toHaveBeenCalledWith('https://hermes.example.com/handoff#ticket=single-use-ticket')
    )
  })

  it('shows a recoverable error when secure remote access is unavailable', async () => {
    const resolveUrl = vi.fn().mockResolvedValue({ ok: false, reason: 'browser-auth-not-supported' })

    renderDialog({ resolveUrl })

    expect(await screen.findByText('Browser sign-in is not supported')).toBeTruthy()
    expect(
      screen.getByText('This dashboard uses token-only access. Configure browser sign-in to continue on a phone.')
    ).toBeTruthy()

    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull()
  })

  it.each([
    ['not-configured', 'Remote access is not configured', false],
    ['insecure-url', 'Remote access URL is not secure', false],
    ['unreachable', 'Remote access is unavailable', true],
    ['handoff-failed', 'Could not create a phone code', true]
  ] as const)('explains %s without an unsafe recovery loop', async (reason, title, canRetry) => {
    renderDialog({ resolveUrl: vi.fn().mockResolvedValue({ ok: false, reason }) })

    expect(await screen.findByText(title)).toBeTruthy()
    expect(Boolean(screen.queryByRole('button', { name: 'Retry' }))).toBe(canRetry)
  })

  it('removes an expired QR code and only mints a replacement when asked', async () => {
    vi.useFakeTimers()
    vi.setSystemTime(new Date('2026-08-03T00:00:00Z'))

    const resolveUrl = vi
      .fn()
      .mockResolvedValueOnce({
        expiresAt: Date.now() + 2_000,
        ok: true,
        url: 'https://hermes.example.com/handoff#ticket=first-ticket'
      })
      .mockResolvedValueOnce({
        expiresAt: Date.now() + 120_000,
        ok: true,
        url: 'https://hermes.example.com/handoff#ticket=second-ticket'
      })

    try {
      renderDialog({ resolveUrl })

      await act(async () => {})
      expect(screen.getByRole('img', { name: 'QR code for this Hermes session' })).toBeTruthy()
      expect(screen.getByText('Code expires in 2s')).toBeTruthy()

      await act(async () => {
        await vi.advanceTimersByTimeAsync(2_000)
      })

      expect(screen.queryByRole('img', { name: 'QR code for this Hermes session' })).toBeNull()
      expect(screen.getByText('This code has expired')).toBeTruthy()
      expect(resolveUrl).toHaveBeenCalledTimes(1)

      fireEvent.click(screen.getByRole('button', { name: 'Create new code' }))

      await act(async () => {})
      expect(screen.getByText(/^Code expires in 11[89]s$/)).toBeTruthy()
      expect(screen.getByText('https://hermes.example.com/handoff#ticket=second-ticket')).toBeTruthy()
      expect(resolveUrl).toHaveBeenCalledTimes(2)
    } finally {
      vi.useRealTimers()
    }
  })

  it('clears the current code as soon as the dialog is closed', async () => {
    const onOpenChange = vi.fn()

    renderDialog({ onOpenChange })

    expect(await screen.findByRole('img', { name: 'QR code for this Hermes session' })).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Close' }))

    expect(onOpenChange).toHaveBeenCalledWith(false)
    expect(screen.queryByRole('img', { name: 'QR code for this Hermes session' })).toBeNull()
  })

  it('restarts cleanly after close and ignores an old request result', async () => {
    let firstResolve: (value: ContinueOnPhoneResult) => void = () => undefined

    const resolveUrl = vi
      .fn()
      .mockImplementationOnce(
        () =>
          new Promise(resolve => {
            firstResolve = resolve
          })
      )
      .mockResolvedValueOnce({
        expiresAt: Date.now() + 120_000,
        ok: true,
        url: 'https://hermes.example.com/handoff#ticket=fresh-ticket'
      })

    const onOpenChange = vi.fn()
    const view = renderDialog({ onOpenChange, open: true, resolveUrl })

    view.rerender(
      <I18nProvider>
        <ContinueOnPhoneDialog
          generateQr={vi.fn().mockResolvedValue('data:image/png;base64,qr')}
          onOpenChange={onOpenChange}
          open={false}
          profile="work"
          resolveUrl={resolveUrl}
          sessionId="session-42"
        />
      </I18nProvider>
    )
    await act(async () => {
      firstResolve({
        expiresAt: Date.now() + 120_000,
        ok: true,
        url: 'https://hermes.example.com/handoff#ticket=stale-ticket'
      })
    })

    view.rerender(
      <I18nProvider>
        <ContinueOnPhoneDialog
          generateQr={vi.fn().mockResolvedValue('data:image/png;base64,qr')}
          onOpenChange={onOpenChange}
          open
          profile="work"
          resolveUrl={resolveUrl}
          sessionId="session-42"
        />
      </I18nProvider>
    )

    expect(await screen.findByRole('img', { name: 'QR code for this Hermes session' })).toBeTruthy()
    expect(screen.getByText('https://hermes.example.com/handoff#ticket=fresh-ticket')).toBeTruthy()
    expect(screen.queryByText('https://hermes.example.com/handoff#ticket=stale-ticket')).toBeNull()
  })
})
