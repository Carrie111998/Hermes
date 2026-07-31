import { describe, expect, it } from 'vitest'

import { registry } from '@/contrib/registry'

import { contributedRoutes, ROUTES_AREA, routeSessionId, SIDEBAR_NAV_AREA } from '../routes'

import { GROUP_CONTRIBUTIONS } from './contribution'

describe('group chat contributions', () => {
  it('registers its page and sidebar entry through the contribution seams', () => {
    const route = GROUP_CONTRIBUTIONS.find(item => item.area === ROUTES_AREA)
    const nav = GROUP_CONTRIBUTIONS.find(item => item.area === SIDEBAR_NAV_AREA)

    expect(route).toMatchObject({
      id: 'groups.route',
      source: 'core',
      data: { path: '/groups' }
    })
    expect(route?.render).toBeTypeOf('function')
    expect(GROUP_CONTRIBUTIONS.filter(item => item.area === ROUTES_AREA)).toHaveLength(2)
    expect(nav).toMatchObject({
      id: 'groups',
      source: 'core',
      data: { codicon: 'organization', label: 'Group chats', path: '/groups' }
    })
  })

  it('reserves the contributed nested room route from session parsing', () => {
    const dispose = registry.registerMany(GROUP_CONTRIBUTIONS)

    try {
      expect(contributedRoutes().map(route => route.path)).toEqual(expect.arrayContaining(['/groups', '/groups/:roomId']))
      expect(routeSessionId('/groups/room')).toBeNull()
      expect(routeSessionId('/groups/room/extra')).toBeNull()
    } finally {
      dispose()
    }
  })
})
