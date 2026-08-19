import assert from 'node:assert/strict'

import { test } from 'vitest'

import { loadBotSessions, plain } from './runtime-harness'

test('regression: set_asset fires only when the avatar image changes', async () => {
  const runtime = await loadBotSessions()
  const { $botMeta, saveBotMeta } = runtime.__sessions
  const png = 'data:image/png;base64,AAAA'

  await Promise.all([
    saveBotMeta('ops', { image: png, title: 'One' }),
    saveBotMeta('ops', { image: png, title: 'Two' }),
    saveBotMeta('ops', { image: null, title: 'Three' }),
    saveBotMeta('ops', { title: 'Four' })
  ])

  const assetCalls = plain(
    runtime.calls
      .filter(([method]) => method === 'profiles.set_asset')
      .map(([, params]) => params)
  )

  assert.deepEqual(assetCalls, [
    { name: 'ops', asset: 'avatar', data: png },
    { name: 'ops', asset: 'avatar', clear: true }
  ])

  assert.equal($botMeta.get().ops.title, 'Four')
  assert.equal(runtime.calls.filter(([method]) => method === 'profiles.configure').length, 4)
})

test('regression: duplicating a bot still pushes the copied avatar once', async () => {
  const runtime = await loadBotSessions()
  const { saveBotMeta } = runtime.__sessions
  const png = 'data:image/png;base64,BBBB'

  await Promise.all([
    saveBotMeta('source', { image: png, title: 'Original' }),
    saveBotMeta('source-2', { image: png, title: 'Original (copy)' })
  ])

  const assetCalls = plain(
    runtime.calls
      .filter(([method]) => method === 'profiles.set_asset')
      .map(([, params]) => params)
  )

  assert.deepEqual(assetCalls, [
    { name: 'source', asset: 'avatar', data: png },
    { name: 'source-2', asset: 'avatar', data: png }
  ])
})
