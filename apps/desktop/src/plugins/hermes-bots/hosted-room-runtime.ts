/**
 * Gateway-hosted Group Chat runtime.
 *
 * RPC ownership lives here: capability negotiation, the durable command
 * outbox, monotonic replay, and the bounded refresh loop. Group state remains
 * owned by `group-chat.ts`; creation, round routing, and room UI call this
 * module through narrow verbs.
 */

import { atom, host } from '@hermes/plugin-sdk'
import type { PluginContext } from '@hermes/plugin-sdk'

import { $lastRoster } from './data'
import {
  $groupChats,
  applyHostedRoomAuthority,
  groupChatHostedGateway,
  mergeGroupChatSyncEntries,
  uniqueGroupChatName,
  updateGroupChat
} from './group-chat'
import {
  classifyHostedRoomCapability,
  createHostedRoomOutbox,
  createHostedRoomReplayState,
  deriveFriendlyHostedRoomStatus,
  isHostedRoomContinuityEligible,
  profileScopedRoomLinkEndpoint,
  reduceHostedRoomOutbox,
  replayHostedRoomPages,
  resolveAutonomousRoomPlan
} from './hosted-room-client'
import type {
  AutonomousRoomPlan,
  FriendlyHostedRoomStatus,
  HostedRoomCapability,
  HostedRoomCommand,
  HostedRoomOutbox,
  HostedRoomRouteResolution
} from './hosted-room-client'
import { botsText } from './i18n'
import { requestForBot } from './routing'
import type { GroupChat, GroupMember, GroupMessage, ProfileRoute } from './types'

export { describeAutonomousRoomPlan, describeHostedRoomCreationError } from './hosted-room-client'

const HOSTED_ROOM_OUTBOX_KEY = 'hosted-room-outbox-v1'
const HOSTED_ROOM_CLEANUP_KEY = 'hosted-room-cleanup-v1'
const HOSTED_ROOM_CLEANUP_LIMIT = 64
const HOSTED_ROOM_LIST_PAGE_SIZE = 500
const HOSTED_ROOM_LIST_MAX_PAGES = 4
const HOSTED_ROOM_SYNC_INTERVAL_MS = 5000
const HOSTED_ROOM_UNSUPPORTED_REPROBE_MS = 30_000

export const $hostedRoomCapabilities = atom<Record<string, HostedRoomCapability>>({})
export const $hostedRoomOutbox = atom<HostedRoomOutbox>(createHostedRoomOutbox())
export const $hostedRoomCleanup = atom<HostedRoomCleanup>({ version: 1, operations: [] })

const hostedAuthorityRoutes = new Map<string, ProfileRoute>()
const hostedRoomPollCache = new Map<string, string>()
const hostedRoomMutationGenerations = new Map<string, number>()
const hostedRoomLocallyDeleted = new Set<string>()
const hostedRoomInventoriedConnections = new Set<string>()
let hostedRoomSyncTimer: ReturnType<typeof setTimeout> | null = null
let hostedRoomSyncRunning = false
let hostedRoomSyncDisposed = true
let hostedOutboxDispatching = false
let hostedCleanupDispatching = false
let hostedRoomStorage: null | PluginContext['storage'] = null
let hostedRoomHooks: HostedRoomRuntimeHooks = {}
const hostedUnsupportedUntil = new Map<string, number>()

function hostedRoomMutationGeneration(roomId: string) {
  return Math.max(0, Number(hostedRoomMutationGenerations.get(String(roomId || '')) || 0))
}

/** Fence an asynchronous local send/Stop/delete against an older replay. */
export function beginHostedRoomMutation(roomId: string) {
  const id = String(roomId || '')
  const generation = hostedRoomMutationGeneration(id) + 1

  if (id) {
    hostedRoomMutationGenerations.set(id, generation)
  }

  return generation
}

export function hostedRoomMutationIsCurrent(roomId: string, generation: number) {
  const id = String(roomId || '')

  return Boolean(id) && !hostedRoomLocallyDeleted.has(id) && hostedRoomMutationGeneration(id) === generation
}

/** Keep an acknowledged local deletion invisible to stale in-flight polls. */
export function markHostedRoomLocallyDeleted(roomId: string) {
  const id = String(roomId || '')

  if (!id) {
    return
  }

  beginHostedRoomMutation(id)
  hostedRoomLocallyDeleted.add(id)
  hostedRoomPollCache.delete(id)
}

/** A projection-only room must not start a classic Desktop driver until each
 * member gateway has been inventoried. Existing local classic rooms carry
 * either a Desktop authority or non-projected member descriptors and remain
 * immediately usable. */
export function groupChatContinuityReady(room: GroupChat | null | undefined) {
  if (!room) {
    return true
  }

  if (groupChatHostedGateway(room)) {
    return room.hostedStatus?.state !== 'deleted'
  }

  if (!room.roomId) {
    return true
  }

  const members = Array.isArray(room.members) ? room.members : []

  if (!members.length || members.some(member => member.remoteSource !== true)) {
    return true
  }

  const connections = [...new Set(members.map(member => String(member.connectionId || '')).filter(Boolean))]

  return !connections.length || connections.every(connectionId => hostedRoomInventoriedConnections.has(connectionId))
}

export interface HostedRoomRuntimeHooks {
  renameGroupChat?: (oldName: string, newName: string, members: GroupMember[]) => Promise<null | string>
}

export interface HostedRoomProbe {
  capability: HostedRoomCapability | null
  capabilities: Record<string, HostedRoomCapability>
  eligible: boolean
  route: AutonomousRoomPlan
  routes: Record<string, ProfileRoute>
}

interface HostedRoomCleanupOperation {
  cancelId?: null | string
  connectionId: string
  grant?: null | string
  kind: 'home-disband' | 'peer-revoke'
  operationId: string
  ownerId: string
  profile?: null | string
  roomId?: null | string
  setupId: string
}

interface HostedRoomCleanup {
  operations: HostedRoomCleanupOperation[]
  version: 1
}

interface HostedRoomCreateInput {
  members: Array<{
    display_name?: string
    handle: string
    member_id: string
    profile: string
  }>
  name: string
  roomId: string
  route: HostedRoomRouteResolution
}

interface AutonomousHostedRoomMember {
  displayName?: string
  handle: string
  member: GroupMember
  profile: string
}

interface AutonomousHostedRoomCreateInput {
  members: AutonomousHostedRoomMember[]
  name: string
  probe: HostedRoomProbe
  roomId: string
}

