import assert from 'node:assert/strict'

import { test } from 'vitest'

import { deferred, loadBotSessions as load, plain } from './runtime-harness'

interface BotMetaPayload {
  chat?: string | null
  pinned?: boolean
  hidden?: boolean
  [key: string]: unknown
}

interface SessionLocale {
  sessions: {
    default: string
    makeDefault: string
    makeDefaultLabel: (title: string) => string
    defaultLabel: (title: string) => string
    changeFailed: string
    persistenceFailed: string
  }
}

test('sessions workspace: selecting the secondary workspace does not navigate', async () => {
  const runtime = await load()
  runtime.__sessions.openBotSessionsWorkspace({ name: 'ops' })
  assert.equal(runtime.__sessions.$botSessionsWorkspace.get(), 'ops')
  assert.equal(runtime.calls.some(([method]) => method === 'openSession'), false)
})

test('sessions workspace: invalid profile names are ignored', async () => {
  const runtime = await load()
  runtime.__sessions.openBotSessionsWorkspace({ name: '../ops' })
  assert.equal(runtime.__sessions.$botSessionsWorkspace.get(), null)
})

test('sessions workspace: the default marker follows a canonical session through compression', async () => {
  const { defaultProfileSessionId } = (await load()).__sessions

  assert.equal(
    defaultProfileSessionId(
      { preferred_session: { id: 'root-123', resolved_id: 'tip-456' } },
      { chat: 'root-123' }
    ),
    'tip-456'
  )
  assert.equal(
    defaultProfileSessionId(
      { preferred_session: { id: 'old-root', resolved_id: 'old-tip' } },
      { chat: 'new-default' }
    ),
    'new-default',
    'a stale roster refresh must not override the newly selected default'
  )
})

test('sessions workspace: filtering searches title, preview, and source without privileged rows', async () => {
  const { filterProfileSessions } = (await load()).__sessions

  const rows = [
    { id: 'named', title: 'Oversight', preview: 'ordinary user session', source: 'user' },
    { id: 'deploy', title: 'Deploy API', preview: 'shipping', source: 'cli' },
    { id: 'docs', title: 'Write docs', preview: 'guide', source: 'desktop' }
  ]

  assert.deepEqual(plain(filterProfileSessions(rows, '').map((row: { id: string }) => row.id)), ['named', 'deploy', 'docs'])
  assert.deepEqual(plain(filterProfileSessions(rows, 'ship').map((row: { id: string }) => row.id)), ['deploy'])
  assert.deepEqual(plain(filterProfileSessions(rows, 'DESKTOP').map((row: { id: string }) => row.id)), ['docs'])
})

test('sessions workspace: opening a stored row uses profile-aware navigation and records selection', async () => {
  const runtime = await load({ profile: 'default' })
  await runtime.__sessions.openProfileSession('ops', { id: 'stored-123', message_count: 4 }, 0)
  assert.deepEqual(plain(runtime.calls), [
    ['openSession', 'stored-123', { profile: 'ops', awaitHydration: true, expectHistory: true, keepAllProfilesScope: false }]
  ])
  assert.equal(runtime.__sessions.$botSelectedSessions.get().ops, 'stored-123')
})

test('sessions workspace: Make default repoints the canonical Bot chat', async () => {
  const runtime = await load({
    request: method => method === 'profiles.configure' ? { applied: { ui_meta: true } } : undefined
  })

  const session = { id: 'stored-123', title: 'Critical context', message_count: 9 }

  const row = runtime.__sessions.ProfileSessionRow({
    session,
    botName: 'ops',
    active: false,
    isDefault: false,
    gatewayGeneration: 0
  })

  const action = row.props.children[1]

  assert.equal(action.type, 'Button')
  assert.equal(action.props.children, 'Make default')
  assert.equal(action.props['aria-label'], 'Make default: Critical context')

  await action.props.onClick()

  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'stored-123')
  assert.deepEqual(plain(runtime.calls), [
    ['profiles.configure', { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'stored-123' } } }]
  ])
  assert.deepEqual(plain(runtime.invalidations), [{ queryKey: ['hermes-bots', 'roster'] }])

  const updated = runtime.__sessions.ProfileSessionRow({
    session,
    botName: 'ops',
    active: false,
    isDefault: true,
    gatewayGeneration: 0
  })

  assert.equal(updated.props.children[1].props.children, 'Default')
})

