import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Activity toasts are OPT-IN: by default new bot activity only sets the
// unread badge — host.notify fires only when the 'activity-toasts' pref is
// enabled. A busy roster (cron runs, bot-to-bot chatter) must not firehose
// the user with notifications out of the box.

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function loadTracker(toastsEnabled, { selected = 'default', openKey = null, chatFocused = true, focusedChatId = null } = {}) {
  const start = source.indexOf('const rosterWatermarks = new Map()')
  const end = source.indexOf('/** Last good cron list', start)
  // The tracker keys watermarks off the REAL botActivitySession helper
  // (defined later in plugin.js) — extract it so the harness can't drift.
  const helperStart = source.indexOf('function botActivitySession(')
  const helperEnd = source.indexOf('/** Bots that are working', helperStart)
  assert.ok(helperStart >= 0 && helperEnd > helperStart, 'botActivitySession must remain extractable')
  const notifications = []
  const context = {
    pluginCtx: null,
    atom: initial => {
      let value = initial
      return { get: () => value, set: next => { value = next } }
    },
    host: {
      notify: params => notifications.push(params),
      // Tier 2: focus that arrived without a roster click (a tab).
      state: { focusedStoredSessionId: { get: () => focusedChatId } }
    },
    botChatIds: bot =>
      [bot?.canonical_session?.id, bot?.canonical_session?.resolved_id, bot?.last_session?.id]
        .filter(Boolean)
        .map(String),
    // The plugin's own open claim — the authoritative on-screen signal.
    // Canonical Bot Chats are hidden sessions, so no profile atom can
    // resolve one; they degrade to the gateway socket's home instead.
    $openBotChat: { get: () => (openKey ? { key: openKey } : null) },
    botRosterKey: bot => `local::${bot.name}`,
    // $selectedBot follows the socket's home and roster clicks: a guess,
    // and only consulted while some chat owns the center.
    $selectedBot: { get: () => selected },
    $botChatFocused: { get: () => chatFocused },
    // Bot-to-bot deliveries carry this prefix; the tracker uses it to tell a
    // real inbound message from the user's own send.
    A2A_PREFIX_RE: /^Message from (?:agent '[^']+'|🤖[^:]+):\s*/i,
    $botMeta: { get: () => ({}) },
    $botUnread: (() => {
      let value = {}
      return { get: () => value, set: next => { value = next } }
    })(),
    botRosterMeta: (bot, meta) => meta?.[bot.name] || null,
    botSelectionKey: bot => bot.name,
    displayName: bot => bot.name
  }
  const section = source
    .slice(helperStart, helperEnd)
    .concat('\n', source.slice(start, end))
    .concat(
      '\nglobalThis.__t = { trackInboundActivity, $activityToasts, setActivityToasts,',
      ' acknowledgeBotActivity, onScreenBotKey };\n'
    )
  vm.runInNewContext(section, context, { filename: 't.js' })
  if (toastsEnabled) {
    context.__t.$activityToasts.set(true)
  }
  return { ...context.__t, notifications, $botUnread: context.$botUnread }
}

function rosterAt(ts) {
  return [{ name: 'researcher', last_session: { last_active: ts, preview: 'Message from writer: hi' } }]
}

test('default: new activity sets unread badge but never toasts', () => {
  const t = loadTracker(false)
  t.trackInboundActivity(rosterAt(100)) // seeding poll
  t.trackInboundActivity(rosterAt(200)) // activity moved past watermark
  assert.equal(t.$botUnread.get().researcher, true, 'unread badge must still be set')
  assert.equal(t.notifications.length, 0, 'no toast by default')
})

test('opt-in: enabling the pref restores per-activity toasts', () => {
  const t = loadTracker(true)
  t.trackInboundActivity(rosterAt(100))
  t.trackInboundActivity(rosterAt(200))
  assert.equal(t.notifications.length, 1)
  assert.match(t.notifications[0].title, /New message for researcher/)
})