interface HostedRoomServerMember {
  display_name?: unknown
  handle?: unknown
  member_id?: unknown
  profile?: unknown
  target?: unknown
}

interface HostedRoomServerState {
  authority_epoch?: unknown
  authority_gateway_id?: unknown
  disbanded_at?: unknown
  latest_seq?: unknown
  members?: unknown
  name?: unknown
  room_id?: unknown
}

function record(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as Record<string, unknown>) : null
}

function activeConnectionId() {
  return String(host.state.connectionId?.get?.() || host.activeConnectionId?.() || '')
}

async function hostedDefaultRoutes(): Promise<ProfileRoute[]> {
  if (typeof host.profileRoutes !== 'function') {
    return []
  }

  const routes = await host.profileRoutes()
  const byConnection = new Map<string, ProfileRoute>()

  for (const route of Array.isArray(routes) ? routes : []) {
    const profile = String(route?.targetProfile || route?.profile || '')
    const connectionId = String(route?.connectionId || '')

    if (!connectionId || profile !== 'default' || byConnection.has(connectionId)) {
      continue
    }

    byConnection.set(connectionId, route as ProfileRoute)
  }

  return [...byConnection.values()]
}

async function requestHostedConnection<T>(
  route: ProfileRoute,
  method: string,
  params: Record<string, unknown> = {}
): Promise<T> {
  if (!route?.connectionId || typeof host.requestProfile !== 'function') {
    throw new Error(botsText().group.hostRouteMissing)
  }

  return host.requestProfile(route, method, params) as Promise<T>
}

const hostedCleanupOwnerId =
  globalThis.crypto?.randomUUID?.() || `desktop-${Date.now()}-${Math.random().toString(36).slice(2)}`

function normalizeHostedRoomCleanup(value: unknown): HostedRoomCleanup {
  const candidate = record(value)
  const operations: HostedRoomCleanupOperation[] = []

  for (const raw of Array.isArray(candidate?.operations) ? candidate.operations : []) {
    const operation = record(raw)
    const operationId = String(operation?.operationId || '')
    const setupId = String(operation?.setupId || '')
    const kind = String(operation?.kind || '')
    const connectionId = String(operation?.connectionId || '')

    if (!operationId || !setupId || !connectionId || !['home-disband', 'peer-revoke'].includes(kind)) {
      continue
    }

    if (kind === 'home-disband' && !String(operation?.roomId || '')) {
      continue
    }

    if (kind === 'peer-revoke' && (!String(operation?.grant || '') || !String(operation?.profile || ''))) {
      continue
    }

    operations.push({
      operationId,
      setupId,
      kind: kind as HostedRoomCleanupOperation['kind'],
      connectionId,
      ownerId: String(operation?.ownerId || ''),
      roomId: kind === 'home-disband' ? String(operation?.roomId || '') : null,
      cancelId:
        kind === 'home-disband' ? String(operation?.cancelId || `rollback-${String(operation?.roomId || '')}`) : null,
      profile: kind === 'peer-revoke' ? String(operation?.profile || '') : null,
      grant: kind === 'peer-revoke' ? String(operation?.grant || '') : null
    })
  }

  return {
    version: 1,
    operations: operations.slice(-HOSTED_ROOM_CLEANUP_LIMIT)
  }
}

async function replaceHostedRoomCleanup(next: HostedRoomCleanup) {
  if (!hostedRoomStorage?.set) {
    throw new Error('Desktop storage is unavailable, so Group Chat setup cannot be secured.')
  }

  const previous = $hostedRoomCleanup.get()

  $hostedRoomCleanup.set(next)

  try {
    await hostedRoomStorage.set(HOSTED_ROOM_CLEANUP_KEY, next)
  } catch (error) {
    $hostedRoomCleanup.set(previous)
    throw error
  }
}

async function addHostedRoomCleanup(operation: Omit<HostedRoomCleanupOperation, 'ownerId'>) {
  const current = normalizeHostedRoomCleanup($hostedRoomCleanup.get())

  const next = normalizeHostedRoomCleanup({
    version: 1,
    operations: [
      ...current.operations.filter(entry => entry.operationId !== operation.operationId),
      {
        ...operation,
        ownerId: hostedCleanupOwnerId
      }
    ]
  })

  if (next.operations.length >= HOSTED_ROOM_CLEANUP_LIMIT && current.operations.length >= HOSTED_ROOM_CLEANUP_LIMIT) {
    throw new Error('Group Chat cleanup is pending. Reconnect the affected devices before creating another.')
  }

  await replaceHostedRoomCleanup(next)
}

async function releaseHostedRoomCleanup(setupId: string) {
  const current = normalizeHostedRoomCleanup($hostedRoomCleanup.get())

  await replaceHostedRoomCleanup({
    version: 1,
    operations: current.operations.filter(operation => operation.setupId !== setupId)
  })
}

async function armHostedRoomCleanup(setupId: string) {
  const current = normalizeHostedRoomCleanup($hostedRoomCleanup.get())

  await replaceHostedRoomCleanup({
    version: 1,
    operations: current.operations.map(operation =>
      operation.setupId === setupId
        ? {
            ...operation,
            ownerId: ''
          }
        : operation
    )
  })
}

async function hostedRouteForReference(connectionId: string, profile = 'default') {
  if (typeof host.profileRoutes !== 'function') {
    return null
  }

  const routes = await host.profileRoutes()

  return ((Array.isArray(routes) ? routes : []).find(route => {
    const routeProfile = String(route?.targetProfile || route?.profile || '')

    return String(route?.connectionId || '') === connectionId && routeProfile === profile
  }) || null) as ProfileRoute | null
}

function hostedCleanupAlreadySettled(operation: HostedRoomCleanupOperation, error: unknown) {
  const candidate = record(error)
  const inner = record(candidate?.error)
  const code = Number(candidate?.code ?? inner?.code)
  const message = String(candidate?.message || inner?.message || error || '')

  return operation.kind === 'home-disband' && code === 4113 && /hosted room not found|already disbanded/i.test(message)
}

