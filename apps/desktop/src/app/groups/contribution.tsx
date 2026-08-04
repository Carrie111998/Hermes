import { lazy } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'

import type { Contribution } from '@/contrib/types'

import { useGatewayRequest } from '../gateway/hooks/use-gateway-request'
import { groupRoomId, GROUPS_ROUTE, ROUTES_AREA, SIDEBAR_NAV_AREA } from '../routes'

const GroupsView = lazy(async () => ({ default: (await import('.')).GroupsView }))

function GroupsRouteContribution() {
  const { pathname } = useLocation()
  const navigate = useNavigate()
  const { requestGateway } = useGatewayRequest()

  return <GroupsView navigate={path => navigate(path)} request={requestGateway} roomId={groupRoomId(pathname)} />
}

export const GROUP_CONTRIBUTIONS = [
  {
    id: 'groups.route',
    area: ROUTES_AREA,
    source: 'core',
    data: { path: GROUPS_ROUTE },
    render: () => <GroupsRouteContribution />
  },
  {
    id: 'groups.room-route',
    area: ROUTES_AREA,
    source: 'core',
    data: { path: `${GROUPS_ROUTE}/:roomId` },
    render: () => <GroupsRouteContribution />
  },
  {
    id: 'groups',
    area: SIDEBAR_NAV_AREA,
    source: 'core',
    order: 30,
    data: { codicon: 'organization', label: 'Group chats', path: GROUPS_ROUTE }
  }
] satisfies Contribution[]
