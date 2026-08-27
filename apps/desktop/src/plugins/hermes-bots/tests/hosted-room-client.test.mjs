import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

import {
  HOSTED_ROOM_CLIENT_LIMITATIONS,
  ROOM_LINK_PROTOCOL_VERSION,
  classifyHostedRoomCapability,
  createHostedRoomOutbox,
  createHostedRoomReplayState,
  deriveFriendlyHostedRoomStatus,
  isHostedRoomContinuityEligible,
  reduceHostedRoomEvents,
  reduceHostedRoomOutbox,
  replayHostedRoomPages,
  resolveAutonomousRoomPlan,
  describeAutonomousRoomPlan,
  resolveSingleGatewayRoute
} from '../hosted-room-client.js'

const protocolFixture = JSON.parse(
  readFileSync(new URL('../../../../../../tests/fixtures/room_link_protocol_v2.json', import.meta.url), 'utf8')
)

test('RoomLink client accepts the backend v2 catalog and rejects unpublished v1', () => {
  assert.equal(ROOM_LINK_PROTOCOL_VERSION, protocolFixture.protocol_version)

  const capability = classifyHostedRoomCapability({
    driver: true,
    persistent_process: true,
    authority_gateway_id: 'install:peer',
    room_link: {
      enabled: true,
      endpoint: { available: true, url: 'https://peer.example.test' },
      catalog: protocolFixture.catalog
    }
  })
  const members = [
    { name: 'home', connectionId: 'home', sourceScoped: true },
    { name: 'peer', connectionId: 'peer', sourceScoped: true }
  ]
  const plan = resolveAutonomousRoomPlan(members, {
    activeConnectionId: 'home',
    capabilities: { home: capability, peer: capability }
  })

  assert.equal(plan.kind, 'multi-gateway')
  const v1 = {
    ...capability,
    roomLink: {
      ...capability.roomLink,
      catalog: { ...capability.roomLink.catalog, protocolVersions: [1] }
    }
  }
  assert.equal(
    resolveAutonomousRoomPlan(members, {
      activeConnectionId: 'home',
      capabilities: { home: capability, peer: v1 }
    }).reason,
    'remote-needs-setup'
  )
})

function attachmentManifest() {
  return [
    {
      kind: 'image',
      name: 'diagram.png',
      size: 2048,
      mime: 'image/png',
      refs: {
        research: 'stage:research:image-1',
        builder: 'stage:builder:image-1'
      }
    },
    {
      kind: 'pdf',
      name: 'brief.pdf',
      size: 4096,
      mime: 'application/pdf',
      refs: {
        research: 'stage:research:pdf-1',
        builder: 'stage:builder:pdf-1'
      }
    },
    {
      kind: 'file',
      name: 'notes.txt',
      size: 128,
      mime: 'text/plain',
      refs: {
        research: 'stage:research:file-1',
        builder: 'stage:builder:file-1'
      }
    }
  ]
}

function event(seq, eventId, kind, payload = {}, actor = { kind: 'gateway', id: 'install:home' }) {
  return {
    room_id: 'room-1',
    seq,
    event_id: eventId,
    kind,
    actor,
    payload,
    created_at: seq
  }
}