async function dispatchHostedRoomCleanup() {
  if (hostedCleanupDispatching || hostedRoomSyncDisposed) {
    return
  }

  hostedCleanupDispatching = true

  try {
    for (const operation of normalizeHostedRoomCleanup($hostedRoomCleanup.get()).operations) {
      if (operation.ownerId === hostedCleanupOwnerId) {
        continue
      }

      const profile = operation.kind === 'peer-revoke' ? String(operation.profile || '') : 'default'
      const route = await hostedRouteForReference(operation.connectionId, profile)

      if (!route) {
        continue
      }

      try {
        if (operation.kind === 'home-disband') {
          await requestHostedConnection(route, 'groups.disband', {
            room_id: operation.roomId,
            cancel_id: operation.cancelId
          })
        } else {
          await requestHostedConnection(route, 'groups.peer.revoke', {
            grant: operation.grant,
            profile: operation.profile
          })
        }
      } catch (error) {
        if (!hostedCleanupAlreadySettled(operation, error)) {
          continue
        }
      }

      const latest = normalizeHostedRoomCleanup($hostedRoomCleanup.get())

      await replaceHostedRoomCleanup({
        version: 1,
        operations: latest.operations.filter(entry => entry.operationId !== operation.operationId)
      })
    }
  } finally {
    hostedCleanupDispatching = false
  }
}

async function withHostedRoomProbeTimeout<T>(task: Promise<T>, timeoutMs = 3000) {
  let timer: null | ReturnType<typeof setTimeout> = null

  try {
    return await Promise.race([
      task,
      new Promise<never>((_resolve, reject) => {
        timer = setTimeout(() => reject(new Error('Host check timed out')), timeoutMs)
      })
    ])
  } finally {
    if (timer !== null) {
      clearTimeout(timer)
    }
  }
}

function sourceLabel(connectionId: string) {
  const source = ($lastRoster.get() || []).find(row => String(row?.connectionId || '') === connectionId)

  return String(source?.connectionLabel || botsText().group.thisHost)
}

function hostedMemberDescriptors(
  room: HostedRoomServerState,
  homeConnectionId: string,
  existingMembers: GroupMember[],
  capabilities: Record<string, HostedRoomCapability>
): GroupMember[] {
  return (Array.isArray(room?.members) ? room.members : []).map(raw => {
    const member = (record(raw) || {}) as HostedRoomServerMember
    const profile = String(member.profile || member.member_id || 'default')
    const handle = String(member.handle || member.profile || 'hermes')
    const target = record(member.target)

    const targetAuthority =
      target?.kind === 'peer' ? String(target.installation_id || target.peer_id || '') : ''

    const prior = (existingMembers || []).find(
      candidate =>
        String(candidate?.handle || candidate?.name || '') === handle &&
        String(candidate?.targetProfile || candidate?.name || '') === profile
    )

    const peerConnectionId = targetAuthority
      ? Object.entries(capabilities).find(([, capability]) => capability.authorityId === targetAuthority)?.[0] || ''
      : ''

    const connectionId = targetAuthority ? peerConnectionId || String(prior?.connectionId || '') : homeConnectionId

    const connectionLabel = connectionId
      ? sourceLabel(connectionId)
      : String(prior?.connectionLabel || '')

    const sourceReachable = connectionId
      ? capabilities[connectionId]
        ? isHostedRoomContinuityEligible(capabilities[connectionId])
        : prior?.sourceReachable !== false
      : false

    return {
      name: profile,
      handle,
      title: String(member.display_name || ''),
      ...(connectionId
        ? {
            connectionId,
            connectionLabel,
            route: {
              connectionId,
              mode: 'remote',
              profile,
              targetProfile: profile
            }
          }
        : {
            sourceMissing: true,
            sourceReachable: false
          }),
      remoteSource: true,
      sourceScoped: true,
      sourceReachable,
      targetProfile: profile
    }
  })
}

function hostedRoomContinuityMode(room: HostedRoomServerState) {
  return (Array.isArray(room?.members) ? room.members : []).some(raw => record(record(raw)?.target)?.kind === 'peer')
    ? ('distributed' as const)
    : ('gateway' as const)
}

function markHostedConnectionUnavailable(connectionId: string, unsupported = false) {
  const connectionName = sourceLabel(connectionId)

  for (const [name, room] of Object.entries($groupChats.get())) {
    if (String(room?.hostedConnectionId || '') !== connectionId) {
      continue
    }

    updateGroupChat(
      name,
      current => ({
        ...current,
        running: false,
        hostedStatus: {
          state: unsupported ? 'unsupported' : 'offline',
          label: unsupported
            ? botsText().group.hostUpdateNeeded(connectionName)
            : botsText().group.hostedUnavailable(connectionName)
        },
        continuityIssue: unsupported ? null : botsText().group.hostReconnectToContinue(connectionName)
      }),
      {
        sync: false
      }
    )
  }
}

export function hostedRoomDriverDisplayStatus(
  replay: FriendlyHostedRoomStatus,
  driverValue: unknown,
  { stopping = false }: { stopping?: boolean } = {}
): FriendlyHostedRoomStatus {
  if (stopping) {
    return { ...replay, kind: 'stopping', canStop: false }
  }

  if (['failed', 'member-unavailable', 'needs-attention', 'needs-you', 'waiting'].includes(replay.kind)) {
    return replay
  }

  const driver = record(driverValue)
  const counts = record(driver?.counts)

  if (Number(counts?.queued || driver?.queued || 0) > 0) {
    return { ...replay, kind: 'queued', canStop: true }
  }

  if (driver?.working === true || replay.kind === 'working') {
    return { ...replay, kind: 'working', canStop: true }
  }

  return replay
}

function hostedStatus(status: FriendlyHostedRoomStatus, connectionName: string) {
  const b = botsText()
  const member = status.member || b.group.aBot

  const labels: Record<string, string> = {
    deleted: b.group.hostedDeleted,
    offline: b.group.hostedUnavailable(connectionName),
    queued: b.group.hostedQueued(connectionName),
    ready: b.roster.ready,
    stopping: b.group.hostedStopping,
    working: b.group.memberThinking(member),
    'member-unavailable': b.group.memberUnavailable(member),
    'needs-attention': b.group.memberNeedsAttention(member),
    failed: b.group.memberCouldNotRespond(member),
    waiting: b.group.memberRetryWhenOnline(member),
    stopped: b.group.hostedStopped,
    'needs-you': b.group.waitingForAnswer
  }

  return {
    state: status.kind,
    label: labels[status.kind] || b.roster.statusUnknown,
    ...(status.canRetry === undefined
      ? {}
      : {
          canRetry: status.canRetry
        }),
    ...(status.canStop === undefined
      ? {}
      : {
          canStop: status.canStop
        })
  }
}