test('sessions workspace: rapid default choices persist in click order with the latest winning', async () => {
  const firstWrite = deferred()
  const secondWrite = deferred()
  let write = 0

  const runtime = await load({
    request: method => method === 'profiles.configure' ? [firstWrite.promise, secondWrite.promise][write++] : undefined
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  const first = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'first-choice' }, 0)
  await Promise.resolve()
  const second = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'second-choice' }, 0)
  await Promise.resolve()
  await Promise.resolve()

  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'second-choice')
  assert.equal(runtime.calls.filter(([method]) => method === 'profiles.configure').length, 1)

  firstWrite.resolve({ applied: { ui_meta: true } })
  await first
  await Promise.resolve()
  await Promise.resolve()
  assert.equal(runtime.calls.filter(([method]) => method === 'profiles.configure').length, 2)

  secondWrite.resolve({ applied: { ui_meta: true } })
  await second
  assert.deepEqual(
    plain(runtime.calls.filter(([method]) => method === 'profiles.configure').map(([, params]) => params.ui_meta['hermes-bots'].chat)),
    ['first-choice', 'second-choice']
  )
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'second-choice')
})

test('sessions workspace: a failed latest choice keeps the last confirmed default through stale reconciliation', async () => {
  const firstWrite = deferred()
  const secondWrite = deferred()
  let write = 0

  const runtime = await load({
    request: method => method === 'profiles.configure' ? [firstWrite.promise, secondWrite.promise][write++] : undefined
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  const first = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'first-choice' }, 0)
  await Promise.resolve()
  const second = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'second-choice' }, 0)
  await Promise.resolve()
  await Promise.resolve()

  firstWrite.resolve({ applied: { ui_meta: true } })
  await first
  await Promise.resolve()
  await Promise.resolve()
  secondWrite.resolve({ applied: { ui_meta: false } })
  await second

  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'first-choice')
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'old-default' } } }
  ])
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'first-choice')
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'first-choice' } } }
  ])
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'first-choice')
})

