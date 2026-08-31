// @vitest-environment jsdom

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { I18nProvider } from '@/i18n'

import { ConfirmDialog } from './confirm-dialog'

afterEach(cleanup)

describe('ConfirmDialog secondary action', () => {
  function renderWithSecondary() {
    const onConfirm = vi.fn()
    const onClose = vi.fn()
    const onSecondary = vi.fn()

    render(
      <ConfirmDialog
        onClose={onClose}
        onConfirm={onConfirm}
        open
        secondaryAction={{ label: 'Remove from sidebar', onClick: onSecondary }}
        title="Remove worktree?"
      />
    )

    return { onClose, onConfirm, onSecondary }
  }

  it('runs the secondary action and closes without confirming', async () => {
    const { onClose, onConfirm, onSecondary } = renderWithSecondary()

    fireEvent.click(await screen.findByRole('button', { name: 'Remove from sidebar' }))

    expect(onSecondary).toHaveBeenCalledTimes(1)
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(onConfirm).not.toHaveBeenCalled()
  })

  it('still opens focused on Confirm, so Enter confirms rather than picking the secondary', async () => {
    const { onConfirm, onSecondary } = renderWithSecondary()

    const dialog = await screen.findByRole('dialog')

    // eslint-disable-next-line no-restricted-globals -- asserting real focus requires the live document
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true))
    // eslint-disable-next-line no-restricted-globals -- asserting real focus requires the live document
    fireEvent.keyDown(document.activeElement!, { key: 'Enter' })

    await waitFor(() => expect(onConfirm).toHaveBeenCalledTimes(1))
    expect(onSecondary).not.toHaveBeenCalled()
  })
})

describe('desktop ConfirmDialog typed confirmation', () => {
  it('does not run until the exact phrase is entered', () => {
    const onConfirm = vi.fn()
    render(
      <I18nProvider configClient={null} initialLocale="en">
        <ConfirmDialog
          onClose={() => undefined}
          onConfirm={onConfirm}
          open
          title="Update Hermes"
          typedConfirmation="UPDATE"
        />
      </I18nProvider>
    )

    const confirm = screen.getByRole('button', { name: 'Confirm' })
    const input = screen.getByLabelText(/Type UPDATE to confirm/i)
    expect((confirm as HTMLButtonElement).disabled).toBe(true)

    fireEvent.change(input, { target: { value: 'UPDATE' } })
    expect((confirm as HTMLButtonElement).disabled).toBe(false)
    fireEvent.click(confirm)
    expect(onConfirm).toHaveBeenCalledOnce()
  })

  it('blocks repeat submission while a dismissing action is pending', async () => {
    let finish: (() => void) | undefined

    const onConfirm = vi.fn(
      () =>
        new Promise<void>(resolve => {
          finish = resolve
        })
    )

    render(
      <I18nProvider configClient={null} initialLocale="en">
        <ConfirmDialog
          dismissOnConfirm
          onClose={() => undefined}
          onConfirm={onConfirm}
          open
          title="Restart Hermes"
        />
      </I18nProvider>
    )

    const confirm = screen.getByRole('button', { name: 'Confirm' }) as HTMLButtonElement
    fireEvent.click(confirm)
    fireEvent.click(confirm)

    expect(confirm.disabled).toBe(true)
    expect(onConfirm).toHaveBeenCalledOnce()
    await act(async () => {
      finish?.()
    })
  })
})
