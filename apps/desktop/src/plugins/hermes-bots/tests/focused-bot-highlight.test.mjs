import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

// The roster highlight and the Routines (Cronjobs) tile must follow the chat
// the user is LOOKING AT — the focused session's owner profile — not the
// gateway socket's home. Tab/tile focus moves without swapping the socket, so
// keying these off `host.state.profile` alone highlighted (and scoped the
// Cronjobs panel to) the wrong bot whenever a focused tab showed another
// profile's chat (community report: Newsanalyst chat open, Hermes highlighted).

test('$focusedBotOwner prefers the connection-qualified focused owner atom', () => {
  assert.match(
    source,
    /const \$focusedBotOwner = host\.state\.focusedSessionOwner \|\|/,
    'newer desktops expose a complete focused owner; older builds retain a feature-detected fallback'
  )
})

test('legacy focused-profile-only SDK never pairs foreign focus with ambient connection', () => {
  const ownerStart = source.indexOf('const $focusedBotProfile =')
  const ownerEnd = source.indexOf('/** Optional secondary navigation', ownerStart)
  const activeStart = source.indexOf('function isActiveRosterBot(')
  const activeEnd = source.indexOf('function botSelectionKey(', activeStart)
  const store = value => ({ get: () => value, listen: () => undefined })
  const context = {
    host: {
      activeConnectionId: () => 'source-a',
      state: {
        connectionId: store('source-a'),
        focusedSessionProfile: store('worker'),
        profile: store('default')
      }
    }
  }

  vm.runInNewContext(
    `${source.slice(ownerStart, ownerEnd)}\n${source.slice(activeStart, activeEnd)}\n` +
      'globalThis.result = { owner: focusedRosterOwner($focusedBotOwner.get()), isActiveRosterBot };',
    context
  )

  assert.equal(context.result.owner, null)
  assert.equal(context.result.isActiveRosterBot({
    connectionId: 'source-a',
    name: 'worker',
    remoteSource: true
  }, context.result.owner), false)
  assert.equal(context.result.isActiveRosterBot({ name: 'default' }, context.result.owner), false)
})