function replayMessages(messages: ReturnType<typeof createHostedRoomReplayState>['messages']): GroupMessage[] {
  return messages.map(message => ({
    at: message.at,
    from: message.from,
    id: message.eventId,
    eventId: message.eventId,
    seq: message.seq,
    text: message.text,
    thread: message.thread
  }))
}

function isDisbanded(room: HostedRoomServerState) {
  return room.disbanded_at !== null && room.disbanded_at !== undefined
}

export function hostedRoomPollFingerprint(value: unknown) {
  const room = record(value)
  const revision = Math.max(0, Number(room?.revision || 0))
  const latestSeq = Math.max(0, Number(room?.latest_seq || 0))

  return `${revision}:${latestSeq}`
}

export function shouldRefreshHostedRoom(room: GroupChat | undefined, listed: unknown) {
  if (!room) {
    return true
  }

  const activeStates = new Set(['queued', 'sending', 'stopping', 'working'])

  const active =
    room.running === true ||
    activeStates.has(String(room.hostedStatus?.state || '')) ||
    $hostedRoomOutbox.get().commands.some(command => command.roomId === room.roomId && command.status !== 'failed')

  const fingerprint = hostedRoomPollFingerprint(listed)

  return active || hostedRoomPollCache.get(String(room.roomId || '')) !== fingerprint
}

/** Replay every hosted room only after plugin storage/ui_meta hydration has
 * settled. The contiguous cursor is persisted with the room, so reconnects
 * fetch only missing events and a gap never skips unseen history. */
