import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, useLocation } from 'react-router-dom'
import { afterEach, describe, expect, it, vi } from 'vitest'

vi.mock('./providers-settings', () => ({
  PROVIDER_VIEWS: ['accounts', 'keys', 'fleet', 'custom-endpoints'],
  ProvidersSettings: ({ view }: { view: string }) => <div data-testid="provider-view">{view}</div>
}))

function LocationProbe() {
  const location = useLocation()

  return <output data-testid="location">{location.search}</output>
}

afterEach(cleanup)

describe('Settings provider navigation', () => {
  it('deep-links to pview=fleet and preserves sidebar navigation behavior', async () => {
    const { SettingsView } = await import('./index')

    render(
      <MemoryRouter initialEntries={['/settings?tab=providers&pview=fleet']}>
        <SettingsView onClose={vi.fn()} />
        <LocationProbe />
      </MemoryRouter>
    )

    expect(screen.getByTestId('provider-view').textContent).toBe('fleet')
    expect(screen.getByTestId('location').textContent).toContain('pview=fleet')

    fireEvent.click(screen.getByRole('button', { name: 'API keys' }))

    await waitFor(() => expect(screen.getByTestId('provider-view').textContent).toBe('keys'))
    expect(screen.getByTestId('location').textContent).toContain('pview=keys')

    fireEvent.click(screen.getByRole('button', { name: 'Fleet Router' }))

    await waitFor(() => expect(screen.getByTestId('provider-view').textContent).toBe('fleet'))
    expect(screen.getByTestId('location').textContent).toContain('pview=fleet')
  })
})
