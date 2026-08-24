import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const pluginSource = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function load({ blobatarSvg, Blobatar } = {}) {
  const values = new Map()
  const atom = initial => {
    const slot = { get: () => values.get(slot), set: value => values.set(slot, value) }
    values.set(slot, initial)
    return slot
  }
  const sdkStub = new Proxy(
    { blobatarSvg, Blobatar },
    { get: (target, key) => (key in target ? target[key] : undefined) }
  )
  const context = {
    atom,
    PALETTE_AREA: 'palette',
    COMPOSER_AREAS: { middleware: 'middleware' },
    document: { getElementById: () => null, createElement: () => ({}), head: { appendChild: () => undefined } },
    // The harness strips the react imports, so the JSX factories are the ones
    // this context provides: a plain node the assertions can read back.
    jsx: (type, props) => ({ type, props }),
    jsxs: (type, props) => ({ type, props }),
    host: { state: { profile: { listen: () => undefined }, gateway: { listen: () => undefined } } },
    sdk: sdkStub
  }
  const source = pluginSource
    // Bare side-effect imports (blobatar/motion.css) carry no binding the
    // sandbox needs, and vm has no module loader to resolve them.
    .replace(/^import '[^']*'\r?\n/gm, '')
    .replace(/^import\s+\*\s+as\s+sdk\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^import\s+\{[\s\S]*?\}\s+from '@hermes\/plugin-sdk'\r?\n/m, '')
    .replace(/^const \{ McpTab, ToolsetConfigPanel \} = sdk\r?\n/m, '')
    .replace(/^import .* from 'react'\r?\n/m, '')
    .replace(/^import .* from 'react\/jsx-runtime'\r?\n/m, '')
    .replace('export default {', 'globalThis.plugin = {')
    .concat(`
globalThis.__blob = { isBlobShape, parseBlobShape, blobShapeString, blobMarkup, blobOpts, blobPx, BotFace, BLOB_KINDS, BLOB_KIND_TRAIT, BLOB_OVERDRAW };
`)
  vm.runInNewContext(source, context, { filename: 'plugin.js' })
  return context.__blob
}

test('blob shape strings round-trip through parse/build', () => {
  const b = load()

  assert.equal(b.isBlobShape('blobatar'), true)
  assert.equal(b.isBlobShape('blobatar:seed123'), true)
  assert.equal(b.isBlobShape('blobatar::sun'), true)
  assert.equal(b.isBlobShape('circle'), false)
  assert.equal(b.isBlobShape(undefined), false)

  // Unlocked: seed follows the name.
  assert.equal(JSON.stringify(b.parseBlobShape('blobatar', 'inbox-triage')), JSON.stringify({ seed: 'inbox-triage', seedPart: '', kind: '' }))
  // Locked seed.
  assert.equal(JSON.stringify(b.parseBlobShape('blobatar:abc123', 'inbox-triage')), JSON.stringify({ seed: 'abc123', seedPart: 'abc123', kind: '' }))
  // Pinned silhouette, unlocked seed.
  assert.equal(JSON.stringify(b.parseBlobShape('blobatar::cloud', 'inbox-triage')), JSON.stringify({ seed: 'inbox-triage', seedPart: '', kind: 'cloud' }))
  // Unknown silhouette is ignored, never trusted.
  assert.equal(b.parseBlobShape('blobatar:abc:mystery', 'x').kind, '')

  assert.equal(b.blobShapeString('', ''), 'blobatar')
  assert.equal(b.blobShapeString('abc', ''), 'blobatar:abc')
  assert.equal(b.blobShapeString('abc', 'sun'), 'blobatar:abc:sun')
  assert.equal(b.blobShapeString('', 'sun'), 'blobatar::sun')
})

test('every silhouette has a trait position inside its frozen band', () => {
  const b = load()
  const bands = {
    round: [0, 0.22], organic: [0.22, 0.48], boxy: [0.48, 0.60], capsule: [0.60, 0.70],
    nub: [0.70, 0.79], cloud: [0.79, 0.86], droplet: [0.86, 0.915], hexagon: [0.915, 0.95],
    sun: [0.95, 0.98], triangle: [0.98, 1]
  }

  for (const kind of b.BLOB_KINDS) {
    const v = b.BLOB_KIND_TRAIT[kind]
    assert.equal(typeof v, 'number', kind)
    assert.ok(v >= bands[kind][0] && v < bands[kind][1], `${kind} trait ${v} outside band`)
  }
})