test('sessions workspace: other bot-meta writes cannot restore an older default', async () => {
  const pinWrite = deferred()
  const defaultWrite = deferred()
  let serverChat = null

  const runtime = await load({
    request: (method, params) => {
      if (method !== 'profiles.configure') {return undefined}
      const chat = params.ui_meta['hermes-bots'].chat
      const result = chat === 'old-default' ? pinWrite.promise : defaultWrite.promise

      return result.then(response => {
        serverChat = chat

        return response
      })
    }
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default', pinned: false } })

  const pinning = runtime.__sessions.saveBotMeta('ops', { pinned: true })
  const choosing = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  await Promise.resolve()
  assert.equal(runtime.calls.filter(([method]) => method === 'profiles.configure').length, 1)

  pinWrite.resolve({ applied: { ui_meta: true } })
  await pinning
  await Promise.resolve()
  assert.equal(runtime.calls.filter(([method]) => method === 'profiles.configure').length, 2)

  defaultWrite.resolve({ applied: { ui_meta: true } })
  await choosing

  assert.equal(serverChat, 'new-default')
  assert.deepEqual(plain(runtime.__sessions.$botMeta.get().ops), { chat: 'new-default', pinned: true })
})

test('sessions workspace: an older ordinary chat write cannot overwrite a newer default choice', async () => {
  const ordinaryWrite = deferred()
  const defaultWrite = deferred()
  let write = 0
  const payloads: BotMetaPayload[] = []

  const runtime = await load({
    request: (method, params) => {
      if (method !== 'profiles.configure') {return undefined}
      payloads.push(plain(params.ui_meta['hermes-bots']))

      return [ordinaryWrite.promise, defaultWrite.promise][write++]
    }
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  const ordinary = runtime.__sessions.saveBotMeta('ops', { chat: 'legacy-navigation' })
  await Promise.resolve()
  await Promise.resolve()
  const choosing = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)

  ordinaryWrite.resolve({ applied: { ui_meta: true } })
  await ordinary
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'new-default')
  await Promise.resolve()
  await Promise.resolve()
  defaultWrite.resolve({ applied: { ui_meta: true } })
  await choosing

  assert.deepEqual(payloads.map(meta => meta.chat), ['legacy-navigation', 'new-default'])
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'new-default')
})

test('sessions workspace: a later ordinary chat write cannot replace an in-flight default choice', async () => {
  const defaultWrite = deferred()
  const ordinaryWrite = deferred()
  let write = 0
  const payloads: BotMetaPayload[] = []

  const runtime = await load({
    request: (method, params) => {
      if (method !== 'profiles.configure') {return undefined}
      payloads.push(plain(params.ui_meta['hermes-bots']))

      return [defaultWrite.promise, ordinaryWrite.promise][write++]
    }
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  const choosing = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  await Promise.resolve()
  await Promise.resolve()
  const ordinary = runtime.__sessions.saveBotMeta('ops', { chat: 'legacy-navigation' })
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'new-default')

  defaultWrite.resolve({ applied: { ui_meta: true } })
  await choosing
  await Promise.resolve()
  await Promise.resolve()
  ordinaryWrite.resolve({ applied: { ui_meta: true } })
  await ordinary

  assert.deepEqual(payloads.map(meta => meta.chat), ['new-default', 'new-default'])
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'new-default')
})

test('sessions workspace: a later ordinary chat write cannot defeat a failed default rollback', async () => {
  const defaultWrite = deferred()
  const ordinaryWrite = deferred()
  let write = 0
  const payloads: BotMetaPayload[] = []

  const runtime = await load({
    request: (method, params) => {
      if (method !== 'profiles.configure') {return undefined}
      payloads.push(plain(params.ui_meta['hermes-bots']))

      return [defaultWrite.promise, ordinaryWrite.promise][write++]
    }
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  const choosing = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  await Promise.resolve()
  await Promise.resolve()
  const ordinary = runtime.__sessions.saveBotMeta('ops', { chat: 'legacy-navigation' })

  defaultWrite.resolve({ applied: { ui_meta: false } })
  await choosing
  await Promise.resolve()
  await Promise.resolve()
  ordinaryWrite.resolve({ applied: { ui_meta: true } })
  await ordinary

  assert.deepEqual(payloads.map(meta => meta.chat), ['new-default', 'old-default'])
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'old-default')
})

test('sessions workspace: an ordinary chat queued under the echo guard stays suppressed after the echo', async () => {
  const pinWrite = deferred()
  const ordinaryWrite = deferred()
  let write = 0
  const payloads: BotMetaPayload[] = []

  const runtime = await load({
    request: (method, params) => {
      if (method !== 'profiles.configure') {return undefined}
      payloads.push(plain(params.ui_meta['hermes-bots']))
      const result = [Promise.resolve({ applied: { ui_meta: true } }), pinWrite.promise, ordinaryWrite.promise][write++]

      return result
    }
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default', pinned: false } })

  await runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  const pinning = runtime.__sessions.saveBotMeta('ops', { pinned: true })
  await Promise.resolve()
  await Promise.resolve()
  const ordinary = runtime.__sessions.saveBotMeta('ops', { chat: 'legacy-navigation' })
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'new-default', pinned: false } } }
  ])

  pinWrite.resolve({ applied: { ui_meta: true } })
  await pinning
  await Promise.resolve()
  await Promise.resolve()
  ordinaryWrite.resolve({ applied: { ui_meta: true } })
  await ordinary

  assert.deepEqual(payloads.map(meta => meta.chat), ['new-default', 'new-default', 'new-default'])
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'new-default')
})

