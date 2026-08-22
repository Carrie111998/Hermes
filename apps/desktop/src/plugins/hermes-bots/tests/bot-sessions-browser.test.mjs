import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Per-bot session browser (#91740): Bot Mode sessions are born hidden=1 in
// the global sidebar BY DESIGN, but nothing replaced their access path —
// only the canonical chat was reachable via roster click. The Bots pane must
// provide the browsing path instead: right-click → Browse sessions… lists
// every session on the bot's own profile with include_hidden (all plugin
// rows are hidden), and opens any row through openStoredBotChat.

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

test('the browser lists with include_hidden — plugin-owned rows are always hidden', async () => {
  const start = source.indexOf('async function fetchBotProfileSessions')
  const end = source.indexOf('/** True when a session summary IS the canonical registry row', start)
  const calls = []
  const context = {
    requestForBot: async (bot, method, params) => {
      calls.push({ bot, method, params })

      return {
        sessions: [
          { id: 'b-old', title: 'Old chat', last_active: 100 },
          { id: 'a-new', title: 'New chat', last_active: 200 }
        ]
      }
    },
    PROFILE_SESSION_LIST_LIMIT: 200
  }
  const section = source.slice(start, end).concat('\nglobalThis.__f = { fetchBotProfileSessions };\n')
  vm.runInNewContext(section, context, { filename: 'f.js' })

  const rows = await context.__f.fetchBotProfileSessions({ name: 'alpha' })

  assert.equal(calls.length, 1)
  assert.equal(calls[0].method, 'session.list')
  assert.equal(calls[0].params.profile, 'alpha')
  assert.equal(calls[0].params.include_hidden, true)
  // Newest first regardless of gateway ordering.
  assert.deepEqual(rows.map(row => row.id), ['a-new', 'b-old'])
})

test('list failure propagates — the dialog shows an error, never a silent empty list', async () => {
  const start = source.indexOf('async function fetchBotProfileSessions')
  const end = source.indexOf('/** True when a session summary IS the canonical registry row', start)
  const context = {
    requestForBot: async () => {
      throw new Error('unknown parameter: include_hidden')
    },
    PROFILE_SESSION_LIST_LIMIT: 200
  }
  const section = source.slice(start, end).concat('\nglobalThis.__f = { fetchBotProfileSessions };\n')
  vm.runInNewContext(section, context, { filename: 'f.js' })

  await assert.rejects(
    context.__f.fetchBotProfileSessions({ name: 'alpha' }),
    /include_hidden/
  )
})

test('open identity prefers resolved_id (compression-lineage tip) over id', () => {
  const start = source.indexOf('function pickSessionStorageId')

  assert.ok(start >= 0, 'pickSessionStorageId helper must exist')

  const context = {}
  vm.runInNewContext(`${source.slice(start, source.indexOf('/** True when', start))}\nglobalThis.__p = pickSessionStorageId;`, context)

  assert.equal(context.__p({ id: 'row-1', resolved_id: 'tip-9' }), 'tip-9')
  assert.equal(context.__p({ id: 'row-1' }), 'row-1')
  assert.equal(context.__p(null), undefined)
})

test('every local bot row wires Browse sessions… into its context menu', () => {
  // Source contract: BotRow takes onSessions and renders the menu item;
  // BotsPane passes setBrowsing down and mounts the dialog.
  const rowStart = source.indexOf('function BotRow({ bot, onDelete, onEdit, onGroup, onSessions })')

  assert.ok(rowStart >= 0, 'BotRow must accept onSessions')

  const menuEnd = source.indexOf('// ── model picker', rowStart)
  const rowSection = source.slice(rowStart, menuEnd)
  assert.match(rowSection, /Browse sessions…/)
  assert.match(rowSection, /onSessions\?\.\(bot\)/)

  const paneStart = source.indexOf('function BotsPane()')
  const paneSection = source.slice(paneStart)
  assert.match(paneSection, /onSessions: setBrowsing/)
  assert.match(paneSection, /jsx\(BotSessionsDialog, \{ bot: browsing/)
})

test('opening a listed row follows roster-click order: owner backend first, then openSession', async () => {
  const start = source.indexOf('function BotSessionsDialog({ bot, onClose })')
  const end = source.indexOf('function GroupDialog(', start)

  assert.ok(start >= 0 && end > start, 'BotSessionsDialog block must be extractable')

  const dialog = source.slice(start, end)
  // Order contract inside openRow: prepareBotSource BEFORE openStoredBotChat,
  // mirroring the roster-click activation sequence.
  const prepareAt = dialog.indexOf('await prepareBotSource(bot)')
  const openAt = dialog.indexOf('await openStoredBotChat(bot.name, storageId, row)')

  assert.ok(prepareAt >= 0 && openAt > prepareAt, 'prepareBotSource must precede openStoredBotChat')
})