export async function refreshHostedRooms() {
  if (hostedRoomSyncDisposed || hostedRoomSyncRunning) {
    return
  }

  hostedRoomSyncRunning = true

  try {
    const routes = await hostedDefaultRoutes()

    const capabilities = {
      ...$hostedRoomCapabilities.get()
    }

    for (const route of routes) {
      if (hostedRoomSyncDisposed) {
        return
      }

      const connectionId = String(route.connectionId)
      let capability: HostedRoomCapability

      const cached = capabilities[connectionId]

      if (cached?.kind === 'unsupported' && Number(hostedUnsupportedUntil.get(connectionId) || 0) > Date.now()) {
        capability = cached
      } else {
        try {
          capability = classifyHostedRoomCapability(await requestHostedConnection(route, 'groups.capabilities'), {
            connectionId
          })
        } catch (error) {
          capability = classifyHostedRoomCapability(
            {
              ok: false,
              error
            },
            {
              connectionId
            }
          )
        }

        if (capability.kind === 'unsupported') {
          hostedUnsupportedUntil.set(connectionId, Date.now() + HOSTED_ROOM_UNSUPPORTED_REPROBE_MS)
        } else {
          hostedUnsupportedUntil.delete(connectionId)
        }
      }

      if (hostedRoomSyncDisposed) {
        return
      }

      capabilities[connectionId] = capability

      if (!isHostedRoomContinuityEligible(capability) || !capability.authorityId) {
        if (capability.kind === 'unsupported') {
          hostedRoomInventoriedConnections.add(connectionId)
        }

        markHostedConnectionUnavailable(connectionId, capability.kind === 'unsupported')

        continue
      }

      hostedAuthorityRoutes.set(capability.authorityId, route)
      const listedRooms: unknown[] = []
      let listOffset = 0
      let listComplete = false

      try {
        for (let page = 0; page < HOSTED_ROOM_LIST_MAX_PAGES; page += 1) {
          const listed = await requestHostedConnection<Record<string, unknown>>(route, 'groups.list', {
            include_disbanded: true,
            limit: HOSTED_ROOM_LIST_PAGE_SIZE,
            offset: listOffset
          })

          const rows = Array.isArray(listed?.rooms) ? listed.rooms : []

          listedRooms.push(...rows)

          const nextOffset = Number(listed?.next_offset)

          if (!Number.isSafeInteger(nextOffset) || nextOffset <= listOffset) {
            listComplete = true

            break
          }

          listOffset = nextOffset
        }
      } catch {
        markHostedConnectionUnavailable(connectionId)

        continue
      }

      if (hostedRoomSyncDisposed) {
        return
      }

      const disbandedIds = new Set(
        listedRooms
          .map(raw => (record(raw) || {}) as HostedRoomServerState)
          .filter(isDisbanded)
          .map(room => String(room.room_id || ''))
          .filter(Boolean)
      )
      const caughtUpDisbandedIds = new Set<string>()

      for (const listedRaw of listedRooms) {
        const listedRoom = (record(listedRaw) || {}) as HostedRoomServerState
        const roomId = String(listedRoom.room_id || '')
        const serverName = String(listedRoom.name || '').trim()

        if (!roomId || !serverName || hostedRoomLocallyDeleted.has(roomId)) {
          continue
        }

        const existingEntry = Object.entries($groupChats.get()).find(
          ([, room]) => String(room?.roomId || '') === roomId
        )
        const includeDisbanded = isDisbanded(listedRoom)

        // A client that already joined the room must replay terminal events
        // committed while it was offline before painting the remote disband.
        // Unknown disbanded rooms remain invisible on newly connected clients.
        if (includeDisbanded && !existingEntry) {
          continue
        }

        if (!shouldRefreshHostedRoom(existingEntry?.[1], listedRoom)) {
          if (
            includeDisbanded &&
            Math.max(0, Number(existingEntry?.[1]?.hostedSeq || 0)) >=
              Math.max(0, Number(listedRoom.latest_seq || 0))
          ) {
            caughtUpDisbandedIds.add(roomId)
          }

          continue
        }

        const refreshGeneration = hostedRoomMutationGeneration(roomId)

        let stateResponse: Record<string, unknown>

        try {
          stateResponse = await requestHostedConnection(route, 'groups.state', {
            room_id: roomId,
            ...(includeDisbanded ? { include_disbanded: true } : {})
          })
        } catch {
          markHostedConnectionUnavailable(connectionId)

          continue
        }

        if (hostedRoomSyncDisposed) {
          return
        }

        if (!hostedRoomMutationIsCurrent(roomId, refreshGeneration)) {
          continue
        }

        const serverRoom = (record(stateResponse.room) || listedRoom) as unknown as HostedRoomServerState

        let existingName = existingEntry?.[0]
        let existing = existingEntry?.[1]
        const taken = new Set(Object.keys($groupChats.get()))

        let localName =
          existingName ||
          (taken.has(serverName)
            ? uniqueGroupChatName(`${serverName} (${sourceLabel(connectionId)})`, taken)
            : serverName)

        const renamePending = $hostedRoomOutbox
          .get()
          .commands.some(
            command => command.kind === 'rename' && command.roomId === roomId && command.status !== 'failed'
          )

        if (existingName && existingName !== serverName && !renamePending && hostedRoomHooks.renameGroupChat) {
          const occupant = $groupChats.get()[serverName]
          const renameTaken = new Set(taken)

          renameTaken.delete(existingName)

          const targetName =
            occupant && occupant.roomId !== roomId
              ? uniqueGroupChatName(`${serverName} (${sourceLabel(connectionId)})`, renameTaken)
              : serverName

          const renamed = await hostedRoomHooks.renameGroupChat(
            existingName,
            targetName,
            Array.isArray(existing?.members) ? existing.members : []
          )

          if (renamed) {
            existingName = renamed
            localName = renamed
            existing = $groupChats.get()[renamed]
          }

          if (hostedRoomSyncDisposed) {
            return
          }

          if (!hostedRoomMutationIsCurrent(roomId, refreshGeneration)) {
            continue
          }
        }

        const replay = await replayHostedRoomPages({
          state: createHostedRoomReplayState({
            roomId,
            name: serverName,
            members: Array.isArray(serverRoom.members) ? (serverRoom.members as Array<Record<string, unknown>>) : [],
            authorityId: String(serverRoom.authority_gateway_id || capability.authorityId),
            authorityEpoch: Number(serverRoom.authority_epoch || 1),
            connectionId,
            cursor: Number(existing?.hostedSeq || 0)
          }),
          fetchPage: request =>
            requestHostedConnection(route, 'groups.log', {
              room_id: roomId,
              since_seq: request.sinceSeq,
              limit: request.limit,
              ...(includeDisbanded ? { include_disbanded: true } : {})
            }),
          pageSize: capability.maxLogLimit || 100
        })

        if (hostedRoomSyncDisposed) {
          return
        }

        if (!hostedRoomMutationIsCurrent(roomId, refreshGeneration)) {
          continue
        }

        const replayStatus = deriveFriendlyHostedRoomStatus(replay.state)
        const driver = record(stateResponse.driver_status)

        const stopping = $hostedRoomOutbox
          .get()
          .commands.some(
            command =>
              command.roomId === roomId && ['disband', 'stop'].includes(command.kind) && command.status !== 'failed'
          )

        const friendly = hostedRoomDriverDisplayStatus(replayStatus, driver, { stopping })
        const running = ['queued', 'stopping', 'working'].includes(friendly.kind)

        const retryAction = (Array.isArray(driver?.pending_actions) ? driver.pending_actions : [])
          .map(record)
          .find(action => action?.kind === 'retry' && String(action?.task_id || ''))

        updateGroupChat(
          localName,
          current => {
            const authoritative = applyHostedRoomAuthority(current, serverRoom as Record<string, unknown>)

            return {
              ...authoritative,
              roomId,
              members: hostedMemberDescriptors(serverRoom, connectionId, current.members || [], capabilities),
              log: mergeGroupChatSyncEntries(current.log || [], replayMessages(replay.state.messages)),
              hostedConnectionId: connectionId,
              hostedSeq: replay.state.cursor,
              hostedStatus: {
                ...hostedStatus(friendly, sourceLabel(connectionId)),
                ...(retryAction ? { taskId: String(retryAction.task_id) } : {}),
                ...(!replay.complete ? { canRetry: true } : {})
              },
              continuityMode: hostedRoomContinuityMode(serverRoom),
              continuityIssue: replay.complete ? null : botsText().group.hostedSyncing,
              running
            }
          },
          {
            sync: false
          }
        )

        if (replay.complete) {
          hostedRoomPollCache.set(roomId, hostedRoomPollFingerprint(listedRoom))

          if (includeDisbanded) {
            caughtUpDisbandedIds.add(roomId)
          }
        } else {
          hostedRoomPollCache.delete(roomId)
        }
      }

      // Keep the local shell long enough to explain a disband observed on
      // another client. Silently deleting only the room atom would strand an
      // open workspace and leave membership metadata half-cleaned. The normal
      // local disband action performs the complete cross-module cleanup.
      if (disbandedIds.size) {
        for (const [name, room] of Object.entries($groupChats.get())) {
          if (
            room.roomId &&
            disbandedIds.has(room.roomId) &&
            caughtUpDisbandedIds.has(room.roomId) &&
            room.hostedConnectionId === connectionId
          ) {
            updateGroupChat(
              name,
              current => ({
                ...current,
                running: false,
                hostedStatus: {
                  state: 'deleted',
                  label: botsText().group.hostedDeleted
                },
                continuityIssue: botsText().group.hostedDeleteLocally
              }),
              {
                sync: false
              }
            )
          }
        }
      }

      if (listComplete) {
        hostedRoomInventoriedConnections.add(connectionId)
        const listedIds = new Set(listedRooms.map(raw => String(record(raw)?.room_id || '')).filter(Boolean))

        for (const [name, room] of Object.entries($groupChats.get())) {
          const roomId = String(room?.roomId || '')

          if (!roomId || room.hostedConnectionId !== connectionId || listedIds.has(roomId)) {
            continue
          }

          try {
            await requestHostedConnection(route, 'groups.state', {
              room_id: roomId,
              include_disbanded: true
            })

            continue
          } catch (error) {
            const message = String(record(error)?.message || record(record(error)?.error)?.message || error || '')

            if (!/history expired|permanently retired|hosted room not found/i.test(message)) {
              continue
            }
          }

          hostedRoomPollCache.delete(roomId)
          updateGroupChat(
            name,
            current => ({
              ...current,
              running: false,
              hostedStatus: {
                state: 'deleted',
                label: botsText().group.hostedDeleted
              },
              continuityIssue: botsText().group.hostedDeleteLocally
            }),
            { sync: false }
          )
        }
      }
    }

    if (!hostedRoomSyncDisposed) {
      $hostedRoomCapabilities.set(capabilities)
    }
  } finally {
    hostedRoomSyncRunning = false
  }
}