test('capability classification distinguishes old, disabled, transient, and driver-capable gateways', () => {
  const missing = Object.assign(new Error('JSON-RPC -32601: method not found'), { code: -32601 })

  assert.deepEqual(classifyHostedRoomCapability({ ok: false, error: missing }, { connectionId: 'local-a' }), {
    kind: 'unsupported',
    reason: 'old-gateway',
    connectionId: 'local-a',
    authorityId: null,
    persistentProcess: null,
    roomLink: null,
    limits: HOSTED_ROOM_CLIENT_LIMITATIONS
  })

  assert.equal(
    classifyHostedRoomCapability({ ok: true, result: { driver: false } }, { connectionId: 'local-a' }).reason,
    'driver-disabled'
  )
  assert.equal(
    classifyHostedRoomCapability({ ok: false, error: new Error('socket closed during reconnect') }).kind,
    'transient-failure'
  )
  assert.equal(classifyHostedRoomCapability(new Error('socket closed during reconnect')).kind, 'transient-failure')

  const capable = classifyHostedRoomCapability(
    {
      ok: true,
      result: {
        driver: true,
        persistent_process: true,
        authority_gateway_id: 'install:stable-home',
        max_log_limit: 250,
        room_link: {
          enabled: true,
          profile: 'default',
          catalog: {
            installation_id: 'install:stable-home',
            protocol_versions: [1],
            link_modes: ['direct', 'pull'],
            persistent_process: true,
            text: true,
            attachments: false,
            catalog_digest: 'a'.repeat(64)
          }
        },
        features: ['typed_events', 'attachments', 'automatic_failover']
      }
    },
    { connectionId: 'machine-local-7' }
  )

  assert.equal(capable.kind, 'driver-capable')
  assert.equal(capable.authorityId, 'install:stable-home')
  assert.equal(capable.connectionId, 'machine-local-7')
  assert.equal(capable.persistentProcess, true)
  assert.equal(capable.maxLogLimit, 250)
  assert.equal(capable.roomLink.enabled, true)
  assert.equal(capable.roomLink.catalog.installationId, 'install:stable-home')
  assert.deepEqual(capable.limits, {
    attachments: false,
    automaticFailover: false,
    crossGatewayMembers: true,
    stagedAttachmentManifest: true
  })

  const appManaged = classifyHostedRoomCapability({
    driver: true,
    persistent_process: false,
    authority_gateway_id: 'install:desktop-child'
  })
  assert.equal(appManaged.kind, 'driver-capable')
  assert.equal(appManaged.persistentProcess, false)
  assert.equal(isHostedRoomContinuityEligible(capable), true)
  assert.equal(isHostedRoomContinuityEligible(appManaged), false)
  assert.equal(isHostedRoomContinuityEligible({ driver: true, persistent_process: true }), true)
  assert.equal(isHostedRoomContinuityEligible({ driver: false, persistent_process: true }), false)
})

test('single-gateway route resolution accepts 2-6 co-located bots and rejects mixed or incomplete routes', () => {
  const same = resolveSingleGatewayRoute(
    [
      { name: 'research', connectionId: 'connection-a', sourceScoped: true },
      { name: 'builder', route: { connectionId: 'connection-a' }, remoteSource: true }
    ],
    { activeConnectionId: 'active-local' }
  )

  assert.deepEqual(same, {
    kind: 'single-gateway',
    reason: null,
    connectionId: 'connection-a',
    memberConnectionIds: ['connection-a', 'connection-a'],
    limits: HOSTED_ROOM_CLIENT_LIMITATIONS
  })

  const local = resolveSingleGatewayRoute([{ name: 'one' }, { name: 'two' }], {
    activeConnectionId: 'active-local'
  })
  assert.equal(local.connectionId, 'active-local')

  const mixed = resolveSingleGatewayRoute([
    { name: 'one', connectionId: 'connection-a', sourceScoped: true },
    { name: 'two', connectionId: 'connection-b', sourceScoped: true }
  ])
  assert.equal(mixed.kind, 'unsupported')
  assert.equal(mixed.reason, 'cross-gateway')

  assert.equal(
    resolveSingleGatewayRoute([{ name: 'alone' }], { activeConnectionId: 'active-local' }).reason,
    'member-count'
  )
  assert.equal(
    resolveSingleGatewayRoute(
      [
        { name: 'one', connectionId: 'connection-a', sourceScoped: true },
        { name: 'lost', sourceScoped: true }
      ],
      { activeConnectionId: 'active-local' }
    ).reason,
    'unresolved-member-route'
  )
})

