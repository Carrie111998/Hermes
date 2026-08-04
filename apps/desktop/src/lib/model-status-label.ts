import { DEFAULT_REASONING_EFFORT, reasoningEffortLabel } from '@/lib/reasoning-effort'

/** Which model/provider pair a picker should mark "current". SessionView state
 *  also drives the composer label, so a complete pair there wins over an older
 *  `model.options` response. During initial hydration (or pre-session startup),
 *  options remain the fallback. Pick one complete pair before mixing fields so
 *  a model is never shown under a different provider. */
export function currentPickerSelection(
  store: { model: string; provider: string },
  options?: { model?: string; provider?: string }
): { model: string; provider: string } {
  const storeSelection = {
    model: String(store.model || ''),
    provider: String(store.provider || '')
  }

  const optionsSelection = {
    model: String(options?.model || ''),
    provider: String(options?.provider || '')
  }

  if (storeSelection.model && storeSelection.provider) {
    return storeSelection
  }

  if (optionsSelection.model && optionsSelection.provider) {
    return optionsSelection
  }

  return {
    model: storeSelection.model || optionsSelection.model,
    provider: storeSelection.provider || optionsSelection.provider
  }
}

/** Strip provider prefix and normalize for display. */
export function modelBaseId(model: string): string {
  const trimmed = model.trim()
  const slash = trimmed.lastIndexOf('/')

  return slash >= 0 ? trimmed.slice(slash + 1) : trimmed
}

// Trailing model-id variants that should render as a grayed tag beside the
// name (e.g. "Opus 4.8" + "Fast") rather than collapsing two distinct ids to
// the same display name.
const VARIANT_TAGS: ReadonlyArray<readonly [RegExp, string]> = [
  [/-fast$/i, 'Fast'],
  [/-thinking$/i, 'Thinking'],
  [/-preview$/i, 'Preview'],
  [/-latest$/i, 'Latest']
]

const titleCase = (text: string): string => text.replace(/\b\w/g, char => char.toUpperCase()).trim()

// Brand names whose canonical casing is not word-initial, so titleCasing alone
// would render "Deepseek" or "Glm". Applied to the finished fallback label;
// whole-word matches only, so version tokens are never touched.
const BRAND_CASING: Readonly<Record<string, string>> = {
  deepseek: 'DeepSeek',
  opencode: 'OpenCode',
  openrouter: 'OpenRouter',
  glm: 'GLM'
}

const applyBrandCasing = (text: string): string =>
  Object.entries(BRAND_CASING).reduce(
    (acc, [raw, canonical]) => acc.replace(new RegExp(`\\b${raw}\\b`, 'gi'), canonical),
    text
  )

// Split on dashes and capitalize lowercase letter-leading tokens (`sol` → `Sol`)
// while digit-leading tokens stay untouched (`4o`, `70b`). Joining with spaces
// matches the normalization the titleCase paths apply; empty tokens (leading,
// trailing, or doubled dashes) are dropped so malformed ids never leak spaces.
const smartTitle = (text: string): string =>
  text
    .split('-')
    .filter(Boolean)
    .map(token =>
      /^[a-z]/.test(token) && token === token.toLowerCase()
        ? token[0].toUpperCase() + token.slice(1)
        : token
    )
    .join(' ')

function prettifyBase(base: string): string {
  if (/^claude-/i.test(base)) {
    return titleCase(base.replace(/^claude-/i, '').replace(/-/g, ' '))
  }

  if (/^gpt-/i.test(base)) {
    const tail = base.replace(/^gpt-/i, '')

    // Dotted ids already carry the canonical version separator, so the
    // remaining dash tokens are variant words (`gpt-5.6-sol` → `GPT-5.6 Sol`).
    // Dash-form tails (`gpt-5-5`) stay untouched — restoring the dot is a
    // separate normalization for dash-separated ids.
    return tail.includes('.') ? `GPT-${smartTitle(tail)}` : `GPT-${tail}`
  }

  if (/^gemini-/i.test(base)) {
    return base.replace(/^gemini-/i, 'Gemini ').replace(/-/g, ' ')
  }

  return applyBrandCasing(smartTitle(base))
}

/** Split a model id into a clean display name plus an optional grayed variant
 *  tag, so distinct ids (e.g. `…-4.8` vs `…-4.8-fast`) don't collapse. */
export function modelDisplayParts(model: string): { name: string; tag: string } {
  let base = modelBaseId(model)
  let tag = ''

  for (const [pattern, label] of VARIANT_TAGS) {
    if (pattern.test(base)) {
      tag = label
      base = base.replace(pattern, '')

      break
    }
  }

  // Drop a trailing date-pin (`…-20251101`) — snapshot noise, not a name.
  base = base.replace(/-\d{8}$/, '')

  return { name: prettifyBase(base) || model.trim() || 'No model', tag }
}

/** Friendly one-line model name for menus and the status bar. */
export function displayModelName(model: string): string {
  return modelDisplayParts(model).name
}

/** Status bar trigger label — model name plus the live session state (effort/fast).
 *  `defaultEffort` is the profile's configured level, used when the surface has
 *  no explicit effort so the label never advertises a default the agent won't use. */
export function formatModelStatusLabel(
  model: string,
  options?: { defaultEffort?: string; fastMode?: boolean; reasoningEffort?: string }
): string {
  const name = displayModelName(model)

  if (!model.trim()) {
    return name
  }

  const parts: string[] = []

  // Fast is shown when the speed=fast param is on (options.fastMode) OR the
  // active model is a `…-fast` variant (fast via a separate model id).
  if (options?.fastMode || /-fast$/i.test(modelBaseId(model))) {
    parts.push('Fast')
  }

  // Always surface the effort so the current reasoning level is visible at a
  // glance, not just when non-default.
  parts.push(reasoningEffortLabel(options?.reasoningEffort || options?.defaultEffort || DEFAULT_REASONING_EFFORT))

  return `${name} · ${parts.join(' ')}`
}
