import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const mocks = vi.hoisted(() => ({
  getCustomEndpoints: vi.fn()
}))

vi.mock('@/hermes', () => ({
  activateCustomEndpoint: vi.fn(),
  deleteCustomEndpoint: vi.fn(),
  getCustomEndpoints: () => mocks.getCustomEndpoints(),
  saveCustomEndpoint: vi.fn(),
  validateCustomEndpoint: vi.fn()
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      onboarding: {
        localEndpointTrust: 'Translated private endpoint trust warning.'
      }
    }
  })
}))

import { CustomEndpointsSettings } from './custom-endpoints-settings'

describe('CustomEndpointsSettings', () => {
  beforeEach(() => {
    mocks.getCustomEndpoints.mockResolvedValue({ endpoints: [] })
  })

  afterEach(() => {
    cleanup()
    vi.clearAllMocks()
  })

  it('renders the translated private-network trust warning', async () => {
    render(<CustomEndpointsSettings />)

    expect(await screen.findByText('Translated private endpoint trust warning.')).toBeTruthy()
  })
})
