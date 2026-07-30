import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import {
  $modelPickerOpen,
  $modelPickerScope,
  setActiveSessionId,
  setModelPickerOpen
} from '@/store/session'
import { $routingBudget, $routingCapability } from '@/store/turn-routing'

import { RoutePill } from './route-pill'

beforeEach(() => {
  $activeGatewayProfile.set('default')
  setActiveSessionId('session-1')
  setModelPickerOpen(false)
  $routingCapability.set({
    available: true,
    error: null,
    loading: false,
    mode: 'observe',
    profile: 'default',
    version: 1
  })
  $routingBudget.set({ available: false, error: null, generation: 0, loading: false, profile: 'default' })
  $gateway.set({
    request: vi.fn(async (method: string, params: Record<string, unknown>) => {
      if (method === 'config.get' && params.key === 'routing_mode') {
        return { capability_version: 1, key: 'routing_mode', value: 'observe' }
      }
      if (method === 'config.get' && params.key === 'routing_budget') {
        return {
          available_slots: 0,
          committed_slots: 0,
          cooldown_reason_code: null,
          cooldown_until_at: null,
          key: 'routing_budget',
          reserved_slots: 0,
          scope: 'grok',
          week_key: '2026-07-27',
          weekly_limit: 0
        }
      }
      throw new Error(`unexpected request: ${method}`)
    })
  } as never)
})

afterEach(() => {
  cleanup()
  setModelPickerOpen(false)
  setActiveSessionId(null)
  $gateway.set(null)
  $activeGatewayProfile.set('default')
})

describe('RoutePill next-turn override', () => {
  it('does not paint routing authority cached for another profile', () => {
    $activeGatewayProfile.set('work')
    $routingCapability.set({
      available: true,
      error: null,
      loading: false,
      mode: 'observe',
      profile: 'default',
      version: 1
    })
    $gateway.set({ request: vi.fn(() => new Promise(() => undefined)) } as never)

    render(<RoutePill disabled={false} />)

    expect(screen.queryByLabelText(/Routing: Observe/)).toBeNull()
  })

  it('opens the shared model picker in one-turn scope', async () => {
    render(<RoutePill disabled={false} />)

    await waitFor(() => expect(screen.getByLabelText('Routing: Observe').hasAttribute('disabled')).toBe(false))
    fireEvent.pointerDown(screen.getByLabelText('Routing: Observe'), {
      button: 0,
      ctrlKey: false,
      pointerType: 'mouse'
    })
    fireEvent.click(await screen.findByRole('menuitem', { name: 'Override next turn…' }))

    expect($modelPickerOpen.get()).toBe(true)
    expect($modelPickerScope.get()).toBe('once')
  })
})
