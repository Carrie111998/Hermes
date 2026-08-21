/** User-editable prompt snippets for the Desktop composer.

Built-in list is a first-run seed — the original 3 composer snippets, using
the active UI language. After that the list lives in localStorage; add / edit
/ delete never touch i18n or source.

Scope: desktop-global (`hermes-desktop-prompt-snippets-v1`), same class as
user-themes.
*/
export interface PromptSnippet {
  id: string
  description: string
  label: string
  text: string
}

export type SnippetCopy = Pick<PromptSnippet, 'description' | 'label' | 'text'>

export const PROMPT_SNIPPETS_STORAGE_KEY = 'hermes-desktop-prompt-snippets-v1'

/** Same keys as the pre-CRUD `SNIPPET_KEYS` in context-menu.tsx. */
export const BUILTIN_SNIPPET_KEYS = ['codeReview', 'implementationPlan', 'explainThis'] as const

export type BuiltinSnippetKey = (typeof BUILTIN_SNIPPET_KEYS)[number]

/** English fallback matching `i18n/en.ts` composer.snippets — used by tests and SSR. */
export const DEFAULT_SNIPPETS: PromptSnippet[] = [
  {
    id: 'codeReview',
    label: 'Code review',
    description: 'Audit the current change for regressions, dropped edge cases, and missing tests.',
    text: 'Please review this for bugs, regressions, and missing tests.'
  },
  {
    id: 'implementationPlan',
    label: 'Implementation plan',
    description: 'Outline an approach before touching code so the diff stays focused.',
    text: 'Please make a concise implementation plan before changing code.'
  },
  {
    id: 'explainThis',
    label: 'Explain this',
    description: 'Walk through how the selected code works and link to the key files.',
    text: 'Please explain how this works and point me to the key files.'
  }
]

function isSnippet(value: unknown): value is PromptSnippet {
  if (!value || typeof value !== 'object') {
    return false
  }

  const item = value as Partial<PromptSnippet>

  return (
    typeof item.id === 'string' &&
    item.id.length > 0 &&
    typeof item.label === 'string' &&
    typeof item.description === 'string' &&
    typeof item.text === 'string'
  )
}

export function seedSnippets(copy?: Record<string, SnippetCopy>): PromptSnippet[] {
  return BUILTIN_SNIPPET_KEYS.map(id => {
    const localized = copy?.[id]
    const fallback = DEFAULT_SNIPPETS.find(item => item.id === id) ?? DEFAULT_SNIPPETS[0]

    return {
      id,
      label: localized?.label ?? fallback.label,
      description: localized?.description ?? fallback.description,
      text: localized?.text ?? fallback.text
    }
  })
}

/** Load the snippet list. Missing/corrupt storage falls back to the seed. */
export function loadSnippets(copy?: Record<string, SnippetCopy>): PromptSnippet[] {
  if (typeof window === 'undefined') {
    return seedSnippets(copy)
  }

  try {
    const raw = window.localStorage.getItem(PROMPT_SNIPPETS_STORAGE_KEY)

    if (!raw) {
      return seedSnippets(copy)
    }

    const parsed: unknown = JSON.parse(raw)

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return seedSnippets(copy)
    }

    const items = (parsed as { items?: unknown }).items

    if (!Array.isArray(items)) {
      return seedSnippets(copy)
    }

    return items.filter(isSnippet)
  } catch {
    return seedSnippets(copy)
  }
}

export function saveSnippets(items: PromptSnippet[]): void {
  if (typeof window === 'undefined') {
    return
  }

  try {
    window.localStorage.setItem(PROMPT_SNIPPETS_STORAGE_KEY, JSON.stringify({ version: 1, items }))
  } catch {
    // Restricted storage shouldn't break the composer.
  }
}

export function createSnippetId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return `custom-${crypto.randomUUID()}`
  }

  return `custom-${Date.now().toString(36)}`
}
