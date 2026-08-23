import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// Bot-row context-menu i18n (#91667): the right-click menu was hardcoded
// English while the app honors display.language. The plugin ships en/zh
// bundles under its own id via ctx.i18n.register (the kanban channel) and
// keeps working on hosts without that surface.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function load() {
  const atom = initial => {
    const slot = { get: () => current, set: value => (current = value) }
    let current = initial
    return slot
  }
  const context = {
    atom,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware', atCompletions: 'atCompletions' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: {
      state: {
        profile: { get: () => 'default', listen: () => undefined },
        gateway: { get: () => 'open', listen: () => undefined }
      },
      request: async () => ({}),
      notify: () => undefined
    },
    sdk: new Proxy({}, { get: () => undefined })
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  return context.plugin
}

function registerWithI18n() {
  const registered = []
  const ctx = {
    storage: { get: () => null, set: () => undefined },
    register: () => undefined,
    i18n: { register: bundles => registered.push(bundles) }
  }
  load().register(ctx)
  return registered
}

const MENU_KEYS = [
  'pinTop',
  'unpin',
  'hideBot',
  'unhideBot',
  'editProfile',
  'manageGroups',
  'groups',
  'duplicate',
  'newChat',
  'delete'
]

test('register ships the bot-row menu bundles under the plugin i18n channel', () => {
  const registered = registerWithI18n()

  assert.equal(registered.length, 1)
  const bundles = registered[0]
  for (const locale of ['en', 'ja', 'zh', 'zh-hant']) {
    assert.ok(bundles[locale], `${locale} bundle registered`)
    for (const key of MENU_KEYS) {
      assert.ok(key in bundles[locale], `${locale}.${key} present`)
    }
  }
})

test('zh bundle replaces every hardcoded menu label', () => {
  const { zh } = registerWithI18n()[0]

  assert.equal(zh.pinTop, '置顶')
  assert.equal(zh.unpin, '取消置顶')
  assert.equal(zh.hideBot, '隐藏机器人')
  assert.equal(zh.unhideBot, '取消隐藏机器人')
  assert.equal(zh.editProfile, '编辑资料')
  assert.equal(zh.manageGroups, '管理群组…')
  assert.equal(zh.duplicate, '复制')
  assert.equal(zh.newChat, '新建对话')
  assert.equal(zh.delete, '删除')
  assert.equal(typeof zh.groups, 'function')
  assert.equal(zh.groups(['a', 'b']), '群组：a、b…')
})

test('zh-hant and ja bundles align with the core glossary', () => {
  const bundles = registerWithI18n()[0]

  assert.equal(bundles['zh-hant'].pinTop, '釘選')
  assert.equal(bundles['zh-hant'].unpin, '取消釘選')
  assert.equal(bundles['zh-hant'].delete, '刪除')
  assert.equal(bundles.ja.pinTop, 'ピン留め')
  assert.equal(bundles.ja.unpin, 'ピン留めを解除')
  assert.equal(bundles.ja.delete, '削除')
})

test('register does not require ctx.i18n (older hosts stay on English)', () => {
  // Same shape as the other vm tests: a ctx with no i18n surface must not
  // throw during registration.
  assert.doesNotThrow(() => {
    load().register({ storage: { get: () => null, set: () => undefined }, register: () => undefined })
  })
})

test('menu items resolve through the translator, not literals', () => {
  const botRow = pluginSource.slice(pluginSource.indexOf('function BotRow('), pluginSource.indexOf('// ── model picker'))
  assert.match(botRow, /children: pinned \? t\('unpin'\) : t\('pinTop'\)/)
  assert.match(botRow, /children: t\('editProfile'\)/)
  assert.match(botRow, /children: groups\.length \? t\('groups', groups\) : t\('manageGroups'\)/)
  assert.match(botRow, /children: t\('duplicate'\)/)
  assert.match(botRow, /children: t\('newChat'\)/)
  assert.match(botRow, /children: t\('delete'\)/)
})
