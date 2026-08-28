import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

// #95613 — Bot Mode keeps stale local avatar and chat preview after profile
// update. Two regressions:
//   1. pullServerAvatars skipped any bot whose local cache already held an
//      image, so a server-side avatar change never replaced the stale local
//      one.
//   2. BotRow always rendered the session preview as the row subtitle, so a
//      stale preview (e.g. the "Hey, tell me about yourself!" onboarding intro)
//      lingered even after the profile description was updated.

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

// ── avatar sync harness (mirrors bot-meta-asset-sync.test.mjs) ──────────────

function avatarLoad(serverAsset) {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const requests = []
  const context = {
    atom,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    host: {
      request: (method, params) => {
        requests.push([method, params])
        if (method === 'profiles.get_asset') {
          return Promise.resolve({ found: true, data: serverAsset })
        }
        return Promise.resolve({})
      },
      state: { profile: { get: () => 'default', listen: () => undefined }, gateway: { listen: () => undefined } }
    }
  }
  const source = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat('\nglobalThis.__meta = { pullServerAvatars, $botMeta };')
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  context.plugin.register({
    storage: { get: () => null, set: () => undefined },
    register: () => undefined
  })
  return { ...context.__meta, requests }
}

test('regression: a changed server avatar replaces a stale local one', async () => {
  const staleLocal = 'data:image/png;base64,STALE'
  const serverAsset = 'data:image/png;base64,FRESH'
  const { pullServerAvatars, $botMeta, requests } = avatarLoad(serverAsset)

  // Local cache holds a stale image for a bot flagged has_avatar.
  $botMeta.set({ ops: { image: staleLocal } })

  await pullServerAvatars([{ name: 'ops', has_avatar: true }])
  await new Promise(resolve => setTimeout(resolve, 0))

  // The server asset must have replaced the stale local image.
  assert.equal($botMeta.get().ops.image, serverAsset)
  assert.ok(requests.some(([method]) => method === 'profiles.get_asset'))
})

test('regression: unchanged server avatar is not re-fetched on every refetch', async () => {
  const serverAsset = 'data:image/png;base64,FRESH'
  const { pullServerAvatars, $botMeta, requests } = avatarLoad(serverAsset)

  $botMeta.set({ ops: { image: serverAsset } })

  await pullServerAvatars([{ name: 'ops', has_avatar: true }])
  await Promise.resolve()
  const first = requests.filter(([method]) => method === 'profiles.get_asset').length

  await pullServerAvatars([{ name: 'ops', has_avatar: true }])
  await Promise.resolve()
  const second = requests.filter(([method]) => method === 'profiles.get_asset').length

  // First pull fetches and applies; the second sees the same server asset and
  // skips the redundant fetch.
  assert.equal(first, 1)
  assert.equal(second, 1)
})

// ── BotRow subtitle harness (mirrors roster-preview.test.mjs) ───────────────

function renderRuntime() {
  const atom = value => ({ get: () => value, set: () => undefined })
  const jsx = (type, props = {}) => ({ type, props })
  const context = {
    atom,
    jsx,
    jsxs: jsx,
    cn: (...args) => args.filter(Boolean).join(' '),
    Button: 'Button',
    BotFace: 'BotFace',
    Codicon: 'Codicon',
    ContextMenu: 'ContextMenu',
    ContextMenuContent: 'ContextMenuContent',
    ContextMenuItem: 'ContextMenuItem',
    ContextMenuSeparator: 'ContextMenuSeparator',
    ContextMenuTrigger: 'ContextMenuTrigger',
    Tip: 'Tip',
    haptic: () => undefined,
    host: {
      state: {
        profile: { get: () => 'scribe', listen: () => undefined },
        gateway: { get: () => 'idle', listen: () => undefined }
      },
      request: () => Promise.resolve({ sessions: [] }),
      openSession: () => undefined,
      newChat: () => undefined,
      navigate: () => undefined
    },
    profileColor: () => '#8b5cf6',
    queryClient: { invalidateQueries: () => undefined },
    relativeTime: () => 'now',
    useQuery: () => ({}),
    useValue: value => (value?.get ? value.get() : value),
    useState: value => [value, () => undefined],
    useEffect: () => undefined,
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } }
  }
  const code = pluginSource
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat('\nglobalThis.__BotRow = BotRow;')
  vm.runInNewContext(code, context)
  return context
}

function textOf(node) {
  if (node == null || typeof node === 'boolean') return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(textOf).join(' ')
  if (typeof node === 'object') {
    if (node.props) return textOf(node.props.children ?? '')
    return Object.values(node).map(textOf).join(' ')
  }
  return ''
}

test('regression: BotRow prefers the configured description over a stale preview', () => {
  const r = renderRuntime()
  const tree = r.__BotRow({
    bot: {
      name: 'ops',
      title: 'Ops',
      description: 'Handles all infrastructure deploys',
      last_session: { id: 's1', title: 'Bot Chat', preview: 'Hey, tell me about yourself!', last_active: 1_700_000_000 }
    },
    onEdit: () => undefined
  })
  const text = textOf(tree)
  assert.match(text, /Handles all infrastructure deploys/)
  assert.doesNotMatch(text, /Hey, tell me about yourself!/)
})

test('regression: BotRow still shows the preview when no description is set', () => {
  const r = renderRuntime()
  const tree = r.__BotRow({
    bot: {
      name: 'ops',
      title: 'Ops',
      description: '',
      last_session: { id: 's2', title: 'Bot Chat', preview: 'All hosts are healthy', last_active: 1_700_000_000 }
    },
    onEdit: () => undefined
  })
  const text = textOf(tree)
  assert.match(text, /All hosts are healthy/)
})