test('blobMarkup renders via the SDK export, tags data-bot-face, pins traits', () => {
  const calls = []
  const b = load({
    blobatarSvg: (seed, opts) => {
      calls.push({ seed, opts })
      return '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100"></svg>'
    }
  })

  const markup = b.blobMarkup('blobatar', 'inbox-triage', 56)
  assert.ok(markup.startsWith('<svg data-bot-face="inbox-triage" '), 'roster PNG backfill needs the data-bot-face tag')
  assert.equal(calls[0].seed, 'inbox-triage')
  assert.equal(calls[0].opts.size, 56)
  assert.equal('traits' in calls[0].opts, false)

  b.blobMarkup('blobatar:abc:sun', 'inbox-triage', 32)
  assert.equal(calls[1].seed, 'abc')
  assert.equal(calls[1].opts.traits.shape, b.BLOB_KIND_TRAIT.sun)
})

test('blobMarkup degrades to null when the SDK lacks the export or the renderer throws', () => {
  const withoutSdk = load()
  assert.equal(withoutSdk.blobMarkup('blobatar', 'x', 32), null)

  const throwing = load({
    blobatarSvg: () => {
      throw new Error('boom')
    }
  })
  assert.equal(throwing.blobMarkup('blobatar', 'x', 32), null)
})

// The library's core body spans ~62-76 of its 100-unit frame while the legacy
// math face fills its own viewBox, so a blobatar drawn at the avatar box size
// reads visibly smaller than the faces it sits next to in the roster.
test('blob faces overdraw their box so they match the legacy faces visual weight', () => {
  const b = load({ Blobatar: () => null })

  assert.ok(b.BLOB_OVERDRAW > 1, 'a blobatar drawn at box size reads too small')
  assert.equal(b.blobPx(34), Math.round(34 * b.BLOB_OVERDRAW))

  const node = b.BotFace({ shape: 'blobatar', color: '#888', size: 34, name: 'inbox-triage' })

  // The layout box is untouched — every row keeps its slot; only the drawing
  // is bigger, and the frame's empty margin falls outside.
  assert.equal(node.props.style.width, 34)
  assert.equal(node.props.style.height, 34)
  assert.equal(node.props.children.props.size, b.blobPx(34))
  assert.equal(node.props.style.overflow, 'visible', 'the hover lift and the overdraw both reach past the box')

  // Absolute centering, not `place-items: center`: Chrome aligns an
  // overflowing child to `start` instead of centering it, which parked every
  // overdrawn blob down-and-right of its slot, crowding the name beside it.
  assert.equal(node.props.style.position, 'relative')
  const face = node.props.children.props.style
  assert.equal(face.position, 'absolute')
  assert.equal(face.left, '50%')
  assert.equal(face.top, '50%')
  assert.equal(face.transform, 'translate(-50%, -50%)')
  assert.equal(node.props.style.display, 'block', 'width/height do not apply to an inline span')

  // The group row's ringed stack opts out: there the frame hugs the face, so a
  // creature drawn past its box ends up under the next member's ring.
  const tight = b.BotFace({ shape: 'blobatar', color: '#888', size: 20, name: 'inbox-triage', overdraw: false })
  assert.equal(tight.props.children.props.size, 20)
})

test('blob faces default to hover and never pose — the typing dots carry mid-turn', () => {
  const b = load({ Blobatar: () => null })

  const face = b.BotFace({ shape: 'blobatar::sun', color: '#888', size: 34, name: 'inbox-triage' }).props.children

  // 'hover' by default — one creature alive at a time, the library's own
  // recommendation, and the right call at the sizes most call sites draw at.
  assert.equal(face.props.animate, 'hover')
  assert.equal(face.props.name, 'inbox-triage')
  assert.equal(face.props.traits.shape, b.BLOB_KIND_TRAIT.sun)
  // The roster PNG backfill finds faces by this attribute.
  assert.equal(face.props['data-bot-face'], 'inbox-triage')

  // A working bot draws the same idle creature: an expression under the
  // typing dots is two motions competing for the same beat.
  const working = b.BotFace({ shape: 'blobatar', color: '#888', size: 34, name: 'inbox-triage', mood: 'work' })
  assert.equal(working.props.children.props.expression, undefined)
})

test('blob faces fall back to static markup, then to a math face, on older SDKs', () => {
  // Component missing (older SDK): the string renderer still draws a face.
  const stringOnly = load({ blobatarSvg: () => '<svg xmlns="http://www.w3.org/2000/svg"></svg>' })
  const still = stringOnly.BotFace({ shape: 'blobatar', color: '#888', size: 34, name: 'inbox-triage' })
  assert.ok(still.props.dangerouslySetInnerHTML.__html.startsWith('<svg data-bot-face="inbox-triage"'))

  // Neither export: a stored blobatar pick renders as a legacy math face.
  const neither = load()
  const legacy = neither.BotFace({ shape: 'blobatar', color: '#888', size: 34, name: 'inbox-triage' })
  assert.equal(legacy.props['data-hb-math'], '1')
})

