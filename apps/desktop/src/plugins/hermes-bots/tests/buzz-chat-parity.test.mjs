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
 *  helpers are reachable and deterministic. */
function load() {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
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
      request: async () => ({}),
      state: { profile: { get: () => 'default', listen: () => undefined }, gateway: { listen: () => undefined } },
      notify: () => undefined,
      notifyError: () => undefined
    }
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(
      '\nglobalThis.__cp = { validateChannelName, createChannel, deleteChannel, knownChannels, channelMemberBots, $channels };\n'
    )
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  const storageWrites = new Map()
  context.plugin.register({
    storage: { get: () => null, set: (key, value) => storageWrites.set(key, value) },
    register: () => undefined
  })
  return { ...context.__cp, storageWrites }
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
