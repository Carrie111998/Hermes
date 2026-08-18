import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Buzz Chat Parity v1 (docs/acceptance/buzz-parity-v1.md) — plugin lane,
// rebased on top of upstream #89049 (group @-mention autocomplete already
// landed upstream, so the MN lane is NOT re-ported here).
// Covers the pure helpers behind copy-per-message / first-class channels
// plus source-contract wiring for the group room UI, and the attachment
// seam adapter (AT-01..AT-03) via the SDK controller capability.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

/** Load the plugin in a vm with scripted host/document so the chat-parity
 *  helpers are reachable and deterministic. Pass `sdkNamespace` to inject a
 *  fake SDK (attachment seam) and `turnScript(profile, prompt)` to drive
 *  member turns (session.create/resume/prompt.submit are simulated). */
function load(sdkNamespace, turnScript = () => 'ok') {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const calls = []
  const sessions = new Map()
  const runtimeToStored = new Map()
  let sessionSequence = 0

  const resolveSession = (profile, target) => {
    const stored = runtimeToStored.get(target) || (sessions.has(target) ? target : null)
    return stored ? sessions.get(stored) : null
  }
  const context = {
    atom,
    setTimeout: fn => {
      fn()
      return 0
    },
    clearTimeout: () => undefined,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: {
      request: async (method, params) => {
        if (method === 'session.create') {
          sessionSequence += 1
          const stored = `sid-${params.profile}-${sessionSequence}`
          const runtime = `rt-${params.profile}-${sessionSequence}`
          const session = { stored, runtime, profile: params.profile, title: params.title, messages: [] }
          sessions.set(stored, session)
          runtimeToStored.set(runtime, stored)
          return { session_id: runtime, stored_session_id: stored, message_count: 0, messages: [] }
        }
        if (method === 'session.resume') {
          const session = resolveSession(params.profile, params.session_id)
          if (!session) {
            throw new Error(`session not found: ${params.session_id}`)
          }
          return {
            session_id: session.runtime,
            session_key: session.stored,
            message_count: session.messages.length,
            messages: [...session.messages],
            inflight: false,
            running: false
          }
        }
        if (method === 'prompt.submit') {
          const session = resolveSession(null, params.session_id)
          if (!session) {
            throw new Error(`runtime session not found: ${params.session_id}`)
          }
          session.messages.push({ role: 'user', content: params.text })
          calls.push({ profile: session.profile, prompt: params.text, runtime: session.runtime, stored: session.stored })
          const reply = turnScript(session.profile, params.text, calls.length, session)
          session.messages.push({ role: 'assistant', content: reply })
          return {}
        }
        return {}
      },
      state: { profile: { get: () => 'default', listen: () => undefined }, gateway: { listen: () => undefined } },
      notify: () => undefined,
      notifyError: () => undefined
    }
  }
  if (sdkNamespace) {
    context.sdk = sdkNamespace
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(
      '\nglobalThis.__cp = { validateChannelName, createChannel, deleteChannel, knownChannels, channelMemberBots, $channels, getOrCreateRoomController, pendingAttachmentSnapshots, roomAttachmentControllers, runGroupChatRounds, updateGroupChat, $groupChats, sendToGroupChat };\n'
    )
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  const storageWrites = new Map()
  context.plugin.register({
    storage: { get: () => null, set: (key, value) => storageWrites.set(key, value) },
    register: () => undefined
  })
  return { ...context.__cp, storageWrites, calls }
}

// ── CP: copy text chat ───────────────────────────────────────────────────────

test('CP: every rendered group message exposes the SDK CopyButton (single clipboard authority)', () => {
  assert.match(pluginSource, /SdkCopyButton\s*=\s*typeof sdk === 'undefined' \? undefined : sdk\.CopyButton/)
  assert.match(pluginSource, /jsx\(SdkCopyButton, \{/)
  assert.match(pluginSource, /text: entry\.text/)
  assert.match(pluginSource, /label: `Copy message from \$\{label\}`/)
  // No second clipboard implementation: the plugin must not hand-roll
  // navigator.clipboard / execCommand / writeClipboardText.
  assert.doesNotMatch(pluginSource, /navigator\.clipboard/)
  assert.doesNotMatch(pluginSource, /execCommand/)
  assert.doesNotMatch(pluginSource, /writeClipboardText/)
})

// ── CH: first-class channels ─────────────────────────────────────────────────

test('CH: blank names are rejected', () => {
  const cp = load()
  assert.match(cp.validateChannelName('', []), /required/i)
  assert.match(cp.validateChannelName('   ', []), /required/i)
})

test('CH: duplicate names are rejected case-insensitively', () => {
  const cp = load()
  assert.match(cp.validateChannelName('Ops', ['ops']), /already exists/i)
})

test('CH: invalid characters are rejected', () => {
  const cp = load()
  assert.match(cp.validateChannelName('a/b', []), /letters, numbers/i)
  assert.match(cp.validateChannelName('x'.repeat(65), []), /max 64/i)
})

test('CH: valid names pass', () => {
  const cp = load()
  assert.equal(cp.validateChannelName('Ops Standup', []), null)
  assert.equal(cp.validateChannelName('ops-standup_2', ['other']), null)
})

test('CH: createChannel is single-flight, awaits storage, and reports honestly', async () => {
  const cp = load()
  const result = await cp.createChannel('Ops Standup', ['research', 'builder'])
  assert.equal(result.ok, true)
  assert.equal(result.channel.name, 'Ops Standup')
  assert.equal(JSON.stringify(result.channel.members), JSON.stringify(['research', 'builder']))
  // Duplicate is rejected.
  const dup = await cp.createChannel('ops standup', ['research', 'builder'])
  assert.equal(dup.ok, false)
  assert.match(dup.error, /already exists/i)
  // Fewer than 2 members is rejected.
  const one = await cp.createChannel('Solo', ['research'])
  assert.equal(one.ok, false)
  assert.match(one.error, /at least 2/i)
})

test('CH: channel membership is separate from botMeta.group (multi-channel, groups untouched)', async () => {
  const cp = load()
  await cp.createChannel('Alpha', ['research', 'builder'])
  await cp.createChannel('Beta', ['research', 'ops'])
  const channels = cp.$channels.get()
  const names = Object.values(channels).map(c => c.name).sort()
  assert.equal(JSON.stringify(names), JSON.stringify(['Alpha', 'Beta']))
  // The same bot belongs to both channels.
  assert.ok(Object.values(channels).every(c => c.members.includes('research')))
  // No botMeta.group was written: roster groups are untouched.
  assert.equal(cp.storageWrites.has('bot-meta'), false)
})

test('CH: create → reload → reopen preserves channel and membership', async () => {
  const cp = load()
  await cp.createChannel('Persist', ['research', 'builder'])
  const persisted = cp.storageWrites.get('channels')
  assert.ok(persisted, 'channel persisted under storage key channels')
  const channel = Object.values(persisted)[0]
  assert.equal(channel.name, 'Persist')
  assert.equal(JSON.stringify(channel.members), JSON.stringify(['research', 'builder']))
  // Rehydrate path: the register() hydrate reads storage 'channels' — the
  // source contract must wire it.
  assert.match(pluginSource, /ctx\.storage\?\.get\?\.\('channels'\)/)
  assert.match(pluginSource, /\$channels\.set\(\{ \.\.\.channels, \.\.\.\$channels\.get\(\) \}\)/)
})

test('CH: create-channel dialog is wired into the Bots pane with validation', () => {
  assert.match(pluginSource, /function CreateChannelDialog\(/)
  assert.match(pluginSource, /jsx\(CreateChannelDialog, \{/)
  assert.match(pluginSource, /'New Channel'/)
  assert.match(pluginSource, /Pick at least 2 bots/)
  assert.match(pluginSource, /createChannel\(name, selected\.map\(bot => botRosterKey\(bot\)\)\)/)
})

test('CH: channels render as first-class roster rows and reopen the room (CH-02)', () => {
  // Roster rows include channel rows with channel-owned membership.
  assert.match(pluginSource, /kind: 'channel'/)
  assert.match(pluginSource, /channelMemberBots\(channel, roster, allMeta\)/)
  // Mutation-sensitive: removing the channel spread from rosterRows must
  // turn this test RED (channels would silently vanish from the list).
  assert.match(pluginSource, /\.\.\.groupRows,\n\s*\.\.\.channelRows/)
  // Clicking a channel row opens the room via openChannel.
  assert.match(pluginSource, /function openChannel\(/)
  assert.match(pluginSource, /function ChannelMainView\(/)
  // The in-panel fallback seats channel members, not botMeta.group members.
  assert.match(pluginSource, /channelMemberBots\(Object\.values\(channels\)\.find/)
})

test('CH: deleteChannel removes the record and room log without touching botMeta.group', async () => {
  const cp = load()
  await cp.createChannel('Temp', ['research', 'builder'])
  const before = cp.$channels.get()
  assert.equal(Object.keys(before).length, 1)
  const result = await cp.deleteChannel('Temp')
  assert.equal(result.ok, true)
  assert.equal(Object.keys(cp.$channels.get()).length, 0)
  // No bot-meta write: roster groups are untouched.
  assert.equal(cp.storageWrites.has('bot-meta'), false)
  // Deleting an unknown channel reports honestly.
  const missing = await cp.deleteChannel('Nope')
  assert.equal(missing.ok, false)
})

test('CH: mutation-sensitive — channels never reuse botMeta.group as authority', () => {
  // The channel create path must not write { group: name } to bot meta
  // (that was the archived prototype's blocker CB-03).
  const createBody = pluginSource.slice(pluginSource.indexOf('async function createChannel('), pluginSource.indexOf('function channelMemberBots'))
  assert.doesNotMatch(createBody, /saveBotMeta/)
  assert.doesNotMatch(createBody, /group: /)
})

// ── AT: attachment adapter (CB-01/CB-02) ─────────────────────────────────────

test('AT: plugin feature-detects the SDK attachment seam and fails closed when absent', () => {
  // The seam is optional: older SDK builds disable the attach action with
  // an explanation — never a direct FileReader/file.attach fallback.
  assert.match(pluginSource, /SdkCreateAttachmentController = typeof sdk === 'undefined' \? undefined : sdk\.createAttachmentController/)
  assert.match(pluginSource, /Attachments need a newer Hermes Desktop/)
  // No raw transport fallback in the group composer path.
  const composerBody = pluginSource.slice(pluginSource.indexOf('function GroupChatWorkspace('), pluginSource.indexOf('function GroupChatMainView('))
  assert.doesNotMatch(composerBody, /file\.attach/)
  assert.doesNotMatch(composerBody, /readAsDataURL/)
})

test('AT: composer wires picker, chips with remove, and honest error state', () => {
  assert.match(pluginSource, /attachmentController\s*\.\s*pickFiles\(\)/)
  assert.match(pluginSource, /attachmentController\s*\.\s*remove\(item\.id\)/)
  assert.match(pluginSource, /'aria-label': `Remove \$\{item\.label\}`/)
  assert.match(pluginSource, /'aria-live': 'polite'/)
})

test('AT: send lifecycle — block while staging/error, immutable snapshot, per-member route-aware staging', () => {
  const submitBody = pluginSource.slice(pluginSource.indexOf('const submit = async () => {'), pluginSource.indexOf('const submitReply'))
  // Block send while any attachment is pending or errored.
  assert.match(submitBody, /item\.status === 'staging' \|\| item\.status === 'error'/)
  // Immutable snapshot captured at submit; stale completions ignored.
  assert.match(submitBody, /attachmentController\.snapshot\(\)/)
  // Attachment-only send is supported once ready.
  assert.match(submitBody, /!text && !attachments\.length/)
  // Submit does NOT stage into a hard-coded local target — it parks the
  // snapshot for the turn loop (CB-02: per-member route/session staging).
  assert.doesNotMatch(submitBody, /routeKey: 'local:default'/)
  assert.match(submitBody, /pendingAttachmentSnapshots\.set\(attachKey, snapshot\)/)

  // The turn loop stages into the member's own session and route.
  const turnBody = pluginSource.slice(pluginSource.indexOf('async function runGroupChatMemberTurn('), pluginSource.indexOf('// Baseline: how many messages exist before our submit.'))
  assert.match(turnBody, /botConnectionRoute\(member\)/)
  assert.match(turnBody, /sessionId: runtime/)
  assert.match(turnBody, /storedSessionId: stored \|\| null/)
  // Unknown outcome: never auto-retry; failure is surfaced in the room log.
  assert.match(turnBody, /Attachment staging failed/)
  assert.doesNotMatch(turnBody, /auto-retry/)
  // Canonical refs ride the member prompt; raw paths never leak into chat text.
  assert.match(turnBody, /refs\.join\('\\n'\)/)
})

test('AT: mutation-sensitive — removing the seam detection disables the capability-negative test', () => {
  // If the seam detection line is removed, the capability-negative test
  // above must turn RED (the attach action would silently vanish).
  assert.match(pluginSource, /SdkCreateAttachmentController/)
})

// ── AT-RG: executable lifecycle regression (CB-02, @sting/@honey findings) ──

/** Fake SDK attachment controller that MIMICS the real WeakMap snapshot
 *  contract: stage() rejects snapshots not created by the same instance
 *  (invalid-snapshot). Tracks stage/remove calls so the test can assert the
 *  whole-round lifecycle. */
function fakeSdkWithController() {
  const instances = []
  const sdkNamespace = {
    createAttachmentController: () => {
      const items = []
      const snapshots = new WeakMap()
      const controller = {
        stageCalls: [],
        removeCalls: [],
        $attachments: {
          get: () => [...items],
          listen: () => () => undefined
        },
        pickFiles: async () => {
          items.push({ id: 'a1', kind: 'file', label: 'report.pdf', status: 'ready' })
          return { added: 1, rejected: 0 }
        },
        addDropped: () => ({ added: 0, rejected: 0 }),
        clear: () => {
          items.length = 0
        },
        remove: id => {
          controller.removeCalls.push(id)
          const index = items.findIndex(item => item.id === id)

          if (index >= 0) {
            items.splice(index, 1)
            return true
          }

          return false
        },
        setContext: () => undefined,
        snapshot: () => {
          const snapshot = Object.freeze({
            attachments: Object.freeze(items.map(item => Object.freeze({ ...item }))),
            contextKey: 'room'
          })
          snapshots.set(snapshot, { attachments: [...items], contextVersion: 0 })

          return snapshot
        },
        stage: async (snapshot, target) => {
          if (!snapshots.has(snapshot)) {
            const error = new Error('Attachment snapshot was not created by this controller.')
            error.code = 'invalid-snapshot'
            throw error
          }

          controller.stageCalls.push({ sessionId: target.sessionId, routeKey: target.routeKey })

          return {
            attachments: snapshot.attachments.map(item => ({ id: item.id, kind: item.kind, label: item.label, refText: '@file:ref' })),
            sessionId: target.sessionId
          }
        }
      }
      instances.push(controller)

      return controller
    }
  }

  return { instances, sdkNamespace }
}

const ATTACH_MEMBERS = [{ name: 'research', title: '' }, { name: 'builder', title: '' }, { name: 'ops', title: '' }]

/** Seed a room with one user message (thread 't1') and an attachment send:
 *  snapshot parked under a pending key, room.pendingAttachKey set. */
async function seedAttachmentSend(cp, group = 'Room') {
  cp.updateGroupChat(group, room => {
    room.log = [{ from: { kind: 'user', name: 'You' }, text: 'here is the file', at: 1, thread: 't1' }]
    room.watermarks = { 't1::research': 0, 't1::builder': 0, 't1::ops': 0 }
    room.epoch = 1
    return room
  })
  const controller = cp.getOrCreateRoomController(group)
  await controller.pickFiles()
  const snapshot = controller.snapshot()
  const attachKey = `${group}\u0000${Date.now()}`
  cp.pendingAttachmentSnapshots.set(attachKey, snapshot)
  cp.updateGroupChat(group, room => {
    room.pendingAttachKey = attachKey
    return room
  })

  return { attachKey, controller }
}

test('AT-RG: one shared controller — snapshot stages for EVERY responder without invalid-snapshot', async () => {
  const { instances, sdkNamespace } = fakeSdkWithController()
  const cp = load(sdkNamespace, () => 'got it')
  // The composer and the turn loop must resolve the SAME instance: a second
  // instance would make stage() reject the snapshot (WeakMap is per-instance).
  const first = cp.getOrCreateRoomController('Room')
  const second = cp.getOrCreateRoomController('Room')
  assert.equal(first, second, 'one controller per room, shared by composer and turn loop')

  const { attachKey } = await seedAttachmentSend(cp)
  await cp.runGroupChatRounds('Room', ATTACH_MEMBERS, 't1', attachKey)

  const controller = instances[0]
  // EVERY responder stages the SAME send into its own session — the exact
  // WeakMap snapshot created by the shared controller must stay valid across
  // the whole round (no invalid-snapshot, no clear() between members).
  const profilesStaged = new Set(controller.stageCalls.map(call => call.sessionId.split('-')[1]))
  assert.equal(profilesStaged.has('research'), true, 'research staged the send')
  assert.equal(profilesStaged.has('builder'), true, 'builder staged the send')
  assert.equal(profilesStaged.has('ops'), true, 'ops staged the send')
  // No staging-failure entry: an invalid-snapshot would have surfaced here.
  const log = cp.$groupChats.get().Room.log
  assert.ok(!log.some(entry => /staging failed/i.test(entry.text)), 'no staging failure in the room log')
})

test('AT-RG: send snapshot survives the whole round and is finalized only after all responders', async () => {
  const { instances, sdkNamespace } = fakeSdkWithController()
  const cp = load(sdkNamespace, () => 'got it')
  const { attachKey } = await seedAttachmentSend(cp)

  await cp.runGroupChatRounds('Room', ATTACH_MEMBERS, 't1', attachKey)

  // After the round: the key is consumed, the room flag is gone, and the
  // sent items were removed from the controller scope (chips cleared).
  assert.equal(cp.pendingAttachmentSnapshots.get(attachKey), undefined, 'snapshot consumed after the round')
  assert.equal(cp.$groupChats.get().Room.pendingAttachKey, undefined, 'pending flag cleared after the round')
  assert.equal(instances[0].removeCalls.length, 1, 'sent attachment removed from the controller scope')
})

test('AT-RG: attachment-only send drives the room (no text, staged files still start a round)', async () => {
  const { sdkNamespace } = fakeSdkWithController()
  const cp = load(sdkNamespace, () => 'got it')
  const { attachKey } = await seedAttachmentSend(cp)

  // The composer's attachment-only path: no text, but the send is bound to
  // the snapshot key — the room must still round-robin (file delivery).
  // sendToGroupChat must NOT reject a text-less send when files are staged.
  const thread = cp.sendToGroupChat('Room', ATTACH_MEMBERS, '', null, attachKey)
  assert.ok(thread, 'attachment-only send starts a round (not rejected)')
  assert.equal(cp.$groupChats.get().Room.running, true, 'room marked running for the attachment-only send')
  // The snapshot stays parked for the (async) drive — it was not dropped.
  assert.equal(cp.pendingAttachmentSnapshots.get(attachKey) === undefined, false, 'snapshot parked for the round')
})

test('AT-RG: a newer send while a round runs is NOT consumed by the older round', async () => {
  const { sdkNamespace } = fakeSdkWithController()
  const cp = load(sdkNamespace, () => 'got it')
  const first = await seedAttachmentSend(cp)
  // Second send: park a NEW snapshot under a different key while the first
  // round is still live (the old round must not delete the new key).
  await cp.getOrCreateRoomController('Room').pickFiles()
  const secondSnapshot = cp.getOrCreateRoomController('Room').snapshot()
  const secondKey = 'Room\u0000second'
  cp.pendingAttachmentSnapshots.set(secondKey, secondSnapshot)
  cp.updateGroupChat('Room', room => {
    room.pendingAttachKey = secondKey
    return room
  })

  // The older round finalizes ONLY its own key.
  await cp.runGroupChatRounds('Room', ATTACH_MEMBERS, 't1', first.attachKey)

  assert.equal(cp.pendingAttachmentSnapshots.get(first.attachKey), undefined, 'old round consumed its own key')
  assert.equal(cp.pendingAttachmentSnapshots.get(secondKey), secondSnapshot, 'newer send snapshot survives the older round')
})
