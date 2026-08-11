import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'

import {
  $backendUpdateChecking,
  $backendUpdateStatus,
  $updateOverlayTarget,
  resetUpdateApplyState,
  setUpdateOverlayOpen
} from '@/store/updates'

import { UpdatesOverlay } from './updates-overlay'

beforeEach(() => {
  resetUpdateApplyState()
  $backendUpdateChecking.set(false)
  $updateOverlayTarget.set('backend')
  setUpdateOverlayOpen(true)
})

afterEach(() => {
  setUpdateOverlayOpen(false)
  $backendUpdateStatus.set(null)
  cleanup()
})

describe('UpdatesOverlay', () => {
  it('shows a retryable check failure before managed-runtime guidance', () => {
    $backendUpdateStatus.set({
      supported: false,
      error: 'update-check-failed',
      message: 'Managed outside the desktop.',
      fetchedAt: Date.now()
    })

    render(<UpdatesOverlay />)

    expect(screen.getByText('Couldn’t check for updates')).toBeTruthy()
    expect(screen.getByRole('button', { name: 'Try again' })).toBeTruthy()
    expect(screen.queryByText('Update not available')).toBeNull()
  })
})