test('pref defaults OFF and persists via ctx.storage under activity-toasts', () => {
  const t = loadTracker(false)
  assert.equal(t.$activityToasts.get(), false, 'default must be off')
  assert.match(
    source.slice(source.indexOf('function setActivityToasts('), source.indexOf('/** Detect new inbound activity')),
    /storage\?\.set\?\.\('activity-toasts', enabled\)/
  )
  assert.match(source, /storage\?\.get\?\.\('activity-toasts'\)/)
})

test('activity in the hidden canonical Bot Chat still badges (the "6d ago" class)', () => {
  // The canonical Bot Chat is hidden from session lists, so last_session
  // never advances when a DM lands there — only canonical_session does.
  const t = loadTracker(false)
  const at = ts => [
    {
      name: 'researcher',
      last_session: { last_active: 100, preview: 'ancient scratch chat' },
      canonical_session: { last_active: ts, preview: 'Message from writer: hi' }
    }
  ]
  t.trackInboundActivity(at(150)) // seeding poll
  t.trackInboundActivity(at(250)) // Bot Chat got a DM; last_session unchanged
  assert.equal(t.$botUnread.get().researcher, true, 'hidden Bot Chat activity must set unread')
})

// `last_active` moves when the USER writes too, so a watermark alone lit the
// unread badge the instant you messaged an agent — "new" on a reply that had
// not happened yet.
test('unread: the user\'s own message never badges; the agent\'s reply does', () => {
  const t = loadTracker(false)
  const roster = (ts, preview, role) => [{ name: 'researcher', last_session: { last_active: ts, preview, last_role: role } }]

  t.trackInboundActivity(roster(100, 'hi there', 'user'))
  t.trackInboundActivity(roster(200, 'what do you think?', 'user'))
  assert.equal(t.$botUnread.get().researcher, undefined, 'writing TO an agent is not unread')

  t.trackInboundActivity(roster(300, 'I think it holds up.', 'assistant'))
  assert.equal(t.$botUnread.get().researcher, true, 'the reply is what the badge is for')
})

// A bot-to-bot delivery lands as a role:'user' row behind the A2A prefix —
// somebody else's message, so it badges.
test('unread: a bot-to-bot delivery badges even though it is stored as a user row', () => {
  const t = loadTracker(false)

  t.trackInboundActivity([{ name: 'researcher', last_session: { last_active: 100, preview: 'seed', last_role: 'assistant' } }])
  t.trackInboundActivity([
    { name: 'researcher', last_session: { last_active: 200, preview: "Message from agent 'writer': take a look", last_role: 'user' } }
  ])

  assert.equal(t.$botUnread.get().researcher, true)
})

test('unread: the bot whose chat is on screen never badges', () => {
  const t = loadTracker(false, { openKey: 'local::researcher' })

  t.trackInboundActivity([{ name: 'researcher', last_session: { last_active: 100, preview: 'seed', last_role: 'assistant' } }])
  t.trackInboundActivity([{ name: 'researcher', last_session: { last_active: 200, preview: 'done', last_role: 'assistant' } }])

  assert.equal(t.$botUnread.get().researcher, undefined)
})

// ── the "no new indicator after focusing another bot" regression ────────────
//
// Third iteration. The first two swapped WHICH profile atom the on-screen
// check consulted; both stayed wrong, because a canonical Bot Chat is a hidden
// session and neither atom can resolve one — they fall back to the gateway
// socket's home, which does not move when a tab opens another bot's chat.
// Compounding it, the watermark advanced BEFORE the suppression decision, so
// one wrong suppression consumed the edge permanently.

const bots = (name, ts, role = 'assistant') => [
  { name, canonical_session: { id: `chat-${name}`, last_active: ts, preview: 'x', last_role: role } },
  { name: 'writer', canonical_session: { id: 'chat-writer', last_active: 1, preview: 'x', last_role: 'assistant' } }
]

