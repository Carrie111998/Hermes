import assert from 'node:assert/strict'
import test from 'node:test'

import {
  botRolloverCommand,
  executeBotRollover,
  focusedCanonicalBot
} from '../bot-session-rollover.mjs'


const keyForBot = bot => `${bot.connectionId || 'local'}::${bot.name}`


test('/new and /reset are rollover commands while /compact is unchanged', () => {
  assert.equal(botRolloverCommand('/new'), 'new')
  assert.equal(botRolloverCommand('  /RESET  '), 'reset')
  assert.equal(botRolloverCommand('/new please'), null)
  assert.equal(botRolloverCommand('/compact'), null)
})

test('only the focused exact canonical owner resolves; Sessions mode passes through', () => {
  const local = {
    name: 'optimus',
    connectionId: 'local',
    canonical_session: { id: 'local-root', resolved_id: 'local-tip', title: 'Bot Chat' }
  }
  const remote = {
    name: 'optimus',
    connectionId: 'spark',
    remoteSource: true,
    canonical_session: { id: 'remote-root', resolved_id: 'remote-tip', title: 'Bot Chat' }
  }
  const roster = [local, remote]

  const target = focusedCanonicalBot({
    roster,
    selectedKey: 'spark::optimus',
    focusedStoredSessionId: 'remote-tip',
    keyForBot
  })
  assert.equal(target.bot, remote)
  assert.equal(target.expectedCurrentSessionId, 'remote-tip')

  assert.equal(
    focusedCanonicalBot({
      roster,
      selectedKey: 'spark::optimus',
      focusedStoredSessionId: 'local-tip',
      keyForBot
    }),
    null,
    'same-name bot on another connection must not collide'
  )
  assert.equal(
    focusedCanonicalBot({ roster, selectedKey: '', focusedStoredSessionId: 'remote-tip', keyForBot }),
    null,
    'Sessions mode has no exact Bot owner and remains untouched'
  )
  assert.equal(
    focusedCanonicalBot({
      roster,
      selectedKey: 'spark::optimus',
      focusedStoredSessionId: 'remote-tip',
      botsMode: false,
      keyForBot
    }),
    null,
    'leaving Bots mode disables the interception even if its tab remains focused'
  )
})


test('compressed canonical root and resolved tip both count as the focused chat', () => {
  const bot = {
    name: 'ops',
    canonical_session: { id: 'root', resolved_id: 'tip', title: 'Bot Chat' }
  }

  for (const focusedStoredSessionId of ['root', 'tip']) {
    const target = focusedCanonicalBot({
      roster: [bot],
      selectedKey: 'local::ops',
      focusedStoredSessionId,
      keyForBot
    })
    assert.equal(target.expectedCurrentSessionId, focusedStoredSessionId)
  }
})


test('rollover refreshes the roster and opens the returned fresh stored session', async () => {
  const calls = []
  const opened = []
  let refreshed = 0
  const target = {
    bot: { name: 'ops', connectionId: 'spark', remoteSource: true },
    canonical: { id: 'old-root', resolved_id: 'old-tip' },
    expectedCurrentSessionId: 'old-tip'
  }

  const result = await executeBotRollover({
    target,
    force: false,
    request: async (bot, method, params) => {
      calls.push({ bot, method, params })
      return {
        created: true,
        confirmation_required: false,
        current_session_id: 'fresh-session',
        current_session: { id: 'fresh-session', title: 'Bot Chat', message_count: 0 }
      }
    },
    profileForBot: () => 'ops',
    refresh: async () => {
      refreshed += 1
    },
    open: async (bot, id, summary) => {
      opened.push({ bot, id, summary })
    }
  })

  assert.equal(result.current_session_id, 'fresh-session')
  assert.equal(calls[0].method, 'session.bot_rollover')
  assert.deepEqual(calls[0].params, {
    expected_current_session_id: 'old-tip',
    force: false,
    profile: 'ops'
  })
  assert.equal(calls[0].bot, target.bot)
  assert.equal(refreshed, 1)
  assert.equal(opened[0].id, 'fresh-session')
  assert.equal(opened[0].summary.message_count, 0)
})


test('active work returns a confirmation result without refresh or navigation', async () => {
  let refreshed = false
  let opened = false
  const result = await executeBotRollover({
    target: {
      bot: { name: 'ops' },
      canonical: { id: 'old' },
      expectedCurrentSessionId: 'old'
    },
    request: async () => ({
      created: false,
      confirmation_required: true,
      active_reasons: ['turn']
    }),
    refresh: async () => {
      refreshed = true
    },
    open: async () => {
      opened = true
    }
  })

  assert.equal(result.confirmation_required, true)
  assert.equal(refreshed, false)
  assert.equal(opened, false)
})


test('a post-commit open failure is tagged recoverably instead of claiming rollback', async () => {
  await assert.rejects(
    () =>
      executeBotRollover({
        target: {
          bot: { name: 'ops' },
          canonical: { id: 'old' },
          expectedCurrentSessionId: 'old'
        },
        request: async () => ({
          created: true,
          current_session_id: 'fresh',
          current_session: { id: 'fresh', title: 'Bot Chat', message_count: 0 }
        }),
        refresh: async () => undefined,
        open: async () => {
          throw new Error('renderer temporarily unavailable')
        }
      }),
    error => {
      assert.equal(error.rolloverCommitted, true)
      assert.equal(error.currentSessionId, 'fresh')
      assert.match(error.message, /created.*could not be opened/i)
      return true
    }
  )
})