function scheduleHostedRoomSync(delay = HOSTED_ROOM_SYNC_INTERVAL_MS) {
  if (hostedRoomSyncDisposed || typeof setTimeout !== 'function') {
    return
  }

  if (hostedRoomSyncTimer) {
    clearTimeout(hostedRoomSyncTimer)
  }

  hostedRoomSyncTimer = setTimeout(() => {
    hostedRoomSyncTimer = null
    void dispatchHostedRoomCleanup()
      .catch(() => undefined)
      .then(() => refreshHostedRooms())
      .catch(() => undefined)
      .then(() => dispatchHostedRoomOutbox())
      .catch(() => undefined)
      .then(() => scheduleHostedRoomSync())
  }, delay)

  const timer = hostedRoomSyncTimer as ReturnType<typeof setTimeout> & { unref?: () => void }
  timer?.unref?.()
}

async function transitionHostedRoomOutbox(action: Parameters<typeof reduceHostedRoomOutbox>[1]) {
  if (typeof hostedRoomStorage?.set !== 'function') {
    throw new Error(botsText().group.desktopStorageUnavailable)
  }

  const previous = $hostedRoomOutbox.get()
  const next = reduceHostedRoomOutbox(previous, action)

  $hostedRoomOutbox.set(next)

  try {
    await hostedRoomStorage.set(HOSTED_ROOM_OUTBOX_KEY, next)
  } catch (error) {
    $hostedRoomOutbox.set(previous)
    throw error
  }

  return next
}

const TERMINAL_HOSTED_ROOM_COMMAND_CODES = new Set([4110, 4111, 4113, 4117])

function terminalCommandFailure(error: unknown) {
  const candidate = record(error)
  const nested = record(candidate?.error)
  const code = Number(candidate?.code ?? nested?.code)

  return Number.isInteger(code) && TERMINAL_HOSTED_ROOM_COMMAND_CODES.has(code)
}

export async function dispatchHostedRoomOutbox() {
  if (hostedOutboxDispatching || hostedRoomSyncDisposed) {
    return
  }

  hostedOutboxDispatching = true

  try {
    let state = $hostedRoomOutbox.get()

    for (const command of state.commands.filter(entry => entry.status === 'pending')) {
      const route = (await hostedDefaultRoutes()).find(candidate => candidate.connectionId === command.connectionId)

      if (hostedRoomSyncDisposed) {
        return
      }

      if (!route) {
        continue
      }

      state = await transitionHostedRoomOutbox({
        type: 'dispatch',
        commandId: command.commandId
      })

      const method: Record<HostedRoomCommand['kind'], string> = {
        create: 'groups.create',
        retry: 'groups.retry',
        rename: 'groups.rename',
        send: 'groups.send',
        stop: 'groups.stop',
        disband: 'groups.disband'
      }

      const params =
        command.kind === 'send'
          ? {
              room_id: command.roomId,
              event_id: command.commandId,
              payload: command.payload
            }
          : command.kind === 'rename'
            ? {
                room_id: command.roomId,
                event_id: command.commandId,
                name: command.payload.name
              }
            : command.kind === 'retry'
              ? {
                  room_id: command.roomId,
                  task_id: command.payload.task_id
                }
              : command.kind === 'stop' || command.kind === 'disband'
                ? {
                    room_id: command.roomId,
                    cancel_id: command.commandId
                  }
                : command.payload

      try {
        await requestHostedConnection(route, method[command.kind], params)

        // Keep the persisted in-flight command untouched when the window is
        // disposed mid-request. Rehydration returns it to pending with the
        // same idempotency key, covering an unknown server outcome safely.
        if (hostedRoomSyncDisposed) {
          return
        }

        state = await transitionHostedRoomOutbox({
          type: 'acknowledge',
          commandId: command.commandId
        })
      } catch (error) {
        state = await transitionHostedRoomOutbox(
          terminalCommandFailure(error)
            ? {
                type: 'terminal-failure',
                commandId: command.commandId,
                failureCode: String(record(error)?.code || 'command-rejected')
              }
            : {
                type: 'transient-failure',
                commandId: command.commandId
              }
        )
      }
    }
  } finally {
    hostedOutboxDispatching = false
  }
}

async function enqueueHostedRoomCommand(command: Partial<HostedRoomCommand>) {
  await transitionHostedRoomOutbox({
    type: 'enqueue',
    command
  })
  await dispatchHostedRoomOutbox()

  const pending = $hostedRoomOutbox.get().commands.find(entry => entry.commandId === command.commandId)

  if (pending?.status === 'failed') {
    throw new Error(botsText().group.hostRejectedCommand)
  }

  scheduleHostedRoomSync(0)

  return !pending
}

async function hostedRouteForRoom(room: GroupChat) {
  const connectionId = String(room?.hostedConnectionId || '')
  const routes = await hostedDefaultRoutes()

  if (connectionId) {
    const exact = routes.find(candidate => candidate.connectionId === connectionId)

    if (exact) {
      return exact
    }
  }

  return hostedAuthorityRoutes.get(groupChatHostedGateway(room)) || null
}

export async function probeHostedRoomMembers(members: GroupMember[]): Promise<HostedRoomProbe> {
  const routes = Object.fromEntries(
    (await hostedDefaultRoutes()).map(route => [String(route.connectionId || ''), route])
  )

  const connectionIds = [
    ...new Set(
      (Array.isArray(members) ? members : [])
        .map(member => String(member?.route?.connectionId || member?.connectionId || activeConnectionId() || ''))
        .filter(Boolean)
    )
  ]

  const capabilities: Record<string, HostedRoomCapability> = {}
  const now = Date.now()

  for (const connectionId of connectionIds) {
    const cached = $hostedRoomCapabilities.get()[connectionId]

    if (cached?.kind === 'unsupported' && Number(hostedUnsupportedUntil.get(connectionId) || 0) > now) {
      capabilities[connectionId] = cached

      continue
    }

    const route = routes[connectionId]
    let capability: HostedRoomCapability

    try {
      capability = classifyHostedRoomCapability(
        route
          ? await withHostedRoomProbeTimeout(requestHostedConnection(route, 'groups.capabilities'))
          : { ok: false, error: new Error('Gateway route unavailable') },
        { connectionId }
      )
    } catch (error) {
      capability = classifyHostedRoomCapability({ ok: false, error }, { connectionId })
    }

    capabilities[connectionId] = capability

    if (capability.kind === 'unsupported') {
      hostedUnsupportedUntil.set(connectionId, now + HOSTED_ROOM_UNSUPPORTED_REPROBE_MS)
    } else {
      hostedUnsupportedUntil.delete(connectionId)
    }

    if (capability.authorityId && isHostedRoomContinuityEligible(capability) && route) {
      hostedAuthorityRoutes.set(capability.authorityId, route)
    }
  }

  $hostedRoomCapabilities.set({ ...$hostedRoomCapabilities.get(), ...capabilities })

  const route = resolveAutonomousRoomPlan(members, {
    activeConnectionId: activeConnectionId(),
    capabilities
  })

  const capability = route.connectionId ? capabilities[route.connectionId] || null : null

  return {
    route,
    routes,
    capabilities,
    capability,
    eligible: route.kind !== 'unsupported' && isHostedRoomContinuityEligible(capability)
  }
}

