/** Reciprocal room control enrollment for creation and background reconciliation. */

import type { HostedRoomCapability } from './hosted-room-client'
import { requestForBot } from './routing'
import type { GroupMember, ProfileRoute } from './types'

export interface HostedRoomServerMember {
  display_name?: unknown
  handle?: unknown
  member_id?: unknown
  profile?: unknown
  target?: unknown
}

export interface HostedRoomServerState {
  authority_epoch?: unknown
  authority_gateway_id?: unknown
  disbanded_at?: unknown
  latest_seq?: unknown
  members?: unknown
  name?: unknown
  room_id?: unknown
}

interface CreatedControlMember {
  member: GroupMember
  connectionId: string
  callerInstallId: unknown
  targetProfile: unknown
}

type RequestConnection = (route: ProfileRoute, method: string, params: Record<string, unknown>) => Promise<unknown>

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
}

async function roomControlRequestId(roomId: string, memberId: string): Promise<string> {
  const material = new TextEncoder().encode(`room-control-v1\0${roomId}\0${memberId}`)
  const digest = await globalThis.crypto.subtle.digest('SHA-256', material)

  return `room-control:${Array.from(new Uint8Array(digest), byte => byte.toString(16).padStart(2, '0')).join('')}`
}

export function createHostedRoomControlEnrollment(requestHostedConnection: RequestConnection) {
  const enrolled = new Set<string>()
  const pending = new Map<string, number>()
  const retryAfter = new Map<string, number>()
  let queue: Promise<void> = Promise.resolve()
  let generation = 0
  let disposed = true

  function reset(active: boolean) {
    generation += 1
    disposed = !active
    enrolled.clear()
    pending.clear()
    retryAfter.clear()
    queue = Promise.resolve()
  }

  async function ensure(
    room: HostedRoomServerState,
    homeRoute: ProfileRoute,
    routes: Record<string, ProfileRoute>,
    capabilities: Record<string, HostedRoomCapability>,
    token: number
  ) {
    const isCurrent = () => !disposed && token === generation
    const roomId = String(room.room_id || '')
    const authorityEpoch = Number(room.authority_epoch || 0)
    const homeConnectionId = String(homeRoute.connectionId || '')

    if (
      !isCurrent() ||
      !roomId ||
      !Number.isSafeInteger(authorityEpoch) ||
      authorityEpoch < 1 ||
      capabilities[homeConnectionId]?.reciprocalControl !== true
    ) {
      return
    }

    for (const raw of Array.isArray(room.members) ? room.members : []) {
      const member = (record(raw) || {}) as HostedRoomServerMember
      const target = record(member.target)

      if (target?.kind !== 'peer') {
        continue
      }

      const memberId = String(member.member_id || '')
      const targetProfile = String(member.profile || member.member_id || 'default')
      const targetAuthority = String(target.installation_id || target.peer_id || '')

      const peerConnectionId = Object.entries(capabilities).find(
        ([, capability]) => capability.authorityId === targetAuthority
      )?.[0]

      const peerRoute = peerConnectionId ? routes[peerConnectionId] : null
      const key = `${roomId}:${authorityEpoch}:${memberId}:${targetAuthority}`

      if (
        !memberId ||
        !targetAuthority ||
        !peerConnectionId ||
        !peerRoute ||
        capabilities[peerConnectionId]?.reciprocalControl !== true ||
        enrolled.has(key) ||
        Number(retryAfter.get(key) || 0) > Date.now()
      ) {
        continue
      }

      try {
        const control = record(
          await requestHostedConnection(homeRoute, 'groups.control.invite', {
            room_id: roomId,
            member_id: memberId,
            caller_install_id: targetAuthority,
            request_id: await roomControlRequestId(roomId, memberId)
          })
        )

        if (!isCurrent()) {
          return
        }

        if (
          !control?.control_token ||
          !control.home_url ||
          !control.authority_gateway_id ||
          !control.authority_epoch ||
          !control.room_name ||
          !control.member_count ||
          !control.expires_at
        ) {
          throw new Error('Group Chat control invitation is incomplete.')
        }

        await requestHostedConnection(peerRoute, 'groups.control.register', {
          room_id: roomId,
          member_id: memberId,
          authority_gateway_id: control.authority_gateway_id,
          authority_epoch: control.authority_epoch,
          room_name: control.room_name,
          member_count: control.member_count,
          profile: targetProfile,
          home_url: control.home_url,
          control_token: control.control_token,
          expires_at: control.expires_at
        })

        if (!isCurrent()) {
          return
        }

        enrolled.add(key)
        retryAfter.delete(key)
      } catch {
        if (isCurrent()) {
          retryAfter.set(key, Date.now() + 30_000)
        }
      }
    }
  }

  function schedule(
    room: HostedRoomServerState,
    homeRoute: ProfileRoute,
    routes: Record<string, ProfileRoute>,
    capabilities: Record<string, HostedRoomCapability>
  ) {
    const key = `${String(room.room_id || '')}:${Number(room.authority_epoch || 0)}`

    if (pending.has(key)) {
      return queue
    }

    const token = generation
    pending.set(key, token)
    queue = queue
      .then(async () => {
        if (!disposed && token === generation) {
          await ensure(room, homeRoute, routes, capabilities, token)
        }
      })
      .catch(() => undefined)
      .finally(() => {
        if (pending.get(key) === token) {
          pending.delete(key)
        }
      })

    return queue
  }

  // Creation is awaited by its caller, independently of the background lifecycle.
  // Keep only control identities here; execution grants stay with runtime cleanup.
  function forCreation(homeRoute: ProfileRoute, roomId: string, capabilities: Record<string, HostedRoomCapability>) {
    const members = new Map<unknown, CreatedControlMember>()

    function add(
      memberId: unknown,
      member: GroupMember,
      connectionId: string,
      callerInstallId: unknown,
      targetProfile: unknown
    ) {
      members.set(memberId, { member, connectionId, callerInstallId, targetProfile })
    }

    async function enroll(memberId: unknown) {
      const entry = members.get(memberId)!
      const homeControl = capabilities[String(homeRoute.connectionId || '')]?.reciprocalControl === true
      const peerControl = capabilities[entry.connectionId]?.reciprocalControl === true

      if (homeControl && peerControl) {
        const control = record(
          await requestHostedConnection(homeRoute, 'groups.control.invite', {
            room_id: roomId,
            member_id: memberId,
            caller_install_id: entry.callerInstallId,
            request_id: await roomControlRequestId(roomId, String(memberId))
          })
        )

        if (
          !control?.control_token ||
          !control.home_url ||
          !control.authority_gateway_id ||
          !control.authority_epoch ||
          !control.room_name ||
          !control.member_count ||
          !control.expires_at
        ) {
          throw new Error('One selected Bot could not prepare remote Group Chat control.')
        }

        await requestForBot(entry.member, 'groups.control.register', {
          room_id: roomId,
          member_id: memberId,
          authority_gateway_id: control.authority_gateway_id,
          authority_epoch: control.authority_epoch,
          room_name: control.room_name,
          member_count: control.member_count,
          profile: entry.targetProfile,
          home_url: control.home_url,
          control_token: control.control_token,
          expires_at: control.expires_at
        })
      }
    }

    async function revoke() {
      await Promise.allSettled(
        [...members].map(([memberId, entry]) =>
          requestForBot(entry.member, 'groups.control.revoke', {
            room_id: roomId,
            member_id: memberId
          })
        )
      )
    }

    return { add, enroll, revoke }
  }

  return { forCreation, reset, schedule }
}
