const assert = require('node:assert/strict')
const test = require('node:test')

const { MEDIA_GRANT_TTL_MS, createEvaMediaGrantCodec } = require('./eva-media-grant.cjs')

test('managed media grants bind path and profile without exposing runtime credentials', () => {
  const codec = createEvaMediaGrantCodec({
    now: () => 1_000,
    secret: Buffer.alloc(32, 7)
  })

  const token = codec.mint({ path: '/srv/work/render.mp4', profile: 'research' })
  assert.doesNotMatch(token, /runtime|eva_session|ecs\.electricsheephq\.com/)
  assert.deepEqual(codec.verify(token), {
    expiresAt: 1_000 + MEDIA_GRANT_TTL_MS,
    path: '/srv/work/render.mp4',
    profile: 'research'
  })
})

test('managed media grants reject tampering, invalid profiles, and expiry', () => {
  let clock = 5_000
  const codec = createEvaMediaGrantCodec({
    now: () => clock,
    secret: Buffer.alloc(32, 9),
    ttlMs: 50
  })
  const token = codec.mint({ path: '/srv/work/audio.mp3', profile: 'default' })

  assert.equal(codec.verify(`${token.slice(0, -1)}x`), null)
  assert.throws(() => codec.mint({ path: '/srv/work/audio.mp3', profile: '../other' }), /profile is invalid/)
  clock += 50
  assert.equal(codec.verify(token), null)
})
