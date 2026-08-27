const MIN_ROOM_MEMBERS = 2
const MAX_ROOM_MEMBERS = 6
const MAX_REPLAY_PAGE_SIZE = 500
const MAX_REPLAY_PAGES = 100
const MAX_ATTACHMENT_COUNT = 8
const MAX_ATTACHMENT_NAME_CHARS = 255
const MAX_ATTACHMENT_NAME_BYTES = 512
const MAX_ATTACHMENT_FILE_BYTES = 15_000_000
const MAX_ATTACHMENT_MANIFEST_BYTES = 32 * 1024
const MAX_ATTACHMENT_MIME_CHARS = 127
const MAX_ATTACHMENT_REFS = 6
const MAX_MEMBER_ID_CHARS = 128
const MAX_STAGED_REF_CHARS = 256
const MAX_OUTBOX_COMMANDS = 256
export const ROOM_LINK_PROTOCOL_VERSION = 2

export const HOSTED_ROOM_CLIENT_LIMITATIONS = Object.freeze({
  attachments: false,
  automaticFailover: false,
  crossGatewayMembers: true,
  stagedAttachmentManifest: true
})

const ATTACHMENT_FIELDS = new Set(['kind', 'name', 'size', 'mime', 'refs'])
const ATTACHMENT_KINDS = new Set(['image', 'pdf', 'file'])
const ATTACHMENT_PAYLOAD_ALIASES = new Set(['attachment', 'attachment_manifest', 'files', 'images'])
const COMMAND_KINDS = new Set(['send', 'stop'])
const DISALLOWED_STAGED_REF_SCHEMES = new Set(['blob', 'data', 'file', 'http', 'https', 'path'])
const FORBIDDEN_TRANSPORT_FIELD_TOKENS = new Set(['base64', 'byte', 'bytes', 'data', 'path', 'paths'])
const MEMBER_ID_RE = /^[A-Za-z0-9][A-Za-z0-9._:-]*$/
const MIME_RE = /^[a-z0-9][a-z0-9!#$&^_.+-]*\/[a-z0-9][a-z0-9!#$&^_.+-]*$/i
const STAGED_REF_RE = /^[a-z][a-z0-9+.-]*:[A-Za-z0-9][A-Za-z0-9._:-]*$/i
const STATUS_EVENT_KINDS = new Set([
  'authority.lost',
  'member.unavailable',
  'room.activity',
  'turn.cancelled',
  'turn.deferred',
  'turn.failed',
  'turn.reassigned',
  'turn.settled',
  'turn.started'
])
const KNOWN_EVENT_KINDS = new Set([
  'authority.claimed',
  'authority.lost',
  'member.unavailable',
  'message.member',
  'message.user',
  'room.activity',
  'room.created',
  'room.disbanded',
  'room.members_changed',
  'room.renamed',
  'turn.cancelled',
  'turn.deferred',
  'turn.failed',
  'turn.reassigned',
  'turn.settled',
  'turn.started'
])

function text(value) {
  return typeof value === 'string' && value.trim() ? value.trim() : null
}

function nonNegativeInteger(value, fallback = 0) {
  const number = Number(value)

  return Number.isSafeInteger(number) && number >= 0 ? number : fallback
}

function positiveInteger(value, fallback = null) {
  const number = Number(value)

  return Number.isSafeInteger(number) && number > 0 ? number : fallback
}

function timestampMilliseconds(value) {
  const number = Number(value)

  if (!Number.isFinite(number) || number <= 0) return 0
  return number < 1_000_000_000_000 ? number * 1000 : number
}

function errorCode(error) {
  return error?.code ?? error?.error?.code ?? null
}

function errorMessage(error) {
  return String(error?.message || error?.error?.message || error || '')
}

function isMissingCapabilityMethod(error) {
  return (
    errorCode(error) === -32601 ||
    /method not found|-32601|unknown method|no such method|no handler for|unsupported rpc/i.test(errorMessage(error))
  )
}

function capabilityResult(probe) {
  if (!probe || typeof probe !== 'object') {
    return null
  }

  if (probe.ok === true && probe.result && typeof probe.result === 'object') {
    return probe.result
  }

  if (!Object.prototype.hasOwnProperty.call(probe, 'ok') && !Object.prototype.hasOwnProperty.call(probe, 'error')) {
    return probe
  }

  return null
}

function roomLinkCapability(value) {
  if (!value || typeof value !== 'object') return null
  const catalog = value.catalog
  const endpoint = value.endpoint && typeof value.endpoint === 'object' ? value.endpoint : null
  return {
    enabled: value.enabled === true,
    endpoint: endpoint?.available === true ? text(endpoint.url) : null,
    endpointReason: endpoint?.available === false ? text(endpoint.reason) : null,
    reason: text(value.reason),
    profile: text(value.profile),
    catalog:
      catalog && typeof catalog === 'object'
        ? {
            installationId: text(catalog.installation_id),
            digest: text(catalog.catalog_digest),
            persistentProcess: catalog.persistent_process === true,
            text: catalog.text === true,
            attachments: catalog.attachments === true,
            linkModes: Array.isArray(catalog.link_modes) ? catalog.link_modes.filter(Boolean) : [],
            protocolVersions: Array.isArray(catalog.protocol_versions)
              ? catalog.protocol_versions.map(value => Number(value)).filter(Number.isSafeInteger)
              : []
          }
        : null
  }
}

/** Classify a groups.capabilities probe without turning connectivity failures
 * into a compatibility verdict. */
export function classifyHostedRoomCapability(probe, { connectionId = null } = {}) {
  const localConnectionId = text(connectionId)
  const error = probe instanceof Error ? probe : probe?.ok === false ? probe.error || probe : probe?.error

  if (error) {
    return {
      kind: isMissingCapabilityMethod(error) ? 'unsupported' : 'transient-failure',
      reason: isMissingCapabilityMethod(error) ? 'old-gateway' : 'probe-failed',
      connectionId: localConnectionId,
      authorityId: null,
      persistentProcess: null,
      roomLink: null,
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  const capabilities = capabilityResult(probe)

  if (!capabilities) {
    return {
      kind: 'transient-failure',
      reason: 'invalid-response',
      connectionId: localConnectionId,
      authorityId: null,
      persistentProcess: null,
      roomLink: null,
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  if (capabilities.driver !== true) {
    return {
      kind: 'unsupported',
      reason: capabilities.driver === false ? 'driver-disabled' : 'incomplete-contract',
      connectionId: localConnectionId,
      authorityId: null,
      persistentProcess: capabilities.persistent_process === true,
      roomLink: roomLinkCapability(capabilities.room_link),
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  const authorityId = text(capabilities.authority_gateway_id)

  if (!authorityId) {
    return {
      kind: 'unsupported',
      reason: 'incomplete-contract',
      connectionId: localConnectionId,
      authorityId: null,
      persistentProcess: capabilities.persistent_process === true,
      roomLink: roomLinkCapability(capabilities.room_link),
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  return {
    kind: 'driver-capable',
    reason: null,
    connectionId: localConnectionId,
    authorityId,
    persistentProcess: capabilities.persistent_process === true,
    roomLink: roomLinkCapability(capabilities.room_link),
    maxLogLimit: positiveInteger(capabilities.max_log_limit, 100),
    limits: HOSTED_ROOM_CLIENT_LIMITATIONS
  }
}

/** A running driver hosted inside an app-managed child process cannot outlive
 * Desktop. Accept either a raw capability response or its classified form. */
export function isHostedRoomContinuityEligible(capability) {
  if (!capability || typeof capability !== 'object') {
    return false
  }

  if (Object.prototype.hasOwnProperty.call(capability, 'kind')) {
    return capability.kind === 'driver-capable' && capability.persistentProcess === true
  }

  return capability.driver === true && capability.persistent_process === true
}

function memberConnectionId(member, activeConnectionId) {
  if (!member || member.sourceMissing) {
    return null
  }

  const explicit = text(member.route?.connectionId) || text(member.connectionId)

  if (explicit) {
    return explicit
  }

  if (member.sourceScoped || member.remoteSource) {
    return null
  }

  return text(activeConnectionId)
}

/** Resolve a same-gateway home candidate. This deliberately does not select a
 * different gateway or attach a stable authority identity. */
export function resolveSingleGatewayRoute(members, { activeConnectionId = null } = {}) {
  const roster = Array.isArray(members) ? members : []

  if (roster.length < MIN_ROOM_MEMBERS || roster.length > MAX_ROOM_MEMBERS) {
    return {
      kind: 'unsupported',
      reason: 'member-count',
      connectionId: null,
      memberConnectionIds: [],
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  const memberConnectionIds = roster.map(member => memberConnectionId(member, activeConnectionId))

  if (memberConnectionIds.some(connectionId => !connectionId)) {
    return {
      kind: 'unsupported',
      reason: 'unresolved-member-route',
      connectionId: null,
      memberConnectionIds,
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  const connectionIds = [...new Set(memberConnectionIds)]

  if (connectionIds.length !== 1) {
    return {
      kind: 'unsupported',
      reason: 'cross-gateway',
      connectionId: null,
      memberConnectionIds,
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  return {
    kind: 'single-gateway',
    reason: null,
    connectionId: connectionIds[0],
    memberConnectionIds,
    limits: HOSTED_ROOM_CLIENT_LIMITATIONS
  }
}

/** Plan the simplest autonomous room across one or several gateways. The
 * gateway catalogs are authoritative; Desktop only chooses among verified
 * capabilities and never upgrades them. */
export function resolveAutonomousRoomPlan(
  members,
  { activeConnectionId = null, capabilities = {} } = {}
) {
  const roster = Array.isArray(members) ? members : []
  const route = resolveSingleGatewayRoute(roster, { activeConnectionId })

  if (route.reason && route.reason !== 'cross-gateway') {
    return { ...route, homeConnectionId: null, remoteConnectionIds: [] }
  }

  const memberConnectionIds = roster.map(member => memberConnectionId(member, activeConnectionId))
  const connectionIds = [...new Set(memberConnectionIds.filter(Boolean))]
  const classified = Object.fromEntries(
    connectionIds.map(connectionId => [connectionId, capabilities[connectionId] || null])
  )
  const homeCandidates = connectionIds.filter(connectionId => {
    const capability = classified[connectionId]
    if (capability?.kind !== 'driver-capable' || capability.persistentProcess !== true) {
      return false
    }
    if (connectionIds.length === 1) {
      return true
    }
    const roomLink = capability.roomLink
    return (
      roomLink?.enabled === true &&
      roomLink?.catalog?.persistentProcess === true &&
      roomLink?.catalog?.protocolVersions?.includes(ROOM_LINK_PROTOCOL_VERSION) &&
      roomLink?.catalog?.linkModes?.includes('direct')
    )
  })
  const preferredHome = text(activeConnectionId)
  const homeConnectionId = homeCandidates.includes(preferredHome)
    ? preferredHome
    : homeCandidates[0] || null

  if (!homeConnectionId) {
    return {
      kind: 'unsupported',
      reason: 'no-persistent-home',
      connectionId: null,
      homeConnectionId: null,
      memberConnectionIds,
      remoteConnectionIds: connectionIds,
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  const remoteConnectionIds = connectionIds.filter(connectionId => connectionId !== homeConnectionId)
  const unsupportedRemote = remoteConnectionIds.find(connectionId => {
    const roomLink = classified[connectionId]?.roomLink
    return (
      roomLink?.enabled !== true ||
      roomLink?.catalog?.persistentProcess !== true ||
      roomLink?.catalog?.text !== true ||
      !roomLink?.catalog?.installationId ||
      !roomLink?.catalog?.digest ||
      !roomLink?.catalog?.protocolVersions?.includes(ROOM_LINK_PROTOCOL_VERSION) ||
      !roomLink?.catalog?.linkModes?.includes('direct')
    )
  })
  if (unsupportedRemote) {
    return {
      kind: 'unsupported',
      reason: 'remote-needs-setup',
      connectionId: null,
      homeConnectionId,
      memberConnectionIds,
      remoteConnectionIds,
      unavailableConnectionId: unsupportedRemote,
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  const unreachableRemote = remoteConnectionIds.find(
    connectionId => !classified[connectionId]?.roomLink?.endpoint
  )
  if (unreachableRemote) {
    return {
      kind: 'unsupported',
      reason: 'remote-needs-address',
      connectionId: null,
      homeConnectionId,
      memberConnectionIds,
      remoteConnectionIds,
      unavailableConnectionId: unreachableRemote,
      limits: HOSTED_ROOM_CLIENT_LIMITATIONS
    }
  }

  return {
    kind: remoteConnectionIds.length ? 'multi-gateway' : 'single-gateway',
    reason: null,
    connectionId: homeConnectionId,
    homeConnectionId,
    memberConnectionIds,
    remoteConnectionIds,
    limits: HOSTED_ROOM_CLIENT_LIMITATIONS
  }
}

/** User-facing continuity summary. Architecture names stay out of the green
 * path; settings can reveal the selected coordinator without exposing URLs,
 * grants, or transport details. */
export function describeAutonomousRoomPlan(
  plan,
  { homeLabel = 'a Hermes gateway', unavailableLabel = 'One gateway' } = {}
) {
  if (plan?.kind === 'multi-gateway') {
    return {
      defaultEnabled: true,
      level: 'distributed',
      title: 'Continues when Desktop is closed',
      description: `Bots keep working together across gateways. ${homeLabel} coordinates this room.`
    }
  }

  if (plan?.kind === 'single-gateway') {
    return {
      defaultEnabled: true,
      level: 'gateway',
      title: 'Continues when Desktop is closed',
      description: `Bots keep working on ${homeLabel}.`
    }
  }

  const needsSetup = ['remote-needs-address', 'remote-needs-setup'].includes(plan?.reason)
  return {
    defaultEnabled: false,
    level: 'desktop',
    title: 'Keep Desktop open for this room',
    description: needsSetup
      ? `On ${unavailableLabel}, set gateway.room_link_url to its reachable HTTPS address, then reopen this screen.`
      : 'The selected gateways cannot continue this room on their own yet.'
  }
}

function cloneList(value) {
  return Array.isArray(value) ? value.map(item => (item && typeof item === 'object' ? { ...item } : item)) : []
}

/** Serializable replay state. Stable gateway authority and local routing are
 * intentionally separate fields. */
export function createHostedRoomReplayState({
  roomId,
  name = '',
  members = [],
  authorityId = null,
  authorityEpoch = null,
  connectionId = null,
  cursor = 0,
  latestSeq = null,
  messages = [],
  activity = [],
  timeline = [],
  pendingEvents = [],
  lastStatusEvent = null,
  deleted = false,
  conflicts = []
} = {}) {
  const normalizedCursor = nonNegativeInteger(cursor)

  return {
    roomId: text(roomId),
    name: typeof name === 'string' ? name : '',
    members: cloneList(members),
    authorityId: text(authorityId),
    authorityEpoch: positiveInteger(authorityEpoch),
    connectionId: text(connectionId),
    cursor: normalizedCursor,
    latestSeq: Math.max(normalizedCursor, nonNegativeInteger(latestSeq, normalizedCursor)),
    messages: cloneList(messages),
    activity: cloneList(activity),
    timeline: cloneList(timeline),
    pendingEvents: cloneList(pendingEvents),
    lastStatusEvent: lastStatusEvent && typeof lastStatusEvent === 'object' ? { ...lastStatusEvent } : null,
    deleted: Boolean(deleted),
    conflicts: cloneList(conflicts)
  }
}

function normalizeEvent(raw) {
  if (!raw || typeof raw !== 'object') {
    return null
  }

  const seq = positiveInteger(raw.seq)
  const eventId = text(raw.event_id) || text(raw.eventId)
  const kind = text(raw.kind)

  if (!seq || !eventId || !kind) {
    return null
  }

  return {
    roomId: text(raw.room_id) || text(raw.roomId),
    seq,
    eventId,
    kind,
    actor: raw.actor && typeof raw.actor === 'object' ? { ...raw.actor } : {},
    payload: raw.payload && typeof raw.payload === 'object' && !Array.isArray(raw.payload) ? { ...raw.payload } : {},
    createdAt: Number.isFinite(Number(raw.created_at ?? raw.createdAt)) ? Number(raw.created_at ?? raw.createdAt) : 0
  }
}

function memberLabel(roomEvent) {
  const payload = roomEvent?.payload || {}
  const actor = roomEvent?.actor || {}

  return (
    text(payload.member_display_name) ||
    text(payload.member_name) ||
    text(payload.display_name) ||
    text(actor.display_name) ||
    text(actor.profile) ||
    'A bot'
  )
}

function messageFromEvent(roomEvent) {
  const isUser = roomEvent.kind === 'message.user'
  const payload = roomEvent.payload || {}
  const actor = roomEvent.actor || {}
  const attachments = safeAttachmentMetadata(payload.attachments)

  return {
    seq: roomEvent.seq,
    eventId: roomEvent.eventId,
    from: {
      kind: isUser ? 'user' : 'member',
      name: isUser ? 'You' : memberLabel(roomEvent),
      ...(text(actor.connection_id) ? { source: text(actor.connection_id) } : {})
    },
    text: typeof payload.text === 'string' ? payload.text : '',
    thread: text(payload.thread_id) || text(payload.thread) || 'legacy',
    at: timestampMilliseconds(roomEvent.createdAt),
    ...(attachments.length ? { attachments } : {})
  }
}

function activityFromEvent(roomEvent) {
  return {
    seq: roomEvent.seq,
    eventId: roomEvent.eventId,
    kind: roomEvent.kind,
    member: memberLabel(roomEvent),
    reasonCode: text(roomEvent.payload?.reason_code),
    at: timestampMilliseconds(roomEvent.createdAt)
  }
}

function applyReplayEvent(state, roomEvent) {
  const next = state

  if (KNOWN_EVENT_KINDS.has(roomEvent.kind)) {
    next.timeline.push({ seq: roomEvent.seq, eventId: roomEvent.eventId, kind: roomEvent.kind })
  }

  if (roomEvent.kind === 'message.user' || roomEvent.kind === 'message.member') {
    next.messages.push(messageFromEvent(roomEvent))
  } else if (roomEvent.kind === 'room.created') {
    next.name = text(roomEvent.payload.name) || next.name
    if (Array.isArray(roomEvent.payload.members)) {
      next.members = cloneList(roomEvent.payload.members)
    }
  } else if (roomEvent.kind === 'room.renamed') {
    next.name = text(roomEvent.payload.name) || next.name
  } else if (roomEvent.kind === 'room.members_changed' && Array.isArray(roomEvent.payload.members)) {
    next.members = cloneList(roomEvent.payload.members)
  } else if (roomEvent.kind === 'room.disbanded') {
    next.deleted = true
  } else if (roomEvent.kind === 'authority.claimed') {
    const claimedAuthorityId = text(roomEvent.payload.authority_gateway_id)

    if (claimedAuthorityId) {
      if (next.authorityId && next.authorityId !== claimedAuthorityId) {
        next.connectionId = null
      }
      next.authorityId = claimedAuthorityId
    }
    next.authorityEpoch = positiveInteger(roomEvent.payload.authority_epoch, next.authorityEpoch)
  } else if (roomEvent.kind === 'authority.lost') {
    next.connectionId = null
  }

  if (STATUS_EVENT_KINDS.has(roomEvent.kind)) {
    next.activity.push(activityFromEvent(roomEvent))
    next.lastStatusEvent = roomEvent
  }

  return next
}

function mergePendingEvents(state, incomingEvents) {
  const bySeq = new Map()
  const byId = new Map()
  const conflicts = [...state.conflicts]

  for (const candidate of [...state.pendingEvents, ...(Array.isArray(incomingEvents) ? incomingEvents : [])]) {
    const roomEvent = normalizeEvent(candidate)

    if (!roomEvent || roomEvent.seq <= state.cursor) {
      continue
    }
    if (state.roomId && roomEvent.roomId && state.roomId !== roomEvent.roomId) {
      continue
    }

    const sequenceTwin = bySeq.get(roomEvent.seq)
    const idTwin = byId.get(roomEvent.eventId)

    if (sequenceTwin || idTwin) {
      const prior = sequenceTwin || idTwin

      if (prior.seq !== roomEvent.seq || prior.eventId !== roomEvent.eventId) {
        conflicts.push({ seq: roomEvent.seq, eventId: roomEvent.eventId })
      }
      continue
    }

    bySeq.set(roomEvent.seq, roomEvent)
    byId.set(roomEvent.eventId, roomEvent)
  }

  return {
    conflicts,
    events: [...bySeq.values()].sort((left, right) => left.seq - right.seq || left.eventId.localeCompare(right.eventId))
  }
}

/** Apply only contiguous gateway events. Reordered pages are buffered; unknown
 * kinds advance the cursor but cannot erase known room state. */
export function reduceHostedRoomEvents(state, incomingEvents) {
  const next = createHostedRoomReplayState(state)
  const merged = mergePendingEvents(next, incomingEvents)
  const pending = [...merged.events]

  next.conflicts = merged.conflicts
  next.latestSeq = Math.max(next.latestSeq, ...pending.map(roomEvent => roomEvent.seq), next.cursor)

  while (pending.length && pending[0].seq === next.cursor + 1) {
    const roomEvent = pending.shift()

    applyReplayEvent(next, roomEvent)
    next.cursor = roomEvent.seq
  }

  next.pendingEvents = pending

  return next
}

function friendlyStatus(kind, textValue, { member = null, canRetry = false, canStop = false } = {}) {
  return { kind, text: textValue, member, canRetry, canStop }
}

function failedTurnStatus(roomEvent) {
  const member = memberLabel(roomEvent)
  const reasonCode = text(roomEvent.payload?.reason_code)

  if (reasonCode === 'provider_auth_or_access') {
    return friendlyStatus('needs-attention', `${member} needs you to sign in again.`, { member })
  }
  if (reasonCode === 'provider_quota_limit') {
    return friendlyStatus('needs-attention', `${member} is out of quota or balance.`, { member })
  }
  if (reasonCode === 'missing_config') {
    return friendlyStatus('needs-attention', `${member} needs a model provider.`, { member })
  }
  if (reasonCode === 'agent_blocked') {
    return friendlyStatus('needs-attention', `${member} needs your help.`, { member })
  }

  return friendlyStatus('failed', `${member} couldn't respond.`, { member, canRetry: true })
}

function roomActivityStatus(roomEvent) {
  const activity = text(roomEvent.payload?.status)?.toLowerCase()
  const member = memberLabel(roomEvent)

  if (activity === 'working') {
    return friendlyStatus('working', `${member} is working.`, { member, canStop: true })
  }
  if (activity === 'needs_user' || activity === 'waiting_for_user') {
    return friendlyStatus('needs-you', 'Waiting for your answer.')
  }
  if (activity === 'waiting') {
    return friendlyStatus('waiting', 'The room is waiting.')
  }
  if (activity === 'paused') {
    return friendlyStatus('paused', 'Paused.')
  }
  if (activity === 'offline') {
    return friendlyStatus('offline', 'This room is offline.', { canRetry: true })
  }

  return friendlyStatus('ready', 'Ready.')
}

/** Convert durable typed events into short, user-facing status copy. Raw
 * coordinator and provider errors are never reflected. */
export function deriveFriendlyHostedRoomStatus(state) {
  if (state?.deleted) {
    return friendlyStatus('deleted', 'This group was deleted.')
  }
  if (state?.authorityId && !state?.connectionId) {
    return friendlyStatus('offline', 'This room is offline.', { canRetry: true })
  }

  const roomEvent = normalizeEvent(state?.lastStatusEvent)

  if (!roomEvent) {
    return friendlyStatus('ready', 'Ready.')
  }

  const member = memberLabel(roomEvent)

  if (roomEvent.kind === 'turn.started' || roomEvent.kind === 'turn.reassigned') {
    return friendlyStatus('working', `${member} is working.`, { member, canStop: true })
  }
  if (roomEvent.kind === 'member.unavailable') {
    return friendlyStatus('member-unavailable', `${member} is unavailable.`, { member, canRetry: true })
  }
  if (roomEvent.kind === 'turn.failed') {
    return failedTurnStatus(roomEvent)
  }
  if (roomEvent.kind === 'turn.cancelled') {
    return friendlyStatus('stopped', 'Stopped.')
  }
  if (roomEvent.kind === 'turn.settled') {
    return friendlyStatus('ready', 'Ready.')
  }
  if (roomEvent.kind === 'turn.deferred') {
    return friendlyStatus('waiting', `${member} is waiting to continue.`, { member, canRetry: true })
  }
  if (roomEvent.kind === 'authority.lost') {
    return friendlyStatus('offline', 'This room is offline.', { canRetry: true })
  }
  if (roomEvent.kind === 'room.activity') {
    return roomActivityStatus(roomEvent)
  }

  return friendlyStatus('ready', 'Ready.')
}

/** Fetch monotonic groups.log pages from the persisted replay cursor. The
 * helper stops on gaps, no-progress responses, transient errors, or a hard
 * page bound instead of spinning. */
export async function replayHostedRoomPages({ state, fetchPage, pageSize = 100, maxPages = 20 } = {}) {
  if (typeof fetchPage !== 'function') {
    throw new TypeError('fetchPage must be a function')
  }

  const limit = Math.min(MAX_REPLAY_PAGE_SIZE, Math.max(1, positiveInteger(pageSize, 100)))
  const pageBound = Math.min(MAX_REPLAY_PAGES, Math.max(1, positiveInteger(maxPages, 20)))
  let next = createHostedRoomReplayState(state)
  let pages = 0
  let fetchedEvents = 0

  while (pages < pageBound) {
    const beforeCursor = next.cursor
    let page

    try {
      page = await fetchPage({ sinceSeq: beforeCursor, limit })
    } catch (error) {
      return { state: next, complete: false, reason: 'transient-failure', pages, fetchedEvents, error }
    }

    if (!page || typeof page !== 'object') {
      return { state: next, complete: false, reason: 'invalid-response', pages, fetchedEvents }
    }

    const events = Array.isArray(page.events) ? page.events : []
    const latestSeq = nonNegativeInteger(page.latest_seq ?? page.latestSeq, next.latestSeq)

    if (events.length > limit) {
      return { state: next, complete: false, reason: 'oversized-page', pages, fetchedEvents }
    }

    pages += 1
    fetchedEvents += events.length
    next = reduceHostedRoomEvents(next, events)
    next.latestSeq = Math.max(next.latestSeq, latestSeq)

    const hasMore = page.has_more === true || next.cursor < latestSeq

    if (!hasMore) {
      if (next.pendingEvents.length) {
        return { state: next, complete: false, reason: 'gap', pages, fetchedEvents }
      }
      return { state: next, complete: true, reason: null, pages, fetchedEvents }
    }

    if (next.cursor <= beforeCursor) {
      return { state: next, complete: false, reason: 'stalled', pages, fetchedEvents }
    }
  }

  return { state: next, complete: false, reason: 'limit', pages, fetchedEvents }
}

function cloneJson(value, label) {
  try {
    return JSON.parse(JSON.stringify(value))
  } catch (error) {
    throw new TypeError(`${label} must be JSON-serializable`, { cause: error })
  }
}

function utf8Size(value) {
  return new TextEncoder().encode(value).length
}

function fieldTokens(field) {
  return String(field)
    .replace(/([a-z0-9])([A-Z])/g, '$1_$2')
    .toLowerCase()
    .split(/[^a-z0-9]+/)
    .filter(Boolean)
}

function forbiddenTransportField(field) {
  return fieldTokens(field).find(token => FORBIDDEN_TRANSPORT_FIELD_TOKENS.has(token)) || null
}

function assertNoRawTransportFields(value, location = 'payload') {
  if (!value || typeof value !== 'object') {
    return
  }
  if (Array.isArray(value)) {
    value.forEach((entry, index) => assertNoRawTransportFields(entry, `${location}[${index}]`))
    return
  }

  for (const [key, nested] of Object.entries(value)) {
    const forbidden = forbiddenTransportField(key)

    if (forbidden) {
      throw new TypeError(`${location}.${key} contains a forbidden ${forbidden} field`)
    }
    if (key !== 'refs') {
      assertNoRawTransportFields(nested, `${location}.${key}`)
    }
  }
}

function normalizeAttachmentName(value) {
  const name = text(value)

  if (
    !name ||
    name.length > MAX_ATTACHMENT_NAME_CHARS ||
    utf8Size(name) > MAX_ATTACHMENT_NAME_BYTES ||
    name.includes('\0') ||
    name.includes('/') ||
    name.includes('\\')
  ) {
    throw new TypeError('attachment name must be a bounded base filename')
  }

  return name
}

function normalizeAttachmentMime(value, kind) {
  const mime = text(value)?.toLowerCase()

  if (!mime || mime.length > MAX_ATTACHMENT_MIME_CHARS || !MIME_RE.test(mime)) {
    throw new TypeError('attachment mime must be a bounded MIME type')
  }
  if (kind === 'image' && !mime.startsWith('image/')) {
    throw new TypeError('image attachments require an image MIME type')
  }
  if (kind === 'pdf' && mime !== 'application/pdf') {
    throw new TypeError('pdf attachments require application/pdf')
  }

  return mime
}

function normalizeAttachmentRefs(value, { required }) {
  if (value === undefined && !required) {
    return null
  }
  if (!value || typeof value !== 'object' || Array.isArray(value)) {
    throw new TypeError('attachment member refs must be an object')
  }

  const entries = Object.entries(value)

  if (!entries.length || entries.length > MAX_ATTACHMENT_REFS) {
    throw new TypeError(`attachment member refs must contain 1-${MAX_ATTACHMENT_REFS} entries`)
  }

  const refs = {}

  for (const [rawMemberId, rawRef] of entries) {
    const memberId = text(rawMemberId)
    const ref = text(rawRef)
    const refScheme = ref?.split(':', 1)[0]?.toLowerCase()

    if (!memberId || memberId.length > MAX_MEMBER_ID_CHARS || !MEMBER_ID_RE.test(memberId)) {
      throw new TypeError('attachment member refs require valid member_id keys')
    }
    if (
      !ref ||
      ref.length > MAX_STAGED_REF_CHARS ||
      !STAGED_REF_RE.test(ref) ||
      DISALLOWED_STAGED_REF_SCHEMES.has(refScheme)
    ) {
      throw new TypeError('attachment member refs require opaque staged reference ids')
    }

    refs[memberId] = ref
  }

  return refs
}

function normalizeAttachmentEntry(raw, { requireRefs }) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new TypeError('each attachment must be an object')
  }

  for (const key of Object.keys(raw)) {
    if (!ATTACHMENT_FIELDS.has(key)) {
      const forbidden = forbiddenTransportField(key)

      throw new TypeError(
        forbidden
          ? `attachment ${key} contains a forbidden ${forbidden} field`
          : `attachment contains unsupported field: ${key}`
      )
    }
  }

  const kind = text(raw.kind)?.toLowerCase()

  if (!kind || !ATTACHMENT_KINDS.has(kind)) {
    throw new TypeError('attachment kind must be image, pdf, or file')
  }
  if (
    typeof raw.size !== 'number' ||
    !Number.isSafeInteger(raw.size) ||
    raw.size < 0 ||
    raw.size > MAX_ATTACHMENT_FILE_BYTES
  ) {
    throw new TypeError(`attachment size must be between 0 and ${MAX_ATTACHMENT_FILE_BYTES} bytes`)
  }

  const normalized = {
    kind,
    name: normalizeAttachmentName(raw.name),
    size: raw.size,
    mime: normalizeAttachmentMime(raw.mime, kind)
  }
  const refs = normalizeAttachmentRefs(raw.refs, { required: requireRefs })

  return refs ? { ...normalized, refs } : normalized
}

function normalizeAttachmentManifest(value, { requireRefs }) {
  if (!Array.isArray(value)) {
    throw new TypeError('attachments must be a manifest array')
  }
  if (value.length > MAX_ATTACHMENT_COUNT) {
    throw new TypeError(`attachment manifest supports at most ${MAX_ATTACHMENT_COUNT} entries`)
  }

  const manifest = value.map(entry => normalizeAttachmentEntry(entry, { requireRefs }))

  if (utf8Size(JSON.stringify(manifest)) > MAX_ATTACHMENT_MANIFEST_BYTES) {
    throw new TypeError('attachment manifest metadata is too large')
  }

  return manifest
}

function safeAttachmentMetadata(value) {
  if (!Array.isArray(value) || value.length > MAX_ATTACHMENT_COUNT) {
    return []
  }

  const metadata = []

  for (const entry of value) {
    try {
      const normalized = normalizeAttachmentEntry(entry, { requireRefs: false })

      metadata.push({ kind: normalized.kind, name: normalized.name, size: normalized.size, mime: normalized.mime })
    } catch {
      // A malformed internal manifest must not expose refs or block replay.
    }
  }

  return metadata
}

function normalizeCommand(raw) {
  if (!raw || typeof raw !== 'object') {
    throw new TypeError('command must be an object')
  }

  const commandId = text(raw.commandId) || text(raw.id)
  const kind = text(raw.kind)
  const roomId = text(raw.roomId) || text(raw.room_id)
  const authorityId = text(raw.authorityId) || text(raw.authority_gateway_id)
  const connectionId = text(raw.connectionId) || text(raw.connection_id)

  if (!commandId || !kind || !roomId || !authorityId || !connectionId) {
    throw new TypeError('command requires commandId, kind, roomId, authorityId, and connectionId')
  }
  if (!COMMAND_KINDS.has(kind)) {
    throw new TypeError(`unsupported hosted room command: ${kind}`)
  }

  const rawPayload = raw.payload && typeof raw.payload === 'object' ? raw.payload : {}

  if (kind === 'send') {
    assertNoRawTransportFields(rawPayload)
    for (const alias of ATTACHMENT_PAYLOAD_ALIASES) {
      if (Object.prototype.hasOwnProperty.call(rawPayload, alias)) {
        throw new TypeError(`send attachments must use the attachments manifest, not ${alias}`)
      }
    }
  }

  const payload = cloneJson(rawPayload, 'command payload')

  if (kind === 'send' && Object.prototype.hasOwnProperty.call(payload, 'attachments')) {
    payload.attachments = normalizeAttachmentManifest(payload.attachments, { requireRefs: true })
  }

  return {
    commandId,
    kind,
    roomId,
    authorityId,
    connectionId,
    payload,
    status: raw.status === 'failed' ? 'failed' : raw.status === 'in-flight' ? 'in-flight' : 'pending',
    attempts: nonNegativeInteger(raw.attempts),
    failureCode: text(raw.failureCode)
  }
}

function stableJson(value) {
  if (Array.isArray(value)) {
    return `[${value.map(stableJson).join(',')}]`
  }
  if (value && typeof value === 'object') {
    return `{${Object.keys(value)
      .sort()
      .map(key => `${JSON.stringify(key)}:${stableJson(value[key])}`)
      .join(',')}}`
  }

  return JSON.stringify(value)
}

function commandSignature(command) {
  return stableJson({
    commandId: command.commandId,
    kind: command.kind,
    roomId: command.roomId,
    authorityId: command.authorityId,
    connectionId: command.connectionId,
    payload: command.payload
  })
}

/** Rehydrate a persisted outbox. An in-flight command has an unknown outcome
 * after relaunch, so it returns to pending with the same idempotency key. */
export function createHostedRoomOutbox(persisted = null) {
  const commands = []

  for (const raw of Array.isArray(persisted?.commands) ? persisted.commands : []) {
    const command = normalizeCommand(raw)
    const existing = commands.find(candidate => candidate.commandId === command.commandId)

    command.status = command.status === 'in-flight' ? 'pending' : command.status

    if (!existing) {
      commands.push(command)
    } else if (commandSignature(existing) !== commandSignature(command)) {
      throw new TypeError(`commandId ${command.commandId} has conflicting persisted content`)
    }
  }

  return { version: 1, commands }
}

function updateCommand(state, commandId, mutate) {
  const index = state.commands.findIndex(command => command.commandId === commandId)

  if (index < 0) {
    return state
  }

  const commands = [...state.commands]
  commands[index] = mutate({ ...commands[index] })

  return { ...state, commands }
}

/** Pure state transitions for persisted send/stop commands. */
export function reduceHostedRoomOutbox(state, action) {
  const current = state && Array.isArray(state.commands) ? state : createHostedRoomOutbox()

  if (!action || typeof action !== 'object') {
    throw new TypeError('outbox action must be an object')
  }

  if (action.type === 'enqueue') {
    const command = normalizeCommand(action.command)
    const existing = current.commands.find(candidate => candidate.commandId === command.commandId)

    command.status = 'pending'

    if (existing) {
      if (commandSignature(existing) !== commandSignature(command)) {
        throw new TypeError(`commandId ${command.commandId} is already bound to different content`)
      }
      return current
    }
    if (current.commands.length >= MAX_OUTBOX_COMMANDS) {
      throw new TypeError('too many room actions are waiting for their gateway')
    }

    return { ...current, commands: [...current.commands, command] }
  }

  const commandId = text(action.commandId)

  if (!commandId) {
    throw new TypeError('outbox action requires commandId')
  }

  if (action.type === 'dispatch') {
    return updateCommand(current, commandId, command => ({
      ...command,
      status: 'in-flight',
      attempts: command.attempts + 1,
      failureCode: null
    }))
  }
  if (action.type === 'retry' || action.type === 'transient-failure') {
    return updateCommand(current, commandId, command => ({ ...command, status: 'pending' }))
  }
  if (action.type === 'terminal-failure') {
    return updateCommand(current, commandId, command => ({
      ...command,
      status: 'failed',
      failureCode: text(action.failureCode) || 'command-failed'
    }))
  }
  if (action.type === 'acknowledge') {
    if (!current.commands.some(command => command.commandId === commandId)) {
      return current
    }
    return { ...current, commands: current.commands.filter(command => command.commandId !== commandId) }
  }

  throw new TypeError(`unsupported outbox action: ${action.type}`)
}