test('autonomous room planning chooses a persistent home and verified remote catalogs', () => {
  const members = [
    { name: 'local', connectionId: 'home', sourceScoped: true },
    { name: 'reviewer', connectionId: 'peer', sourceScoped: true }
  ]
  const home = classifyHostedRoomCapability({
    driver: true,
    persistent_process: true,
    authority_gateway_id: 'install:home',
    room_link: {
      enabled: true,
      profile: 'default',
      catalog: {
        installation_id: 'install:home',
        protocol_versions: [1],
        link_modes: ['direct'],
        persistent_process: true,
        text: true,
        attachments: false,
        catalog_digest: 'a'.repeat(64)
      }
    }
  })
  const peer = classifyHostedRoomCapability({
    driver: true,
    persistent_process: true,
    authority_gateway_id: 'install:peer',
    room_link: {
      enabled: true,
      profile: 'default',
      catalog: {
        installation_id: 'install:peer',
        protocol_versions: [1],
        link_modes: ['direct', 'pull'],
        persistent_process: true,
        text: true,
        attachments: false,
        catalog_digest: 'b'.repeat(64)
      },
      endpoint: {
        available: true,
        url: 'https://peer.example.test',
        transport_security: 'tls'
      }
    }
  })
  const plan = resolveAutonomousRoomPlan(members, {
    activeConnectionId: 'home',
    capabilities: { home, peer }
  })
  assert.equal(plan.kind, 'multi-gateway')
  assert.equal(plan.homeConnectionId, 'home')
  assert.deepEqual(plan.remoteConnectionIds, ['peer'])

  const unavailable = resolveAutonomousRoomPlan(members, {
    activeConnectionId: 'home',
    capabilities: { home, peer: { ...peer, roomLink: { enabled: false } } }
  })
  assert.equal(unavailable.kind, 'unsupported')
  assert.equal(unavailable.reason, 'remote-needs-setup')

  const noAddress = resolveAutonomousRoomPlan(members, {
    activeConnectionId: 'home',
    capabilities: {
      home,
      peer: { ...peer, roomLink: { ...peer.roomLink, endpoint: null } }
    }
  })
  assert.equal(noAddress.reason, 'remote-needs-address')

  const oldHome = resolveAutonomousRoomPlan(members, {
    activeConnectionId: 'home',
    capabilities: { home: { ...home, roomLink: null }, peer }
  })
  assert.equal(oldHome.homeConnectionId, 'peer')
  assert.equal(oldHome.reason, 'remote-needs-setup')

  const incompatible = resolveAutonomousRoomPlan(members, {
    activeConnectionId: 'home',
    capabilities: {
      home,
      peer: {
        ...peer,
        roomLink: {
          ...peer.roomLink,
          catalog: {
            ...peer.roomLink.catalog,
            protocolVersions: [99],
            linkModes: ['desktop']
          }
        }
      }
    }
  })
  assert.equal(incompatible.reason, 'remote-needs-setup')

  assert.deepEqual(describeAutonomousRoomPlan(plan, { homeLabel: 'Home lab' }), {
    defaultEnabled: true,
    level: 'distributed',
    title: 'Continues when Desktop is closed',
    description: 'Bots keep working together across gateways. Home lab coordinates this room.'
  })
  assert.equal(describeAutonomousRoomPlan(noAddress).level, 'desktop')
  assert.equal(
    describeAutonomousRoomPlan(noAddress, { unavailableLabel: 'Workshop' }).description,
    'Workshop needs setup before this room can continue on its own.'
  )
})

test('authoritative replay orders and deduplicates by sequence and event id while ignoring unknown events safely', () => {
  const initial = createHostedRoomReplayState({
    roomId: 'room-1',
    name: 'Release room',
    members: [{ profile: 'research' }],
    authorityId: 'install:stable-home',
    connectionId: 'machine-local-7'
  })
  const user = event(
    1,
    'event-user-1',
    'message.user',
    { text: 'Start the review', thread_id: 'thread-1' },
    { kind: 'user', id: 'desktop' }
  )
  const unknown = event(2, 'event-future-1', 'future.room.signal', { destructive: true })
  const member = event(
    3,
    'event-member-1',
    'message.member',
    { text: 'Review complete', thread_id: 'thread-1' },
    { kind: 'member', id: 'research', display_name: 'Research', profile: 'research' }
  )

  const replayed = reduceHostedRoomEvents(initial, [member, unknown, user, { ...user }])

  assert.equal(replayed.cursor, 3)
  assert.equal(replayed.name, 'Release room')
  assert.deepEqual(replayed.members, [{ profile: 'research' }])
  assert.deepEqual(
    replayed.messages.map(message => [message.seq, message.eventId, message.text]),
    [
      [1, 'event-user-1', 'Start the review'],
      [3, 'event-member-1', 'Review complete']
    ]
  )
  assert.deepEqual(replayed.pendingEvents, [])

  const repeated = reduceHostedRoomEvents(replayed, [member, unknown, user])
  assert.equal(repeated.cursor, 3)
  assert.equal(repeated.messages.length, 2)
})