// A pinned silhouette must reach the animated component too — it used to be
// unpacked inside blobMarkup, which the component path never calls.
test('blobOpts unpacks the seed and the pinned silhouette', () => {
  const b = load()

  assert.equal(b.blobOpts('blobatar', 'inbox-triage').seed, 'inbox-triage')
  assert.equal(b.blobOpts('blobatar', 'inbox-triage').traits, undefined)
  assert.equal(b.blobOpts('blobatar:abc:cloud', 'inbox-triage').seed, 'abc')
  assert.equal(b.blobOpts('blobatar:abc:cloud', 'inbox-triage').traits.shape, b.BLOB_KIND_TRAIT.cloud)
})

// Unread used to be a dot on the title line, between the name and the age,
// where it read as punctuation rather than as a mark on the bot. It now rides
// the face as a corner badge.
test('the unread badge sits on the avatar, not on the title line', () => {
  const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

  // The avatar slot is the badge's positioning context; upstream's
  // source-status graying shares the wrapper so marks dim with the face.
  assert.match(
    source,
    /className: cn\('relative shrink-0', !sourceStatus\.available && 'grayscale opacity-60'\),\s*\n\s*children: \[\s*\n\s*jsx\(BotFace, \{/
  )
  // Top-right corner, ringed against the roster's own (opaque) surface — the
  // `--ui-bg-*` fills are translucent washes and let the creature through.
  assert.match(source, /'absolute -right-0\.5 -top-0\.5 size-2 rounded-full bg-\(--ui-accent,#4f9cf9\) ' \+\s*\n\s*'ring-2 ring-\(--ui-sidebar-surface-background,#111\)'/)
  // And no unread dot left inline on the title line.
  assert.doesNotMatch(source, /unread\s*\n\s*\? jsx\('span', \{\s*\n\s*className: 'size-2 shrink-0 rounded-full/)
})

// Mid-turn used to be one dot on the title line, fading in and out next to the
// age. It is now the composer's typing idiom, under the face.
test('a working bot shows three typing dots under its face', () => {
  const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

  // Three dots, one clock, staggered by delay.
  assert.match(source, /@keyframes hermes-bots-typing/)
  // The stylesheet is rewritten on every evaluation — guarding on the tag's
  // existence left edited rules uninstalled until a full app restart.
  assert.doesNotMatch(source, /!document\.getElementById\('hermes-bots-roster-css'\)/)
  assert.match(source, /\.hermes-bots-typing > span \{ animation: hermes-bots-typing/)
  assert.match(source, /nth-child\(2\) \{ animation-delay: 0\.16s/)
  assert.match(source, /nth-child\(3\) \{ animation-delay: 0\.32s/)
  // Bottom-centre of the avatar slot, opaque so the face's ink does not read
  // as part of the indicator.
  assert.match(source, /'hermes-bots-typing absolute -bottom-1 left-1\/2 flex -translate-x-1\/2 items-center gap-0\.5'/)
  // Exactly three.
  assert.equal(source.match(/'size-\[3px\] rounded-full bg-\(--ui-accent,#4f9cf9\)'/g).length, 3)
  // The old blinking dot and its now-dead keyframes are gone.
  assert.doesNotMatch(source, /hermes-bots-pulse/)
})

// Ambient motion belongs to the faces big enough to read as an identity: the
// roster rows and the create/edit preview. Everything else — picker swatches,
// the Active Now strip, group member stacks, list rows — is drawn small
// enough that constant motion is noise, so those animate on hover.
test('only the roster rows and the create/edit preview animate always', () => {
  const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')
  const always = [...source.matchAll(/animate: 'always'/g)]

  assert.equal(always.length, 3, 'exactly three call sites opt into ambient motion')

  const at = index => {
    const start = source.lastIndexOf('function ', index)
    return source.slice(start, source.indexOf('(', start))
  }
  const owners = always.map(m => at(m.index)).sort()

  assert.deepEqual(owners, ['function BotRow', 'function CreateAgentDialog', 'function EditProfileDialog'])
})

test('every other BotFace call site inherits the hover default', () => {
  const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')
  const calls = [...source.matchAll(/jsx\(BotFace, \{/g)]

  assert.ok(calls.length >= 10, 'sanity: the roster is not the only face in the plugin')
  // No call site may pass 'hover' explicitly — the default carries it, so a
  // new call site cannot forget and land on ambient motion by accident.
  assert.doesNotMatch(source, /animate: 'hover'/)
})