export async function createHostedGroupChat({ route, roomId, name, members }: HostedRoomCreateInput): Promise<{
  authorityEpoch: number
  authorityId: string
  connectionId: string
}> {
  if ((route.kind !== 'single-gateway' && route.kind !== 'multi-gateway') || !route.connectionId) {
    throw new Error(botsText().group.botsNeedOneHost)
  }

  const profileRoute = (await hostedDefaultRoutes()).find(candidate => candidate.connectionId === route.connectionId)

  if (!profileRoute) {
    throw new Error(botsText().group.hostRouteMissing)
  }

  let room: Record<string, unknown> | null = null

  try {
    const result = await requestHostedConnection<Record<string, unknown>>(profileRoute, 'groups.create', {
      room_id: roomId,
      name,
      members
    })

    room = record(result.room)
  } catch (createError) {
    // A dropped response has an unknown outcome. Verify the idempotent room id
    // before falling back to Desktop, or both drivers could start the first
    // user turn. A true create failure has no state and safely falls through.
    try {
      const state = await requestHostedConnection<Record<string, unknown>>(profileRoute, 'groups.state', {
        room_id: roomId
      })

      room = record(state.room)
    } catch {
      throw createError
    }
  }

  const authorityId = String(room?.authority_gateway_id || '')

  if (!authorityId) {
    throw new Error(botsText().group.hostRejectedCommand)
  }

  hostedAuthorityRoutes.set(authorityId, profileRoute)

  return {
    authorityId,
    authorityEpoch: Math.max(1, Number(room?.authority_epoch || 1)),
    connectionId: route.connectionId
  }
}

export async function createAutonomousHostedGroupChat({
  probe,
  roomId,
  name,
  members
}: AutonomousHostedRoomCreateInput) {
  const plan = probe.route
  const homeConnectionId = String(plan.homeConnectionId || '')
  const homeRoute = probe.routes[homeConnectionId]
  const homeCapability = probe.capabilities[homeConnectionId]

  if (!probe.eligible || !homeConnectionId || !homeRoute || !homeCapability?.authorityId) {
    throw new Error('This Group Chat cannot continue without Desktop yet.')
  }

  const hostedMembers: Array<Record<string, unknown>> = []
  const peerRegistrations: Array<Record<string, unknown>> = []

  try {
    await addHostedRoomCleanup({
      operationId: `${roomId}:home-disband`,
      setupId: roomId,
      kind: 'home-disband',
      connectionId: homeConnectionId,
      roomId,
      cancelId: `rollback-${roomId}`
    })

    for (const [index, item] of members.entries()) {
      const connectionId = String(item.member.route?.connectionId || item.member.connectionId || '')
      const profile = String(item.member.targetProfile || item.profile || item.member.name || 'default')
      const memberId = `member-${index + 1}-${profile}`.replace(/[^A-Za-z0-9._:-]/g, '-').slice(0, 128)

      const descriptor: Record<string, unknown> = {
        member_id: memberId,
        profile,
        handle: item.handle,
        ...(item.displayName
          ? {
              display_name: item.displayName
            }
          : {})
      }

      if (connectionId === homeConnectionId) {
        hostedMembers.push(descriptor)

        continue
      }

      const invitation = record(
        await requestForBot(item.member, 'groups.peer.invite', {
          room_id: roomId,
          home_install_id: homeCapability.authorityId,
          authority_gateway_id: homeCapability.authorityId,
          authority_epoch: 1,
          member_id: memberId,
          profile
        })
      )

      const catalog = record(invitation?.catalog)
      const invitedProfile = String(invitation?.target_profile || profile || '')

      const scopedTargetUrl = profileScopedRoomLinkEndpoint(
        probe.capabilities[connectionId]?.roomLink?.endpoint,
        invitation?.target_profile
      )

      if (invitation?.grant && invitedProfile) {
        await addHostedRoomCleanup({
          operationId: `${roomId}:peer-revoke:${memberId}`,
          setupId: roomId,
          kind: 'peer-revoke',
          connectionId,
          profile: invitedProfile,
          grant: String(invitation.grant)
        })
      }

      if (
        !scopedTargetUrl ||
        !invitation?.grant ||
        !catalog?.installation_id ||
        !catalog.catalog_digest ||
        !invitation.target_profile
      ) {
        throw new Error('One selected Bot could not prepare this Group Chat.')
      }

      hostedMembers.push({
        ...descriptor,
        profile: invitation.target_profile,
        target: {
          kind: 'peer',
          peer_id: catalog.installation_id,
          installation_id: catalog.installation_id,
          profile: invitation.target_profile,
          capability_digest: catalog.catalog_digest
        }
      })
      peerRegistrations.push({
        room_id: roomId,
        member_id: memberId,
        target_url: scopedTargetUrl,
        target_profile: invitation.target_profile,
        grant: invitation.grant,
        catalog
      })
    }

    const created = await createHostedGroupChat({
      route: plan,
      roomId,
      name,
      members: hostedMembers as HostedRoomCreateInput['members']
    })

    for (const registration of peerRegistrations) {
      await requestHostedConnection(homeRoute, 'groups.peer.register', registration)
    }

    await releaseHostedRoomCleanup(roomId)

    return {
      ...created,
      continuityMode: plan.kind === 'multi-gateway' ? ('distributed' as const) : ('gateway' as const)
    }
  } catch (error) {
    await armHostedRoomCleanup(roomId).catch(() => undefined)
    await dispatchHostedRoomCleanup().catch(() => undefined)

    if (
      normalizeHostedRoomCleanup($hostedRoomCleanup.get()).operations.some(operation => operation.setupId === roomId)
    ) {
      throw Object.assign(
        new Error('Some selected Bots could not finish cleanup. Reconnect them before trying again.', {
          cause: error
        }),
        {
          fallbackSafe: false
        }
      )
    }

    throw error
  }
}