test('replay buffers sequence gaps and a tombstone preserves history while closing the room', () => {
  const initial = createHostedRoomReplayState({ roomId: 'room-1', name: 'Core' })
  const later = event(
    2,
    'event-member-1',
    'message.member',
    { text: 'Done' },
    { kind: 'member', id: 'builder', display_name: 'Builder' }
  )

  const buffered = reduceHostedRoomEvents(initial, [later])
  assert.equal(buffered.cursor, 0)
  assert.equal(buffered.pendingEvents.length, 1)

  const caughtUp = reduceHostedRoomEvents(buffered, [
    event(1, 'event-user-1', 'message.user', { text: 'Build it' }, { kind: 'user', id: 'desktop' })
  ])
  const deleted = reduceHostedRoomEvents(caughtUp, [event(3, 'system:room-disbanded', 'room.disbanded')])

  assert.equal(deleted.cursor, 3)
  assert.equal(deleted.deleted, true)
  assert.deepEqual(
    deleted.messages.map(message => message.text),
    ['Build it', 'Done']
  )
  assert.deepEqual(deriveFriendlyHostedRoomStatus(deleted), {
    kind: 'deleted',
    text: 'This group was deleted.',
    member: null,
    canRetry: false,
    canStop: false
  })
})

test('replay preserves safe attachment metadata without exposing staged member refs', () => {
  const replayed = reduceHostedRoomEvents(createHostedRoomReplayState({ roomId: 'room-1' }), [
    event(
      1,
      'event-user-with-attachments',
      'message.user',
      { text: 'Review these', thread_id: 'thread-1', attachments: attachmentManifest() },
      { kind: 'user', id: 'desktop' }
    )
  ])

  assert.deepEqual(replayed.messages[0].attachments, [
    { kind: 'image', name: 'diagram.png', size: 2048, mime: 'image/png' },
    { kind: 'pdf', name: 'brief.pdf', size: 4096, mime: 'application/pdf' },
    { kind: 'file', name: 'notes.txt', size: 128, mime: 'text/plain' }
  ])
  assert.doesNotMatch(JSON.stringify(replayed.messages[0]), /stage:|refs/)
})

test('friendly status output uses typed reason codes without exposing coordination jargon', () => {
  const cases = [
    [
      event(1, 'turn-started-1', 'turn.started', { member_display_name: 'Research', task_id: 'task-secret' }),
      'Research is working.',
      'working'
    ],
    [
      event(1, 'member-unavailable-1', 'member.unavailable', { member_display_name: 'Builder', lease: 'lease-secret' }),
      'Builder is unavailable.',
      'member-unavailable'
    ],
    [
      event(1, 'turn-failed-1', 'turn.failed', {
        member_display_name: 'Research',
        reason_code: 'provider_auth_or_access',
        error: 'stale lease at epoch 9 for task 12'
      }),
      'Research needs you to sign in again.',
      'needs-attention'
    ],
    [event(1, 'turn-cancelled-1', 'turn.cancelled', { task_id: 'task-secret' }), 'Stopped.', 'stopped'],
    [event(1, 'authority-lost-1', 'authority.lost', { authority_epoch: 9 }), 'This room is offline.', 'offline']
  ]

  for (const [roomEvent, text, kind] of cases) {
    const state = reduceHostedRoomEvents(createHostedRoomReplayState({ roomId: 'room-1' }), [roomEvent])
    const status = deriveFriendlyHostedRoomStatus(state)

    assert.equal(status.text, text)
    assert.equal(status.kind, kind)
    assert.doesNotMatch(JSON.stringify(status), /lease|epoch|task/i)
  }
})