test('sessions workspace: a queued Pin write carries a newly confirmed default', async () => {
  const defaultWrite = deferred()
  const pinWrite = deferred()
  let write = 0
  const payloads: BotMetaPayload[] = []

  const runtime = await load({
    request: (method, params) => {
      if (method !== 'profiles.configure') {return undefined}
      payloads.push(plain(params.ui_meta['hermes-bots']))

      return [defaultWrite.promise, pinWrite.promise][write++]
    }
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default', pinned: false } })

  const choosing = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  await Promise.resolve()
  await Promise.resolve()
  const pinning = runtime.__sessions.saveBotMeta('ops', { pinned: true })

  defaultWrite.resolve({ applied: { ui_meta: true } })
  await choosing
  await Promise.resolve()
  await Promise.resolve()
  pinWrite.resolve({ applied: { ui_meta: true } })
  await pinning

  assert.deepEqual(payloads.map(meta => meta.chat), ['new-default', 'new-default'])
  assert.deepEqual(plain(runtime.__sessions.$botMeta.get().ops), { chat: 'new-default', pinned: true })
})

test('sessions workspace: queued metadata writes retain their patches through stale roster merges', async () => {
  const pinWrite = deferred()
  const hideWrite = deferred()
  let write = 0
  const payloads: BotMetaPayload[] = []

  const runtime = await load({
    request: (method, params) => {
      if (method !== 'profiles.configure') {return undefined}
      payloads.push(plain(params.ui_meta['hermes-bots']))

      return [pinWrite.promise, hideWrite.promise][write++]
    }
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default', pinned: false, hidden: false } })

  const pinning = runtime.__sessions.saveBotMeta('ops', { pinned: true })
  await Promise.resolve()
  await Promise.resolve()
  const hiding = runtime.__sessions.saveBotMeta('ops', { hidden: true })
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'old-default', pinned: false, hidden: false } } }
  ])

  pinWrite.resolve({ applied: { ui_meta: true } })
  await pinning
  await Promise.resolve()
  await Promise.resolve()
  hideWrite.resolve({ applied: { ui_meta: true } })
  await hiding

  assert.equal(payloads.at(-1)?.pinned, true)
  assert.equal(payloads.at(-1)?.hidden, true)
  assert.deepEqual(plain(runtime.__sessions.$botMeta.get().ops), {
    chat: 'old-default',
    pinned: true,
    hidden: true
  })
})

test('sessions workspace: an earlier metadata result cannot overwrite a newer local patch', async () => {
  const firstWrite = deferred()
  const secondWrite = deferred()
  let write = 0

  const runtime = await load({
    request: method => method === 'profiles.configure' ? [firstWrite.promise, secondWrite.promise][write++] : undefined
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default', pinned: false } })

  const pinning = runtime.__sessions.saveBotMeta('ops', { pinned: true })
  await Promise.resolve()
  await Promise.resolve()
  const unpinning = runtime.__sessions.saveBotMeta('ops', { pinned: false })
  assert.equal(runtime.__sessions.$botMeta.get().ops.pinned, false)

  firstWrite.resolve({ applied: { ui_meta: true } })
  await pinning
  assert.equal(runtime.__sessions.$botMeta.get().ops.pinned, false)
  await Promise.resolve()
  await Promise.resolve()
  secondWrite.resolve({ applied: { ui_meta: true } })
  await unpinning
  assert.equal(runtime.__sessions.$botMeta.get().ops.pinned, false)
})

test('sessions workspace: a queued Pin write cannot revive a default whose persistence failed', async () => {
  const defaultWrite = deferred()
  const pinWrite = deferred()
  let write = 0
  const payloads: BotMetaPayload[] = []

  const runtime = await load({
    request: (method, params) => {
      if (method !== 'profiles.configure') {return undefined}
      payloads.push(plain(params.ui_meta['hermes-bots']))

      return [defaultWrite.promise, pinWrite.promise][write++]
    }
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default', pinned: false } })

  const choosing = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  await Promise.resolve()
  await Promise.resolve()
  const pinning = runtime.__sessions.saveBotMeta('ops', { pinned: true })

  defaultWrite.resolve({ applied: { ui_meta: false } })
  await choosing
  await Promise.resolve()
  await Promise.resolve()
  pinWrite.resolve({ applied: { ui_meta: true } })
  await pinning

  assert.deepEqual(payloads.map(meta => meta.chat), ['new-default', 'old-default'])
  assert.deepEqual(plain(runtime.__sessions.$botMeta.get().ops), { chat: 'old-default', pinned: true })
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': payloads.at(-1) } }
  ])
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'old-default')
})

test('sessions workspace: a stale roster refresh cannot overwrite an in-flight choice', async () => {
  const write = deferred()

  const runtime = await load({
    request: method => method === 'profiles.configure' ? write.promise : undefined
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  const choosing = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  await Promise.resolve()
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'old-default' } } }
  ])

  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'new-default')
  write.resolve({ applied: { ui_meta: true } })
  await choosing
})

