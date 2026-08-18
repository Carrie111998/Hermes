import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

// The pose engine is a set of pure functions between the ring samplers and
// the easing memo. Slice them out (plus the string PRNG they seed from) so
// they run in a bare vm without the ES-module surface.
function slice(startMarker, endMarker) {
  const start = pluginSource.indexOf(startMarker)
  const end = pluginSource.indexOf(endMarker, start)
  assert.ok(start > 0 && end > start, `${startMarker} present in plugin.js`)
  return pluginSource.slice(start, end)
}

const source = [
  slice('function sigilRng(text)', '\n/**\n * Angular hermetic sigil'),
  slice('/** Moods a face can hold.', 'const EASED_CHANNELS')
].join('\n')

const context = {}
vm.runInNewContext(
  `${source}\nglobalThis.FACE_MOODS = FACE_MOODS; globalThis.facePose = facePose; ` +
    'globalThis.facePhase = facePhase; globalThis.blinkSquash = blinkSquash; globalThis.idleGaze = idleGaze',
  context
)

const CHANNELS = ['turn', 'tilt', 'roll', 'gazeX', 'gazeY', 'sx', 'sy', 'cant', 'lid', 'cheek', 'bob', 'blink', 'd0', 'd1', 'd2']

test('facePose: every mood returns every channel as a finite number', () => {
  for (const mood of context.FACE_MOODS) {
    for (const t of [0, 0.5, 3.7, 12.25, 61]) {
      const pose = context.facePose(mood, t)
      for (const key of CHANNELS) {
        assert.equal(typeof pose[key], 'number', `${mood}.${key} at t=${t} is a number`)
        assert.ok(Number.isFinite(pose[key]), `${mood}.${key} at t=${t} is finite`)
      }
      assert.ok(pose.blink >= 0 && pose.blink <= 1, `${mood}.blink in [0,1]`)
      assert.ok(pose.lid >= 0 && pose.lid <= 1, `${mood}.lid in [0,1]`)
      assert.ok(pose.cheek >= 0 && pose.cheek <= 1, `${mood}.cheek in [0,1]`)
    }
  }
})

test('facePose: a pure function of time (replayable, no hidden state)', () => {
  for (const mood of context.FACE_MOODS) {
    const a = context.facePose(mood, 8.4)
    context.facePose(mood, 100)
    const b = context.facePose(mood, 8.4)
    assert.deepEqual(a, b, `${mood} replays identically`)
  }
})

test('facePose: unknown moods fall back to the idle pose', () => {
  assert.deepEqual(context.facePose('nonsense', 2.2), context.facePose('idle', 2.2))
})

test('facePose: moods read the way the roster expects', () => {
  const at = mood => context.facePose(mood, 1)
  // Happy is an upward crescent (cheek up), not a squint from above.
  assert.ok(at('happy').cheek > 0.4)
  assert.equal(at('happy').lid, 0)
  // Sleepy and sick drop the upper lid; idle and work keep it up.
  assert.ok(at('sleepy').lid > 0.3)
  assert.ok(at('sick').lid > 0.3)
  assert.equal(at('idle').lid, 0)
  assert.equal(at('work').lid, 0)
  // Sad cants the pupils inner-corner-up, work inner-corner-down.
  assert.ok(at('sad').cant > 0)
  assert.ok(at('work').cant < 0)
  // Shy looks away, surprised opens wide.
  assert.ok(at('shy').gazeX < -1.5)
  assert.ok(at('surprised').sy > 1.2 && at('surprised').sx > 1.2)
  // Only work lights the thinking dots.
  assert.ok(at('work').d0 + at('work').d1 + at('work').d2 > 0)
  assert.equal(at('idle').d0 + at('idle').d1 + at('idle').d2, 0)
})

test('blinkSquash: blinks happen, are brief, and are irregular between slots', () => {
  const period = 3.4
  let closedFrames = 0
  let total = 0
  const blinkStarts = []
  let wasOpen = true

  for (let t = 0; t < period * 12; t += 1 / 60) {
    const s = context.blinkSquash(t, period, 0.16)
    total += 1
    if (s > 0.5) closedFrames += 1
    if (s > 0 && wasOpen) blinkStarts.push(t % period)
    wasOpen = s === 0
  }

  // Shut less than a tenth of the time, but not never.
  assert.ok(closedFrames > 0 && closedFrames / total < 0.1, `shut fraction ${closedFrames / total}`)
  // Offsets inside the slot vary (not a fixed metronome).
  const distinct = new Set(blinkStarts.map(x => x.toFixed(1)))
  assert.ok(distinct.size >= 4, `distinct blink offsets: ${[...distinct].join(', ')}`)
})

test('idleGaze: bounded wander with occasional glances', () => {
  let maxX = 0
  for (let t = 0; t < 60; t += 0.05) {
    const [x, y] = context.idleGaze(t)
    assert.ok(Math.abs(x) < 4 && Math.abs(y) < 2, `gaze stays inside the eye area at t=${t}`)
    maxX = Math.max(maxX, Math.abs(x))
  }
  assert.ok(maxX > 1.5, 'at least one glance aside within a minute')
})

test('facePhase: bots get their own rhythm, stable per name', () => {
  assert.equal(context.facePhase('research'), context.facePhase('research'))
  assert.notEqual(context.facePhase('research'), context.facePhase('ops'))
  assert.ok(context.facePhase('') >= 0)
})