test('BotRow keys the highlight off the focused profile, not the socket home', () => {
  const rowStart = source.indexOf('function BotRow(')
  assert.ok(rowStart >= 0)
  const row = source.slice(rowStart, rowStart + 2000)

  assert.match(row, /const focusedOwner = focusedRosterOwner\(useValue\(\$focusedBotOwner\)\)/)
  assert.match(row, /const isActive = botRowOwnsWorkspace\([\s\S]*?focusedOwner,[\s\S]*?selectedRosterKey/)
})

// Iterations 1-3 each pinned ONE global turn bit to ONE bot, so only the
// focused row could ever show work. Iteration 3's expression, restored here,
// also had to be wrong: `host.state.gateway` is socket state and never reads
// 'busy'. Mid-turn is now per-chat, keyed by the bot's own stored session ids.
test('BotRow reads mid-turn per chat, not from focus or the socket', () => {
  const rowStart = source.indexOf('function BotRow(')
  const row = source.slice(rowStart, rowStart + 7000)

  assert.match(row, /const workingChats = useValue\(\$workingChats\)/)
  assert.match(
    row,
    /const working = workerWorking \|\| \(!answered && !stalled && botOwnsWorkingChat\(bot, workingChats\)\)/
  )
  assert.match(row, /const botMood = working \? 'work' : 'idle'/)
  assert.ok(!/gatewayState === 'busy'/.test(row), 'the socket atom never reads busy')
  assert.ok(!/focusedTurnBusy/.test(row), 'a single focused-chat bit cannot answer a roster')
})

test('the working-chats atom is feature-detected for older desktops', () => {
  assert.match(source, /const \$workingChats = host\.state\.workingStoredSessionIds \|\| atom\(\[\]\)/)
  assert.match(source, /const \$stalledChats = host\.state\.stalledStoredSessionIds \|\| atom\(\[\]\)/)
})

// A turn that dies before any assistant message leaves `last_role` on 'user'
// (so `answered` never flips) and, with no terminal frame and no socket drop,
// `busy` stranded true — the dots would run forever. The stream watchdog is
// the only witness that arrives in that case.
test('a stalled chat releases the typing dots even when the busy flag is stranded', () => {
  const idsStart = source.indexOf('function botChatIds(')
  const ownsStart = source.indexOf('function botOwnsChat(')
  const stalledStart = source.indexOf('function botOwnsStalledChat(')
  const body =
    source.slice(idsStart, source.indexOf('\n}', idsStart) + 2) +
    source.slice(ownsStart, source.indexOf('\n}', ownsStart) + 2) +
    source.slice(stalledStart, source.indexOf('\n}', stalledStart) + 2)
  const stalledFor = new Function(`${body}; return botOwnsStalledChat`)()

  const bot = { canonical_session: { id: 'reg-1', resolved_id: 'tip-9' } }
  const working = (answered, stalled) => !answered && !stalled

  assert.equal(stalledFor(bot, ['tip-9']), true)
  assert.equal(stalledFor(bot, []), false)
  // Stranded busy + no assistant reply yet: the watchdog releases the dots.
  assert.equal(working(false, stalledFor(bot, ['tip-9'])), false)
  // A healthy long turn is quiet in the TRANSCRIPT, not on the stream, so it
  // never lands in the stalled set and keeps its dots.
  assert.equal(working(false, stalledFor(bot, [])), true)
})

// The turn lands on whichever id the runtime bound, so both must match.
test('botOwnsWorkingChat matches the registry id and the lineage tip', () => {
  const idsStart = source.indexOf('function botChatIds(')
  const ownsStart = source.indexOf('function botOwnsChat(')
  const start = source.indexOf('function botOwnsWorkingChat(')
  const body =
    source.slice(idsStart, source.indexOf('\n}', idsStart) + 2) +
    source.slice(ownsStart, source.indexOf('\n}', ownsStart) + 2) +
    source.slice(start, source.indexOf('\n}', start) + 2)
  const owns = new Function(`${body}; return botOwnsWorkingChat`)()

  const bot = { canonical_session: { id: 'reg-1', resolved_id: 'tip-9' } }
  assert.equal(owns(bot, ['tip-9']), true)
  assert.equal(owns(bot, ['reg-1']), true)
  assert.equal(owns(bot, ['someone-else']), false)
  assert.equal(owns(bot, []), false)
  assert.equal(owns({}, ['tip-9']), false)
})

test('RoutinesPane scopes the Cronjobs tile to the focused chat owner', () => {
  const paneStart = source.indexOf('function RoutinesPane(')
  assert.ok(paneStart >= 0)
  const pane = source.slice(paneStart, paneStart + 1200)

  assert.match(pane, /const focusedOwner = focusedRosterOwner\(useValue\(\$focusedBotOwner\)\)/)
  // The roster read must be a SUBSCRIPTION, not a bare .get(): BotsHomeView
  // owns the fetch and can hydrate after this pane mounted, so a bare snapshot
  // pinned the tile on "unavailable" forever (#94483). Scoping intent is
  // unchanged — it still keys off the focused owner, never the socket-home
  // profile atom asserted against below.
  assert.match(pane, /const owner = resolveRoutineOwner\(useValue\(\$lastRoster\), focusedOwner, selected\)/)
  assert.ok(!/useValue\(host\.state\.profile\)/.test(pane), 'the tile must not read the socket-home atom directly')
})

test('the $selectedBot tracker binds the focused profile ladder (reseed + unbind captured)', () => {
  assert.match(source, /const unbindProfileListener = bindProfileSync\(\$focusedBotOwner\)/)
})

// The gateway reports the newest tool/kanban row whether or not it is alive,
// and the 150s window is heartbeat tolerance, not proof — so a finished turn's
// tool row used to hold the typing dots lit for the whole window.
test('a finished turn does not keep the dots on via its tool row', () => {
  const rowStart = source.indexOf('function BotRow(')
  const row = source.slice(rowStart, rowStart + 7000)

  assert.match(row, /const workerWorking = workerLive && workerTs > chatTs/)
  // The age label still counts a live worker, dots or not.
  assert.match(row, /const rowAgeTs = workerLive \? Math\.max\(chatTs, workerTs\) : chatTs/)
})

// Every bot runs its own gateway process, so a background bot's turn-end never
// reaches the renderer and its busy flag is stranded true forever (observed:
// dots still running minutes after the reply landed, with no worker session on
// the profile at all). The roster poll is the witness that always arrives.
test('a stranded busy flag cannot outlive the answer', () => {
  const rowStart = source.indexOf('function BotRow(')
  const row = source.slice(rowStart, rowStart + 8000)

  assert.match(row, /const answered = String\(activitySession\?\.last_role \|\| ''\)\.toLowerCase\(\) === 'assistant'/)
  assert.match(
    row,
    /const working = workerWorking \|\| \(!answered && !stalled && botOwnsWorkingChat\(bot, workingChats\)\)/
  )
})