test('sessions workspace: a pre-write roster response cannot win after persistence succeeds', async () => {
  const runtime = await load({
    request: method => method === 'profiles.configure' ? { applied: { ui_meta: true } } : undefined
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  await runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'old-default' } } }
  ])

  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'new-default')
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'new-default' } } }
  ])
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'new-default')
})

test('sessions workspace: the post-write echo guard expires back to server authority', async () => {
  const runtime = await load({
    request: method => method === 'profiles.configure' ? { applied: { ui_meta: true } } : undefined
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  await runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'server-later-default' } } }
  ], Number.MAX_SAFE_INTEGER)

  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'server-later-default')
})

test('sessions workspace: an explicit persistence failure rolls back the optimistic default', async () => {
  const runtime = await load({
    request: method => method === 'profiles.configure' ? { applied: { ui_meta: false } } : undefined
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  await runtime.__sessions.makeProfileSessionDefault('ops', { id: 'stored-123' }, 0)

  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'old-default')
  assert.deepEqual(plain(runtime.notifications), [
    { kind: 'error', message: 'Could not save the new default; kept the previous session.' }
  ])
})

test('sessions workspace: default actions localize their visible label, name, and live state', async () => {
  const runtime = await load()
  const session = { id: 'stored-123', title: 'Critical context' }

  const copy = {
    default: '既定',
    defaultLabel: (title: string) => `既定のセッション: ${title}`,
    makeDefault: '既定にする',
    makeDefaultLabel: (title: string) => `既定にする: ${title}`
  }

  const actionRow = runtime.__sessions.ProfileSessionRow({
    session,
    botName: 'ops',
    active: false,
    isDefault: false,
    gatewayGeneration: 0,
    copy
  })

  const action = actionRow.props.children[1]
  assert.equal(action.props.children, '既定にする')
  assert.equal(action.props['aria-label'], '既定にする: Critical context')

  const defaultRow = runtime.__sessions.ProfileSessionRow({
    session,
    botName: 'ops',
    active: false,
    isDefault: true,
    gatewayGeneration: 0,
    copy
  })

  const status = defaultRow.props.children[1]
  assert.equal(status.props.role, 'status')
  assert.equal(status.props['aria-live'], 'polite')
  assert.equal(status.props['aria-label'], '既定のセッション: Critical context')
  assert.equal(status.props.children, '既定')
})

test('sessions workspace: ships every new string in all supported plugin locales', async () => {
  const { BOT_SESSION_LOCALES } = (await load()).__sessions
  assert.deepEqual(Object.keys(BOT_SESSION_LOCALES).sort(), ['en', 'ja', 'zh', 'zh-hant'])

  for (const locale of Object.values(BOT_SESSION_LOCALES) as SessionLocale[]) {
    assert.equal(typeof locale.sessions.default, 'string')
    assert.equal(typeof locale.sessions.makeDefault, 'string')
    assert.equal(typeof locale.sessions.makeDefaultLabel, 'function')
    assert.equal(typeof locale.sessions.defaultLabel, 'function')
    assert.equal(typeof locale.sessions.changeFailed, 'string')
    assert.equal(typeof locale.sessions.persistenceFailed, 'string')
  }
})

test('sessions workspace: malformed profile or session input is a no-op', async () => {
  const runtime = await load()
  await runtime.__sessions.openProfileSession('../ops', { id: 'stored-123' }, 0)
  await runtime.__sessions.openProfileSession('ops', { id: '' }, 0)
  assert.deepEqual(runtime.calls, [])
})

test('sessions workspace: a gateway lifecycle change clears selection and rejects stale row clicks', async () => {
  const runtime = await load()
  runtime.__sessions.$botSelectedSessions.set({ ops: 'stored-123' })
  runtime.__sessions.handleSessionsGatewayTransition()
  assert.equal(runtime.__sessions.$sessionsGatewayGeneration.get(), 1)
  assert.deepEqual(plain(runtime.__sessions.$botSelectedSessions.get()), {})

  await runtime.__sessions.openProfileSession('ops', { id: 'stored-123' }, 0)
  assert.deepEqual(runtime.calls, [])
})

test('sessions workspace: a gateway transition releases pending default-session authority', async () => {
  const write = deferred()

  const runtime = await load({
    request: method => method === 'profiles.configure' ? write.promise : undefined
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  const choosing = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-default' }, 0)
  await Promise.resolve()
  runtime.__sessions.handleSessionsGatewayTransition()
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'other-gateway-default' } } }
  ])

  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'other-gateway-default')
  write.resolve({ applied: { ui_meta: true } })
  await choosing
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'other-gateway-default')
})

