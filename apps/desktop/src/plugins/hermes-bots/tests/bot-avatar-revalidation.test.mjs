import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// #95613 — Bot Mode kept a stale locally cached avatar (and a stale
// "Hey, tell me about yourself!" onboarding preview) after the profile
// avatar was edited server-side. pullServerAvatars only fetched avatars
// when the local cache had NO image, so a cache that already held an old
// image was never revalidated — the roster rendered deleted/replaced art
// forever. Gateways now report an avatar fingerprint (avatar_mtime +
// avatar_size) on profiles.list rows; the plugin records the fingerprint
// next to every cached image, refetches when it changes (or when the
// cache predates fingerprinting), drops the cache when the server avatar
// is removed, and fences overwrites against the user's own fresh writes.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function load(options = {}) {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const requests = []
  const assetResponses = options.assetResponses || {}
  const context = {
    atom,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    atob: payload => Buffer.from(payload, 'base64').toString('binary'),
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: {
      request: (method, params) => {
        requests.push([method, params])
        const respond = assetResponses[method]
        return Promise.resolve(typeof respond === 'function' ? respond(params) : {})
      },
      state: {
        profile: { get: () => 'default', listen: () => undefined },
        gateway: { listen: () => undefined }
      }
    }
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat('\nglobalThis.__api = { saveBotMeta, $botMeta, pullServerAvatars, introOnlyPreview };\n')
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  context.plugin.register({
    storage: { get: () => null, set: () => undefined },
    register: () => undefined
  })
  return { ...context.__api, requests }
}

const OLD = 'data:image/png;base64,AAAA'
const NEW = 'data:image/png;base64,BBBB'

/** Flush the vm realm's promise reactions: `await Promise.resolve()` in the
 *  host realm does not reliably drain the sandbox's microtask queue, but a
 *  macrotask boundary (timer) runs the isolate-wide microtask checkpoint. */
const flush = () => new Promise(resolve => setTimeout(resolve, 0))

function getAssetCalls(requests) {
  return requests.filter(([method]) => method === 'profiles.get_asset').length
}

test('regression: a server-side avatar replacement replaces the stale cached image', async () => {
  const { $botMeta, pullServerAvatars, requests } = load({
    assetResponses: {
      'profiles.get_asset': () => ({ found: true, data: NEW })
    }
  })
  $botMeta.set({ ops: { image: OLD, imageStamp: '100:1' } })

  pullServerAvatars(
    [{ name: 'ops', has_avatar: true, avatar_mtime: 200, avatar_size: 50 }],
    Date.now()
  )
  await flush()

  assert.equal($botMeta.get().ops.image, NEW)
  assert.equal($botMeta.get().ops.imageStamp, '200:50')
  assert.equal(getAssetCalls(requests), 1)
})

test('regression: an unchanged server avatar is not refetched (no churn)', async () => {
  const { $botMeta, pullServerAvatars, requests } = load({
    assetResponses: {
      'profiles.get_asset': () => ({ found: true, data: NEW })
    }
  })
  $botMeta.set({ ops: { image: OLD, imageStamp: '100:1' } })

  pullServerAvatars(
    [{ name: 'ops', has_avatar: true, avatar_mtime: 100, avatar_size: 1 }],
    Date.now()
  )
  await flush()

  assert.equal($botMeta.get().ops.image, OLD)
  assert.equal($botMeta.get().ops.imageStamp, '100:1')
  assert.equal(getAssetCalls(requests), 0)
})

test('regression: a legacy cached image without a fingerprint is validated once', async () => {
  const { $botMeta, pullServerAvatars, requests } = load({
    assetResponses: {
      'profiles.get_asset': () => ({ found: true, data: OLD })
    }
  })
  $botMeta.set({ ops: { image: OLD } }) // cache predates fingerprinting

  pullServerAvatars(
    [{ name: 'ops', has_avatar: true, avatar_mtime: 200, avatar_size: 50 }],
    Date.now()
  )
  await flush()

  assert.equal($botMeta.get().ops.image, OLD)
  assert.equal($botMeta.get().ops.imageStamp, '200:50')
  assert.equal(getAssetCalls(requests), 1)
})

