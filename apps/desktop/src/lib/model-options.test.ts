import { beforeEach, describe, expect, it } from 'vitest'

import type { ModelOptionsResponse } from '@/types/hermes'

import { modelOptionsQueryKey, readModelOptionsCache, writeModelOptionsCache } from './model-options'

const catalog = (model: string): ModelOptionsResponse => ({
  model,
  provider: 'novacode',
  providers: [{ name: 'NovaCode', slug: 'novacode', models: [model] }]
})

describe('model options local cache', () => {
  beforeEach(() => localStorage.clear())

  it('uses stable profile/session keys and restores a successful catalog', () => {
    writeModelOptionsCache('default', 'session-1', catalog('gpt-5.6-sol'))

    const cached = readModelOptionsCache('default', 'session-1')

    expect(modelOptionsQueryKey('default', 'session-1')).toEqual(['model-options', 'default', 'session-1'])
    expect(cached?.data).toEqual(catalog('gpt-5.6-sol'))
    expect(cached?.updatedAt).toEqual(expect.any(Number))
  })

  it('keeps the cache bounded across session-scoped catalogs', () => {
    for (let index = 0; index < 20; index += 1) {
      writeModelOptionsCache('default', `session-${index}`, catalog(`model-${index}`))
    }

    const raw = localStorage.getItem('hermes.desktop.model-options.v1')
    expect(raw ? Object.keys(JSON.parse(raw)).length : 0).toBe(12)
  })

  it('treats malformed or incomplete entries as a cache miss', () => {
    localStorage.setItem(
      'hermes.desktop.model-options.v1',
      JSON.stringify({ 'default:global': { data: { providers: 'not-an-array' }, updatedAt: 1 } })
    )

    expect(readModelOptionsCache('default')).toBeUndefined()
  })
})
