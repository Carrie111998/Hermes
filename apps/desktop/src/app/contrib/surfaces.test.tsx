import { act, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $gateway } from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import { $gatewayState } from '@/store/session'

import { ChatRoutesSurface } from './surfaces'
import type { WiringActions } from './types'

vi.mock('@/app/chat', () => ({
  ChatView: ({ modelMenuContent }: { modelMenuContent?: ReactNode }) => <>{modelMenuContent}</>
}))
vi.mock('@/app/chat/sidebar', () => ({ ChatSidebar: () => null }))
vi.mock('@/app/right-sidebar/terminal/chrome', () => ({ TerminalPaneChrome: () => null }))
vi.mock('@/app/shell/statusbar-controls', () => ({ StatusbarControls: () => null }))

vi.mock('@/app/routes', () => ({
  contributedRoutes: () => [],
  NEW_CHAT_ROUTE: '/',
  ROUTES_AREA: 'routes',
  sessionRoute: (sessionId: string) => `/${sessionId}`
}))

vi.mock('@/app/shell/model-menu-panel', () => ({
  ModelMenuPanel: ({ gateway, profile }: { gateway?: { label?: string }; profile: string }) => (
    <div data-testid="model-menu">{`${profile}:${gateway?.label ?? 'none'}`}</div>
  )
}))

vi.mock('@/contrib/react/use-contributions', () => ({ useContributions: () => undefined }))
vi.mock('./latest-actions', () => ({ latestChatActions: () => ({}) }))

describe('ChatRoutesSurface', () => {
  afterEach(() => {
    act(() => {
      $gateway.set(null)
      $activeGatewayProfile.set('default')
      $gatewayState.set('closed')
    })
  })

  it('rebinds the model menu to the active gateway when profiles swap without a connection-state transition', () => {
    const primaryGateway = { label: 'primary' }
    const profileGateway = { label: 'hermes-exec' }

    $gateway.set(primaryGateway as never)
    $activeGatewayProfile.set('default')
    $gatewayState.set('open')

    const actions = {
      getGateway: vi.fn(() => primaryGateway),
      requestGateway: vi.fn(),
      selectModel: vi.fn()
    } as unknown as WiringActions

    render(
      <MemoryRouter>
        <ChatRoutesSurface actions={actions} />
      </MemoryRouter>
    )

    expect(screen.getByTestId('model-menu').textContent).toBe('default:primary')

    act(() => {
      $gateway.set(profileGateway as never)
      $activeGatewayProfile.set('hermes-exec')
    })

    expect(screen.getByTestId('model-menu').textContent).toBe('hermes-exec:hermes-exec')
    expect(actions.getGateway).not.toHaveBeenCalled()
  })
})