export async function sendHostedGroupChat(group: string, message: GroupMessage, thread: string) {
  const room = $groupChats.get()[group]

  if (!room?.roomId || !groupChatHostedGateway(room)) {
    throw new Error(botsText().group.hostRouteMissing)
  }

  const route = await hostedRouteForRoom(room)
  const connectionId = String(route?.connectionId || room.hostedConnectionId || '')

  if (!connectionId) {
    throw new Error(botsText().group.hostRouteMissing)
  }

  return enqueueHostedRoomCommand({
    commandId: String(message.id || ''),
    kind: 'send',
    roomId: room.roomId,
    authorityId: groupChatHostedGateway(room),
    connectionId,
    payload: {
      text: message.text || '',
      thread_id: thread
    }
  })
}

export async function stopHostedGroupChat(group: string) {
  const room = $groupChats.get()[group]

  if (!room?.roomId || !groupChatHostedGateway(room)) {
    return false
  }

  const route = await hostedRouteForRoom(room)
  const connectionId = String(route?.connectionId || room.hostedConnectionId || '')

  if (!connectionId) {
    throw new Error(botsText().group.hostRouteMissing)
  }

  return enqueueHostedRoomCommand({
    commandId: crypto.randomUUID(),
    kind: 'stop',
    roomId: room.roomId,
    authorityId: groupChatHostedGateway(room),
    connectionId,
    payload: {}
  })
}

export async function retryHostedGroupChat(group: string, taskId: string) {
  const room = $groupChats.get()[group]

  if (!room?.roomId || !groupChatHostedGateway(room) || !String(taskId || '').trim()) {
    return false
  }

  const route = await hostedRouteForRoom(room)
  const connectionId = String(route?.connectionId || room.hostedConnectionId || '')

  if (!connectionId) {
    throw new Error(botsText().group.hostRouteMissing)
  }

  return enqueueHostedRoomCommand({
    commandId: crypto.randomUUID(),
    kind: 'retry',
    roomId: room.roomId,
    authorityId: groupChatHostedGateway(room),
    connectionId,
    payload: { task_id: String(taskId).trim() }
  })
}

/** Resume bounded history replay without retrying any Bot work. */
export async function retryHostedRoomReplay(group: string) {
  const room = $groupChats.get()[group]
  const roomId = String(room?.roomId || '')

  if (!roomId || !groupChatHostedGateway(room)) {
    return false
  }

  hostedRoomPollCache.delete(roomId)
  await refreshHostedRooms()
  scheduleHostedRoomSync(0)

  return true
}

export async function renameHostedGroupChat(group: string, name: string) {
  const room = $groupChats.get()[group]

  if (!room?.roomId || !groupChatHostedGateway(room)) {
    return true
  }

  // A refresh may already be replaying the pre-rename server snapshot. Advance
  // the room fence before the request so that stale replay cannot restore the
  // old map key after the local rename completes or is queued for retry.
  beginHostedRoomMutation(room.roomId)

  const route = await hostedRouteForRoom(room)
  const connectionId = String(route?.connectionId || room.hostedConnectionId || '')

  if (!connectionId) {
    throw new Error(botsText().group.hostRouteMissing)
  }

  return enqueueHostedRoomCommand({
    commandId: crypto.randomUUID(),
    kind: 'rename',
    roomId: room.roomId,
    authorityId: groupChatHostedGateway(room),
    connectionId,
    payload: {
      name
    }
  })
}

export async function disbandHostedGroupChat(group: string) {
  const room = $groupChats.get()[group]

  if (!room?.roomId || !groupChatHostedGateway(room)) {
    return false
  }

  const route = await hostedRouteForRoom(room)

  if (!route) {
    throw new Error(
      botsText().group.hostedReconnectToDelete(
        sourceLabel(String(room.hostedConnectionId || '')) || botsText().group.thisHost
      )
    )
  }

  return enqueueHostedRoomCommand({
    commandId: crypto.randomUUID(),
    kind: 'disband',
    roomId: room.roomId,
    authorityId: groupChatHostedGateway(room),
    connectionId: route.connectionId,
    payload: {}
  })
}

export async function startHostedRoomRuntime(storage: PluginContext['storage'], hooks: HostedRoomRuntimeHooks = {}) {
  hostedRoomStorage = storage
  hostedRoomHooks = hooks
  hostedRoomSyncDisposed = false
  hostedRoomMutationGenerations.clear()
  hostedRoomLocallyDeleted.clear()
  hostedRoomInventoriedConnections.clear()
  let persisted: unknown = null

  try {
    persisted = await storage?.get?.(HOSTED_ROOM_OUTBOX_KEY, null)
  } catch {
    /* an empty outbox is the safe fallback */
  }

  try {
    $hostedRoomOutbox.set(createHostedRoomOutbox(persisted))
  } catch {
    $hostedRoomOutbox.set(createHostedRoomOutbox())
  }

  try {
    $hostedRoomCleanup.set(normalizeHostedRoomCleanup(await storage?.get?.(HOSTED_ROOM_CLEANUP_KEY, null)))
  } catch {
    $hostedRoomCleanup.set({ version: 1, operations: [] })
  }

  await dispatchHostedRoomCleanup().catch(() => undefined)
  await refreshHostedRooms().catch(() => undefined)
  await dispatchHostedRoomOutbox().catch(() => undefined)
  scheduleHostedRoomSync()
}

export function stopHostedRoomRuntime() {
  hostedRoomSyncDisposed = true
  hostedRoomStorage = null
  hostedRoomHooks = {}
  hostedAuthorityRoutes.clear()
  hostedRoomPollCache.clear()
  hostedRoomMutationGenerations.clear()
  hostedRoomLocallyDeleted.clear()
  hostedRoomInventoriedConnections.clear()
  hostedUnsupportedUntil.clear()

  if (hostedRoomSyncTimer) {
    clearTimeout(hostedRoomSyncTimer)
  }

  hostedRoomSyncTimer = null
}

/** Test-only lifecycle reset through the same public stop door. */
export function resetHostedRoomRuntimeForTests() {
  stopHostedRoomRuntime()
  hostedRoomSyncRunning = false
  hostedOutboxDispatching = false
  hostedCleanupDispatching = false
  $hostedRoomCapabilities.set({})
  $hostedRoomOutbox.set(createHostedRoomOutbox())
  $hostedRoomCleanup.set({ version: 1, operations: [] })
}
