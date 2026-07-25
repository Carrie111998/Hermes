const REASONING_LABELS: Record<string, string> = {
  none: 'Off',
  minimal: 'Min',
  low: 'Low',
  medium: 'Med',
  high: 'High',
  xhigh: 'Max'
}

export function reasoningEffortLabel(effort: string): string {
  const key = effort.trim().toLowerCase()

  if (!key) {
    return ''
  }

  return REASONING_LABELS[key] ?? effort
}

/** Which model/provider a picker should mark "current". With a live session the
 *  gateway's `model.options` is authoritative; pre-session there is no server
 *  "current", so the sticky composer pick wins over the profile default the
 *  global options query returns — else the checkmark snaps back to the default
 *  and the pick looks ignored. */
export function currentPickerSelection(
  hasSession: boolean,
  store: { model: string; provider: string },
  options?: { model?: string; provider?: string }
): { model: string; provider: string } {
  return {
    model: String((hasSession && options?.model) || store.model || options?.model || ''),
    provider: String((hasSession && options?.provider) || store.provider || options?.provider || '')
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

// Chinese-friendly display names for common model families.
// Keyed by lowercased model-id prefix (after provider slash stripped).
const MODEL_DISPLAY_NAMES: Record<string, string> = {
  'qwen': '通义千问',
  'qwen2': '通义千问',
  'qwen3': '通义千问',
  'qwq': '通义千问 QwQ',
  'deepseek': 'DeepSeek',
  'glm-4': '智谱 GLM-4',
  'glm-4v': '智谱 GLM-4V',
  'glm-5': '智谱 GLM-5',
  'glm-5-turbo': '智谱 GLM-5 Turbo',
  'glm-5.1': '智谱 GLM-5.1',
  'glm': '智谱 GLM',
  'ernie': '文心一言',
  'moonshot': '月之暗面 Kimi',
  'kimi': '月之暗面 Kimi',
  'yi-': '零一万物 Yi',
  'baichuan': '百川',
  'spark': '讯飞星火',
  'hunyuan': '腾讯混元',
  'doubao': '字节豆包',
  'abab': 'MiniMax',
  'command': 'Cohere Command',
  'llama': 'Llama',
  'mistral': 'Mistral',
  'mixtral': 'Mixtral'
}

// Chinese-friendly provider names (maps backend slug/name → display name).
// Keyed by EXACT slug match first, then falls back to prefix/contains matching.
// Multi-entry providers (e.g. minimax / minimax-oauth / minimax-cn) use
// distinguishable suffixes so users don't see 3 identical "MiniMax" entries.
const PROVIDER_SLUG_MAP: Record<string, string> = {
  'openai': 'OpenAI',
  'openai-api': 'OpenAI API',
  'openai-codex': 'OpenAI Codex',
  'anthropic': 'Anthropic',
  'gemini': '谷歌 Gemini',
  'google': '谷歌',
  'deepseek': 'DeepSeek',
  'alibaba': '通义千问',
  'qwen': '通义千问',
  'qwen-oauth': '通义千问 (OAuth)',
  'zhipu': '智谱',
  'glm': '智谱',
  'zai': '智谱 Z.AI',
  'moonshot': '月之暗面',
  'kimi': '月之暗面',
  'kimi-coding': '月之暗面 (编码计划)',
  'kimi-coding-cn': '月之暗面 (国内)',
  'baichuan': '百川',
  'spark': '讯飞星火',
  'hunyuan': '腾讯混元',
  'tencent-tokenhub': '腾讯混元 (TokenHub)',
  'doubao': '字节豆包',
  'minimax': 'MiniMax',
  'minimax-oauth': 'MiniMax (OAuth)',
  'minimax-cn': 'MiniMax (国内)',
  'mistral': 'Mistral',
  'cohere': 'Cohere',
  'meta': 'Meta',
  'llama': 'Meta Llama',
  'nous': 'Nous Research',
  'openrouter': 'OpenRouter',
  'novita': 'NovitaAI',
  'lmstudio': 'LM Studio',
  'groq': 'Groq',
  'together': 'Together AI',
  'fireworks': 'Fireworks AI',
  'perplexity': 'Perplexity',
  'xai': 'xAI',
  'xai-oauth': 'xAI Grok (OAuth)',
  'xiaomi': '小米 MiMo',
  'nvidia': 'NVIDIA NIM',
  'copilot': 'GitHub Copilot',
  'copilot-acp': 'GitHub Copilot ACP',
  'huggingface': 'Hugging Face',
  'bedrock': 'AWS Bedrock',
  'azure-foundry': 'Azure Foundry',
  'stepfun': '阶跃星辰 StepFun',
  'arcee': 'Arcee AI',
  'gmi': 'GMI Cloud',
  'kilocode': 'Kilo Code',
  'opencode-zen': 'OpenCode Zen',
  'opencode-go': 'OpenCode Go',
  'ollama-cloud': 'Ollama Cloud',
  'ollama': 'Ollama',
  'custom': '自定义端点',
  'local': '本地',
  'provider': '提供方'
}

// Fallback: if no exact slug match, try fuzzy prefix matching against the
// BASE provider names (without suffix variants).
const PROVIDER_PREFIX_MAP: Record<string, string> = {
  'qwen': '通义千问',
  'zhipu': '智谱',
  'glm': '智谱',
  'moonshot': '月之暗面',
  'baichuan': '百川',
  'spark': '讯飞星火',
  'hunyuan': '腾讯混元',
  'doubao': '字节豆包'
}

export function providerDisplayName(slugOrName: string): string {
  const lower = (slugOrName || '').trim().toLowerCase()

  if (PROVIDER_SLUG_MAP[lower]) {
    return PROVIDER_SLUG_MAP[lower]
  }

  for (const key of Object.keys(PROVIDER_PREFIX_MAP)) {
    if (lower.includes(key)) {
      return PROVIDER_PREFIX_MAP[key]
    }
  }

  return slugOrName
}

function lookupDisplayName(base: string): string | null {
  const lower = base.toLowerCase()

  // Try exact match first, then prefix match
  if (MODEL_DISPLAY_NAMES[lower]) {
    return MODEL_DISPLAY_NAMES[lower]
  }

  for (const prefix of Object.keys(MODEL_DISPLAY_NAMES)) {
    if (lower.startsWith(prefix)) {
      return MODEL_DISPLAY_NAMES[prefix]
    }
  }

  return null
}

function prettifyBase(base: string): string {
  // Check Chinese display name table first
  const displayName = lookupDisplayName(base)

  if (displayName) {
    // Append version/variant suffix if present (e.g. "qwen-2.5-72b" → "通义千问 2.5 72B")
    const lower = base.toLowerCase()
    const matchedPrefix = Object.keys(MODEL_DISPLAY_NAMES).find(p => lower.startsWith(p))

    if (matchedPrefix) {
      const suffix = base.slice(matchedPrefix.length).replace(/^-/, '').replace(/-/g, ' ').trim()

      if (suffix) {
        const upper = suffix.replace(/\b(\d+(?:\.\d+)?)b\b/i, '$1B')
          .replace(/\b(\d+(?:\.\d+)?)k\b/i, '$1K')

        return `${displayName} ${upper}`
      }
    }

    return displayName
  }

  if (/^claude-/i.test(base)) {
    return titleCase(base.replace(/^claude-/i, '').replace(/-/g, ' '))
  }

  if (/^gpt-/i.test(base)) {
    return base.replace(/^gpt-/i, 'GPT-')
  }

  if (/^gemini-/i.test(base)) {
    return base.replace(/^gemini-/i, 'Gemini ').replace(/-/g, ' ')
  }

  return titleCase(base.replace(/-/g, ' '))
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

/** Status bar trigger label — model name plus the live session state (effort/fast). */
export function formatModelStatusLabel(
  model: string,
  options?: { fastMode?: boolean; reasoningEffort?: string }
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

  // Always surface the effort (empty = Hermes default of medium) so the
  // current reasoning level is visible at a glance, not just when non-default.
  parts.push(reasoningEffortLabel(options?.reasoningEffort ?? '') || 'Med')

  return `${name} · ${parts.join(' ')}`
}
