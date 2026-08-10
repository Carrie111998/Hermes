import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import type { CustomEndpoint } from '@/types/hermes'

const activateCustomEndpoint = vi.fn()
const deleteCustomEndpoint = vi.fn()
const getCustomEndpoints = vi.fn()
const saveCustomEndpoint = vi.fn()
const validateCustomEndpoint = vi.fn()

// Radix Select uses DOM APIs that jsdom does not implement.
beforeAll(() => {
  Element.prototype.scrollIntoView = vi.fn()
  Element.prototype.hasPointerCapture = vi.fn(() => false)
  Element.prototype.releasePointerCapture = vi.fn()
})

vi.mock('@/hermes', () => ({
  activateCustomEndpoint: (endpointId: string) => activateCustomEndpoint(endpointId),
  deleteCustomEndpoint: (endpointId: string) => deleteCustomEndpoint(endpointId),
  getCustomEndpoints: () => getCustomEndpoints(),
  saveCustomEndpoint: (payload: unknown) => saveCustomEndpoint(payload),
  validateCustomEndpoint: (payload: unknown) => validateCustomEndpoint(payload)
}))

vi.mock('@/lib/haptics', () => ({
  triggerHaptic: vi.fn()
}))

function endpoint(patch: Partial<CustomEndpoint> = {}): CustomEndpoint {
  return {
    api_mode: 'auto',
    base_url: 'https://claude.example.com',
    discover_models: true,
    has_api_key: true,
    id: 'claude-proxy',
    is_current: true,
    model: 'claude-opus-5',
    models: ['claude-opus-5'],
    name: 'Claude Proxy',
    ...patch
  }
}

beforeEach(() => {
  const configured = endpoint()
  getCustomEndpoints.mockResolvedValue({
    current: {
      base_url: configured.base_url,
      model: configured.model,
      provider: configured.id
    },
    endpoints: [configured]
  })
  saveCustomEndpoint.mockResolvedValue({
    current: {
      base_url: configured.base_url,
      model: configured.model,
      provider: configured.id
    },
    endpoints: [endpoint({ api_mode: 'anthropic_messages' })],
    id: configured.id,
    ok: true
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('CustomEndpointsSettings', () => {
  it('saves the API compatibility mode selected in Desktop', async () => {
    const { CustomEndpointsSettings } = await import('./custom-endpoints-settings')

    await act(async () => {
      render(<CustomEndpointsSettings />)
    })

    const apiModeSelect = await screen.findByRole('combobox', { name: 'API Mode' })
    fireEvent.click(apiModeSelect)
    fireEvent.click(await screen.findByRole('option', { name: 'Anthropic Messages' }))
    fireEvent.click(screen.getByRole('button', { name: 'Save' }))

    await waitFor(() =>
      expect(saveCustomEndpoint).toHaveBeenCalledWith(
        expect.objectContaining({
          api_mode: 'anthropic_messages',
          base_url: 'https://claude.example.com',
          model: 'claude-opus-5'
        })
      )
    )
  })
})