test('bounded paged replay resumes from the persisted cursor and catches up through reordered pages', async () => {
  const calls = []
  const pages = [
    {
      events: [
        event(4, 'event-member-2', 'message.member', { text: 'Four' }, { kind: 'member', id: 'builder' }),
        event(3, 'event-user-2', 'message.user', { text: 'Three' }, { kind: 'user', id: 'desktop' })
      ],
      cursor: 4,
      latest_seq: 5,
      has_more: true
    },
    {
      events: [event(5, 'turn-settled-1', 'turn.settled')],
      cursor: 5,
      latest_seq: 5,
      has_more: false
    }
  ]

  const result = await replayHostedRoomPages({
    state: createHostedRoomReplayState({ roomId: 'room-1', cursor: 2 }),
    pageSize: 2,
    maxPages: 4,
    fetchPage: async params => {
      calls.push(params)
      return pages.shift()
    }
  })

  assert.equal(result.complete, true)
  assert.equal(result.reason, null)
  assert.equal(result.pages, 2)
  assert.equal(result.state.cursor, 5)
  assert.deepEqual(calls, [
    { sinceSeq: 2, limit: 2 },
    { sinceSeq: 4, limit: 2 }
  ])
  assert.deepEqual(
    result.state.messages.map(message => message.text),
    ['Three', 'Four']
  )
})

test('paged replay stops at its configured bound instead of spinning', async () => {
  const result = await replayHostedRoomPages({
    state: createHostedRoomReplayState({ roomId: 'room-1' }),
    pageSize: 1,
    maxPages: 1,
    fetchPage: async () => ({
      events: [event(1, 'event-user-1', 'message.user', { text: 'One' }, { kind: 'user', id: 'desktop' })],
      cursor: 1,
      latest_seq: 2,
      has_more: true
    })
  })

  assert.equal(result.complete, false)
  assert.equal(result.reason, 'limit')
  assert.equal(result.state.cursor, 1)
})

test('paged replay refuses a gateway response larger than the requested bound', async () => {
  const result = await replayHostedRoomPages({
    state: createHostedRoomReplayState({ roomId: 'room-1' }),
    pageSize: 1,
    fetchPage: async () => ({
      events: [
        event(1, 'event-user-1', 'message.user', { text: 'One' }, { kind: 'user', id: 'desktop' }),
        event(2, 'event-user-2', 'message.user', { text: 'Two' }, { kind: 'user', id: 'desktop' })
      ],
      cursor: 2,
      latest_seq: 2,
      has_more: false
    })
  })

  assert.equal(result.complete, false)
  assert.equal(result.reason, 'oversized-page')
  assert.equal(result.state.cursor, 0)
})

test('persisted command outbox accepts each supported command and enqueue is idempotent', () => {
  let outbox = createHostedRoomOutbox()

  for (const kind of ['create', 'send', 'stop', 'disband']) {
    outbox = reduceHostedRoomOutbox(outbox, {
      type: 'enqueue',
      command: {
        commandId: `${kind}-1`,
        kind,
        roomId: 'room-1',
        authorityId: 'install:stable-home',
        connectionId: 'machine-local-7',
        payload: kind === 'send' ? { text: 'Hello', thread_id: 'thread-1', attachments: attachmentManifest() } : {}
      }
    })
  }

  const unchanged = reduceHostedRoomOutbox(outbox, { type: 'enqueue', command: outbox.commands[0] })
  assert.equal(unchanged, outbox)
  assert.deepEqual(
    outbox.commands.map(command => command.kind),
    ['create', 'send', 'stop', 'disband']
  )
  assert.equal(outbox.commands[0].authorityId, 'install:stable-home')
  assert.equal(outbox.commands[0].connectionId, 'machine-local-7')
  assert.deepEqual(outbox.commands[1].payload.attachments, attachmentManifest())
})