test('sessions workspace: an old gateway cleanup cannot delete a new gateway mutation guard', async () => {
  const oldWrite = deferred()
  const newWrite = deferred()
  let write = 0

  const runtime = await load({
    request: method => method === 'profiles.configure' ? [oldWrite.promise, newWrite.promise][write++] : undefined
  })

  runtime.__sessions.$botMeta.set({ ops: { chat: 'old-default' } })

  const oldChoice = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'old-gateway-choice' }, 0)
  await Promise.resolve()
  runtime.__sessions.handleSessionsGatewayTransition()
  runtime.__sessions.$botMeta.set({ ops: { chat: 'new-gateway-default' } })
  const newChoice = runtime.__sessions.makeProfileSessionDefault('ops', { id: 'new-gateway-choice' }, 1)
  await Promise.resolve()

  oldWrite.resolve({ applied: { ui_meta: true } })
  await oldChoice
  runtime.__sessions.mergeServerMeta([
    { name: 'ops', ui_meta: { 'hermes-bots': { chat: 'new-gateway-default' } } }
  ])
  assert.equal(runtime.__sessions.$botMeta.get().ops.chat, 'new-gateway-choice')

  newWrite.resolve({ applied: { ui_meta: true } })
  await newChoice
})

test('sessions workspace: an empty session with no preview does not demand history', async () => {
  const runtime = await load()
  await runtime.__sessions.openProfileSession('ops', { id: 'stored-empty', message_count: 0 }, 0)
  assert.deepEqual(plain(runtime.calls), [
    ['openSession', 'stored-empty', { profile: 'ops', awaitHydration: true, expectHistory: false, keepAllProfilesScope: false }]
  ])
})

test('sessions workspace: an in-flight open cannot restore selection after gateway replacement', async () => {
  let runtime: Awaited<ReturnType<typeof load>>
  runtime = await load({
    openSession: async () => runtime.__sessions.handleSessionsGatewayTransition()
  })

  await runtime.__sessions.openProfileSession('ops', { id: 'stored-123', preview: 'last line' }, 0)
  assert.equal(runtime.calls.length, 1)
  assert.deepEqual(plain(runtime.__sessions.$botSelectedSessions.get()), {})
})
