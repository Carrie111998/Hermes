import { beforeEach, describe, expect, it } from 'vitest'

import {
  BUILTIN_SNIPPET_KEYS,
  DEFAULT_SNIPPETS,
  loadSnippets,
  PROMPT_SNIPPETS_STORAGE_KEY,
  saveSnippets,
  seedSnippets
} from './prompt-snippets'

describe('prompt snippets store', () => {
  beforeEach(() => {
    window.localStorage.clear()
  })

  it('seeds the original 3 built-in prompts when storage is empty', () => {
    expect(loadSnippets()).toHaveLength(3)
    expect(loadSnippets().map(item => item.id)).toEqual([...BUILTIN_SNIPPET_KEYS])
    expect(loadSnippets()).toEqual(DEFAULT_SNIPPETS)
  })

  it('uses localized copy for the first-run seed', () => {
    const seeded = seedSnippets({
      codeReview: { label: '代码审查', description: '查回归', text: '请审查。' },
      implementationPlan: { label: '实现计划', description: '先计划', text: '请先做计划。' },
      explainThis: { label: '解释这段', description: '讲清楚', text: '请解释。' }
    })

    expect(seeded.map(item => item.label)).toEqual(['代码审查', '实现计划', '解释这段'])
  })

  it('persists a user edit and round-trips it', () => {
    const [first, ...rest] = loadSnippets()
    saveSnippets([{ ...first, label: 'Custom name', text: 'hello' }, ...rest])

    const stored = window.localStorage.getItem(PROMPT_SNIPPETS_STORAGE_KEY)

    expect(stored).toContain('Custom name')
    expect(loadSnippets()[0]).toMatchObject({ id: first.id, label: 'Custom name', text: 'hello' })
  })

  it('keeps an empty list after the user deletes everything', () => {
    saveSnippets([])
    expect(loadSnippets()).toEqual([])
  })

  it('falls back to the seed when storage is corrupt', () => {
    window.localStorage.setItem(PROMPT_SNIPPETS_STORAGE_KEY, '{not json')
    expect(loadSnippets()).toHaveLength(3)
  })
})