test('regression: a server-cleared avatar drops the stale cached image and is not re-pushed', async () => {
  const { $botMeta, pullServerAvatars, requests } = load()
  $botMeta.set({ ops: { image: OLD, imageStamp: '100:1' } })

  pullServerAvatars(
    [{ name: 'ops', has_avatar: false, avatar_mtime: null, avatar_size: null }],
    Date.now()
  )
  await flush()

  assert.equal($botMeta.get().ops.image, undefined)
  assert.equal($botMeta.get().ops.imageStamp, undefined)
  assert.equal(requests.filter(([method]) => method === 'profiles.set_asset').length, 0)
})

test('regression: an asset deleted between snapshot and fetch drops the cache too', async () => {
  const { $botMeta, pullServerAvatars, requests } = load({
    assetResponses: {
      'profiles.get_asset': () => ({ found: false })
    }
  })
  $botMeta.set({ ops: { image: OLD, imageStamp: '100:1' } })

  pullServerAvatars(
    [{ name: 'ops', has_avatar: true, avatar_mtime: 200, avatar_size: 50 }],
    Date.now()
  )
  await flush()

  assert.equal($botMeta.get().ops.image, undefined)
  assert.equal($botMeta.get().ops.imageStamp, undefined)
  assert.equal(getAssetCalls(requests), 1)
})

test('regression: a roster snapshot that predates a fresh local save never clobbers it', async () => {
  const { saveBotMeta, $botMeta, pullServerAvatars, requests } = load({
    assetResponses: {
      'profiles.get_asset': () => ({ found: true, data: OLD })
    }
  })

  saveBotMeta('ops', { image: NEW }) // user saves a new avatar right now
  const issuedBeforeWrite = Date.now() - 1000
  pullServerAvatars(
    [{ name: 'ops', has_avatar: true, avatar_mtime: 200, avatar_size: 50 }],
    issuedBeforeWrite
  )
  await flush()
  await flush()

  assert.equal($botMeta.get().ops.image, NEW)
  assert.equal(getAssetCalls(requests), 0)
})

test('regression: gateways without a fingerprint keep the legacy fetch-when-missing behavior', async () => {
  const { $botMeta, pullServerAvatars, requests } = load({
    assetResponses: {
      'profiles.get_asset': () => ({ found: true, data: NEW })
    }
  })
  $botMeta.set({ ops: { image: OLD } })

  // Older gateway: no avatar_mtime/avatar_size on the row.
  pullServerAvatars([{ name: 'ops', has_avatar: true }], Date.now())
  await flush()

  assert.equal($botMeta.get().ops.image, OLD)
  assert.equal(getAssetCalls(requests), 0)

  // ...but a bot with NO cached image still fetches, as before.
  pullServerAvatars([{ name: 'ops2', has_avatar: true }], Date.now())
  await flush()

  assert.equal($botMeta.get().ops2.image, NEW)
})

test('regression: intro-only onboarding previews are detected', () => {
  const { introOnlyPreview } = load()
  assert.equal(introOnlyPreview({ message_count: 1, preview: 'Hey, tell me about yourself!' }), true)
  assert.equal(introOnlyPreview({ message_count: 2, preview: 'Hey, tell me about yourself!' }), true)
  assert.equal(introOnlyPreview({ preview: 'Hey, tell me about yourself!' }), true)
  assert.equal(introOnlyPreview({ message_count: 1, preview: 'hello' }), false)
  assert.equal(introOnlyPreview({ message_count: 5, preview: 'Hey, tell me about yourself!' }), false)
  assert.equal(introOnlyPreview({ message_count: 1, preview: '' }), false)
  assert.equal(introOnlyPreview(null), false)
})