test('send attachment manifests reject raw transport data, local paths, malformed refs, and oversized metadata', () => {
  const enqueue = attachments =>
    reduceHostedRoomOutbox(createHostedRoomOutbox(), {
      type: 'enqueue',
      command: {
        commandId: 'send-with-files',
        kind: 'send',
        roomId: 'room-1',
        authorityId: 'install:stable-home',
        connectionId: 'machine-local-7',
        payload: { text: 'See files', attachments }
      }
    })

  assert.throws(() => enqueue(Array.from({ length: 9 }, () => attachmentManifest()[0])), /at most 8/i)

  const oversizedName = attachmentManifest()
  oversizedName[0].name = `${'x'.repeat(256)}.png`
  assert.throws(() => enqueue(oversizedName), /attachment name/i)

  const rawData = attachmentManifest()
  rawData[0].data = 'data:image/png;base64,AAAA'
  assert.throws(() => enqueue(rawData), /data/i)

  const rawBase64 = attachmentManifest()
  rawBase64[0].content_base64 = 'AAAA'
  assert.throws(() => enqueue(rawBase64), /base64/i)

  const localPath = attachmentManifest()
  localPath[0].path = '/Users/david/Desktop/diagram.png'
  assert.throws(() => enqueue(localPath), /path/i)

  const pathRef = attachmentManifest()
  pathRef[0].refs.research = '/tmp/diagram.png'
  assert.throws(() => enqueue(pathRef), /staged reference/i)

  const fileRef = attachmentManifest()
  fileRef[0].refs.research = 'file:diagram.png'
  assert.throws(() => enqueue(fileRef), /staged reference/i)

  assert.throws(
    () =>
      reduceHostedRoomOutbox(createHostedRoomOutbox(), {
        type: 'enqueue',
        command: {
          commandId: 'send-with-image-alias',
          kind: 'send',
          roomId: 'room-1',
          authorityId: 'install:stable-home',
          connectionId: 'machine-local-7',
          payload: { text: 'See image', images: attachmentManifest() }
        }
      }),
    /attachments manifest/i
  )

  assert.throws(
    () =>
      enqueue([
        {
          kind: 'pdf',
          name: 'report.pdf',
          size: 10,
          mime: 'application/pdf',
          refs: {}
        }
      ]),
    /member refs/i
  )
})

test('response-lost commands retry with the same idempotency key after persisted rehydration', () => {
  const command = {
    commandId: 'send-stable-1',
    kind: 'send',
    roomId: 'room-1',
    authorityId: 'install:stable-home',
    connectionId: 'machine-local-7',
    payload: { text: 'Ship it', thread_id: 'thread-1', attachments: attachmentManifest() }
  }
  let outbox = reduceHostedRoomOutbox(createHostedRoomOutbox(), { type: 'enqueue', command })
  outbox = reduceHostedRoomOutbox(outbox, { type: 'dispatch', commandId: command.commandId })

  const rehydrated = createHostedRoomOutbox(JSON.parse(JSON.stringify(outbox)))
  assert.equal(rehydrated.commands[0].status, 'pending')
  assert.equal(rehydrated.commands[0].attempts, 1)
  assert.equal(rehydrated.commands[0].commandId, command.commandId)
  assert.deepEqual(rehydrated.commands[0].payload.attachments, attachmentManifest())

  const deduped = reduceHostedRoomOutbox(rehydrated, { type: 'enqueue', command })
  assert.equal(deduped, rehydrated)

  const retried = reduceHostedRoomOutbox(rehydrated, { type: 'dispatch', commandId: command.commandId })
  assert.equal(retried.commands[0].attempts, 2)
  assert.equal(retried.commands[0].commandId, command.commandId)

  const acknowledged = reduceHostedRoomOutbox(retried, { type: 'acknowledge', commandId: command.commandId })
  assert.deepEqual(acknowledged.commands, [])
})
