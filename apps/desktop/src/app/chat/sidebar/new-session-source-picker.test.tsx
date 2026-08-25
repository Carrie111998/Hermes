import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { DesktopRegistryConnection } from '@/global'

import { NewSessionSourcePicker } from './new-session-source-picker'

// Radix menus use pointer capture; jsdom does not implement it.
Element.prototype.hasPointerCapture ??= () => false
Element.prototype.setPointerCapture ??= () => undefined
Element.prototype.releasePointerCapture ??= () => undefined
Element.prototype.scrollIntoView ??= () => undefined
globalThis.ResizeObserver ??= class ResizeObserver {
  disconnect() {}
  observe() {}
  unobserve() {}
}

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      sidebar: {
        nav: {
          'new-session': 'New session'
        }
      },
      settings: {
        connections: {
          kindCloud: 'Hermes Cloud',
          kindLocal: 'Local',
          kindRemote: 'Remote gateway',
          kindSsh: 'SSH',
          title: 'Registered gateways'
        }
      }
    }
  })
}))

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

const connection = (
  id: string,
  label: string,
  kind: DesktopRegistryConnection['kind'] = 'remote'
): DesktopRegistryConnection => ({
  id,
  kind,
  label,
  tokenPreview: null,
  tokenSet: false
})

describe('NewSessionSourcePicker', () => {
  it('lists every registered source with its kind and surface nothing when empty', async () => {
    render(
      <NewSessionSourcePicker
        activeConnectionId="local"
        connections={[
          connection('local', 'This device', 'local'),
          connection('homelab', 'Homelab', 'remote'),
          connection('work', 'Work laptop', 'ssh')
        ]}
        onPick={vi.fn()}
        trigger={<button>New session</button>}
      />
    )

    fireEvent.pointerDown(screen.getByRole('button', { name: 'New session' }), {
      button: 0,
      pointerType: 'mouse'
    })

    await waitFor(() => expect(screen.getByText('This device')).toBeDefined())
    expect(screen.getByText('Homelab')).toBeDefined()
    expect(screen.getByText('Work laptop')).toBeDefined()
    // Kind labels render per connection.
    expect(screen.getAllByText('Local').length).toBeGreaterThan(0)
    expect(screen.getAllByText('Remote gateway').length).toBe(1)
    expect(screen.getAllByText('SSH').length).toBe(1)
  })

  it('calls onPick with the chosen source id', async () => {
    const onPick = vi.fn()

    render(
      <NewSessionSourcePicker
        activeConnectionId="local"
        connections={[connection('local', 'This device', 'local'), connection('homelab', 'Homelab', 'remote')]}
        onPick={onPick}
        trigger={<button>New session</button>}
      />
    )

    fireEvent.pointerDown(screen.getByRole('button', { name: 'New session' }), {
      button: 0,
      pointerType: 'mouse'
    })
    await waitFor(() => expect(screen.getByText('Homelab')).toBeDefined())
    fireEvent.click(screen.getByText('Homelab'))

    await waitFor(() => expect(onPick).toHaveBeenCalledWith('homelab'))
  })
})