test('on-screen comes from the open claim, not the selected profile', () => {
  const t = loadTracker(false, { openKey: 'local::writer' })

  assert.equal(t.onScreenBotKey(bots('researcher', 5)), 'writer')
  assert.equal(loadTracker(false).onScreenBotKey(bots('researcher', 5)), null, 'no claim -> nobody')
})

// THE reported bug: message researcher, focus writer, researcher replies. The
// socket is still homed on researcher, so $selectedBot says researcher.
test('regression: a reply badges after focus moved to another bot', () => {
  const t = loadTracker(false, { selected: 'researcher', openKey: 'local::writer' })

  t.trackInboundActivity(bots('researcher', 100))
  t.trackInboundActivity(bots('researcher', 200, 'user'))
  assert.equal(t.$botUnread.get().researcher, undefined, 'writing TO a bot is never unread')

  t.trackInboundActivity(bots('researcher', 300))
  assert.equal(t.$botUnread.get().researcher, true, 'the reply must badge')
})

test('regression: a suppressed guess leaves the edge pending instead of eating it', () => {
  const guessed = loadTracker(false, { selected: 'researcher' })
  guessed.trackInboundActivity(bots('researcher', 100))
  guessed.trackInboundActivity(bots('researcher', 300))
  assert.equal(guessed.$botUnread.get().researcher, undefined, 'suppressed on this poll')

  // Same activity, same timestamp, guess gone: the badge must still be
  // raisable. The old unconditional advance killed it via ts <= prev.
  const away = loadTracker(false, { selected: 'writer' })
  away.trackInboundActivity(bots('researcher', 100))
  away.trackInboundActivity(bots('researcher', 300))
  assert.equal(away.$botUnread.get().researcher, true)
})

test('a trusted on-screen verdict acknowledges, so leaving does not re-badge', () => {
  const t = loadTracker(false, { openKey: 'local::researcher' })
  t.trackInboundActivity(bots('researcher', 100))
  t.trackInboundActivity(bots('researcher', 300))
  t.trackInboundActivity(bots('researcher', 300))

  assert.equal(t.$botUnread.get().researcher, undefined, 'already seen on screen')
})

test('opening a chat acknowledges pending activity so it cannot re-badge', () => {
  const t = loadTracker(false, { selected: 'researcher' })
  t.trackInboundActivity(bots('researcher', 100))
  t.trackInboundActivity(bots('researcher', 300))

  t.acknowledgeBotActivity({ name: 'researcher', canonical_session: { last_active: 300 } })

  t.trackInboundActivity(bots('researcher', 300))
  assert.equal(t.$botUnread.get().researcher, undefined, 'opening the chat settles it')
})

// With no claim AND no chat owning the center (the Bots home), nothing is on
// screen — suppressing there left a bot unbadgeable while the socket stayed
// homed on it, because the guess never flipped.
test('the Bots home suppresses nothing', () => {
  const t = loadTracker(false, { selected: 'researcher', chatFocused: false })

  t.trackInboundActivity(bots('researcher', 100))
  t.trackInboundActivity(bots('researcher', 300))

  assert.equal(t.$botUnread.get().researcher, true)
})

// Focusing another bot from a TAB never runs openRosterBot, and the focus edge
// releases the previous claim — so tier 1 is empty. Falling back to the
// profile guess there re-imported the root cause: $selectedBot still names the
// bot you messaged, because no profile atom can resolve a hidden Bot Chat.
test('regression: tab focus badges the bot you left, with no open claim', () => {
  const t = loadTracker(false, { selected: 'researcher', openKey: null, focusedChatId: 'chat-writer' })

  t.trackInboundActivity(bots('researcher', 100))
  t.trackInboundActivity(bots('researcher', 300))

  assert.equal(t.$botUnread.get().researcher, true)
})

test('tier 2 still suppresses the bot whose own chat is focused', () => {
  const t = loadTracker(false, { selected: 'default', openKey: null, focusedChatId: 'chat-researcher' })

  t.trackInboundActivity(bots('researcher', 100))
  t.trackInboundActivity(bots('researcher', 300))

  assert.equal(t.$botUnread.get().researcher, undefined)
})
