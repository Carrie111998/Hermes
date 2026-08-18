import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'

// Bot Mode > New Agent > Advanced > General had no OpenRouter provider/
// endpoint routing picker, unlike Settings > Model and Profiles > New
// Profile. It must reuse the SAME reviewed SDK-exported components/helpers
// (OpenRouterRoutingField, OpenRouterModelInput, isOpenRouterProvider,
// openRouterRoutingDraft, updateOpenRouterRoutingConfig) rather than
// reimplementing routing UI — plugin.js is plain ESM and may only import
// from @hermes/plugin-sdk + react (the plugin fence, eslint.config.mjs).

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

test('CreateAgentDialog imports the OpenRouter routing surface from the SDK, not a duplicate implementation', () => {
  // Feature-detected the same way SkillsView/Streamdown already are: an
  // older desktop without these SDK exports gets `undefined`, not a crash.
  assert.match(source, /const OpenRouterRoutingField = typeof sdk === 'undefined' \? undefined : sdk\.OpenRouterRoutingField/)
  assert.match(source, /const OpenRouterModelInput = typeof sdk === 'undefined' \? undefined : sdk\.OpenRouterModelInput/)
  assert.match(source, /const isOpenRouterProvider = typeof sdk === 'undefined' \? undefined : sdk\.isOpenRouterProvider/)
  assert.match(source, /const openRouterRoutingDraft = typeof sdk === 'undefined' \? undefined : sdk\.openRouterRoutingDraft/)
  assert.match(
    source,
    /const updateOpenRouterRoutingConfig = typeof sdk === 'undefined' \? undefined : sdk\.updateOpenRouterRoutingConfig/
  )
  assert.match(source, /const getOpenRouterEndpoints = typeof sdk === 'undefined' \? undefined : sdk\.getOpenRouterEndpoints/)
  assert.match(source, /const getHermesConfigRecord = typeof sdk === 'undefined' \? undefined : sdk\.getHermesConfigRecord/)
  assert.match(source, /const saveHermesConfig = typeof sdk === 'undefined' \? undefined : sdk\.saveHermesConfig/)
})

function createAgentDialogSource() {
  const start = source.indexOf('function CreateAgentDialog(')
  const end = source.indexOf('\nfunction ', start + 1)

  return source.slice(start, end === -1 ? source.length : end)
}

test('CreateAgentDialog renders the OpenRouter routing field gated on provider + model + local target only', () => {
  const fn = createAgentDialogSource()

  // Tightly scoped to the ternary condition IMMEDIATELY preceding the
  // jsx(OpenRouterRoutingField call, not a substring match anywhere in the
  // function — the persistence block below also mentions
  // isOpenRouterProvider(provider) && model, so a loose match here proves
  // the string exists, not that the RENDER is actually gated by it.
  const callIndex = fn.indexOf('jsx(OpenRouterRoutingField,')
  assert.ok(callIndex !== -1, 'jsx(OpenRouterRoutingField, call not found')
  const gate = fn.slice(Math.max(0, callIndex - 300), callIndex)

  // The field must never render for a remote target: persistence is
  // local-target only (getHermesConfigRecord/saveHermesConfig hit the
  // ACTIVE gateway's backend, not the remote one), so rendering the control
  // for a remote create would let a user configure a lock that is silently
  // discarded on submit — a control that lies about what it does.
  assert.match(gate, /!remoteTarget/)
  assert.match(gate, /isOpenRouterProvider\(provider\)/)
  assert.match(gate, /&&\s*model/)
})

test('CreateAgentDialog opts the shared ModelPicker into the OpenRouter model typeahead', () => {
  const fn = createAgentDialogSource()

  assert.match(fn, /openRouterModel:\s*true/)
})

test('ModelPicker swaps in OpenRouterModelInput for the model field when the provider is OpenRouter', () => {
  const start = source.indexOf('function ModelPicker(')
  const end = source.indexOf('\n// ── advanced profile config', start)
  const fn = source.slice(start, end === -1 ? source.length : end)

  assert.match(fn, /openRouterModel/)
  assert.match(fn, /isOpenRouterProvider\(value\.provider\)/)
  assert.match(fn, /jsx\(OpenRouterModelInput,/)
})

test('CreateAgentDialog persists the routing override by re-fetching the authoritative config after creation, never a stale PUT', () => {
  const fn = createAgentDialogSource()

  // Must fetch AFTER ensureAgentCreated/profiles.create resolves for the new
  // slug, not from a config snapshot captured before creation (Issue B: a
  // whole-record PUT built from a stale pre-write snapshot can silently wipe
  // an unrelated field).
  const ensureBlock = fn.slice(fn.indexOf('const ensureAgentCreated ='), fn.indexOf('const submit = async'))
  assert.match(ensureBlock, /getHermesConfigRecord\(/)
  assert.match(ensureBlock, /updateOpenRouterRoutingConfig\(/)
  assert.match(ensureBlock, /saveHermesConfig\(/)
  // The fetch must happen after createdRef.current is set for the new slug
  // (i.e. after profiles.create/profiles.configure resolved), not before.
  const createdAt = ensureBlock.indexOf('createdRef.current = slug')
  const fetchAt = ensureBlock.indexOf('getHermesConfigRecord(')
  assert.ok(createdAt !== -1 && fetchAt !== -1 && fetchAt > createdAt)
})

test('CreateAgentDialog resets routing draft state alongside provider/model on dialog reset', () => {
  const fn = createAgentDialogSource()
  const resetBlock = fn.slice(fn.indexOf('const reset = () =>'), fn.indexOf('const reset = () =>') + 1500)

  assert.match(resetBlock, /setRoutingDraft\(/)
})
