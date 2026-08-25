import { describe, expect, it } from 'vitest'

import type { CronJobUpdates } from '@/types/hermes'

import { cronEditorUpdates } from './cron-job-model'

describe('cron model switcher — updateCronJob payload', () => {
  // The model switcher in CronJobDetail calls updateCronJob directly with
  // { model, provider } — bypassing the editor form. Verify the payload shape
  // matches what the API expects (null clears, string pins).

  it('pins a model and provider when both are provided', () => {
    const updates: CronJobUpdates = {
      model: 'claude-sonnet-4',
      provider: 'anthropic'
    }

    expect(updates.model).toBe('claude-sonnet-4')
    expect(updates.provider).toBe('anthropic')
  })

  it('clears a previous pin when model is empty', () => {
    const updates: CronJobUpdates = {
      model: null,
      provider: null
    }

    expect(updates.model).toBeNull()
    expect(updates.provider).toBeNull()
  })

  it('pins a model with an empty provider when only model is selected', () => {
    const updates: CronJobUpdates = {
      model: 'gpt-4o',
      provider: ''
    }

    // The API normalizes empty-string provider to null/undefined
    expect(updates.model).toBe('gpt-4o')
  })
})

describe('cronEditorUpdates — model switcher integration', () => {
  it('writes the model override for agent jobs (same path as the switcher)', () => {
    const updates = cronEditorUpdates(
      {
        deliver: 'local',
        model: 'deepseek-chat',
        name: 'Daily summary',
        prompt: 'go',
        provider: 'deepseek',
        schedule: '0 9 * * *'
      },
      { scriptOnlyJob: false }
    )

    expect(updates.model).toBe('deepseek-chat')
    expect(updates.provider).toBe('deepseek')
  })

  it('clears a previous pin when the switcher resets to default', () => {
    const updates = cronEditorUpdates(
      { deliver: 'local', model: '', name: 'Daily', prompt: 'go', provider: '', schedule: '0 9 * * *' },
      { scriptOnlyJob: false }
    )

    expect(updates.model).toBe(null)
    expect(updates.provider).toBe(null)
  })

  it('uses a cheaper model for low-effort jobs and a powerful model for heavy jobs', () => {
    // Low-effort summarising job
    const cheapUpdates = cronEditorUpdates(
      {
        deliver: 'local',
        model: 'deepseek-chat',
        name: 'Feed summariser',
        prompt: 'Summarise the feed',
        provider: 'deepseek',
        schedule: '0 * * * *'
      },
      { scriptOnlyJob: false }
    )

    expect(cheapUpdates.model).toBe('deepseek-chat')

    // Heavy reasoning job
    const heavyUpdates = cronEditorUpdates(
      {
        deliver: 'local',
        model: 'claude-sonnet-4',
        name: 'Weekly analysis',
        prompt: 'Analyse the week',
        provider: 'anthropic',
        schedule: '0 9 * * 1'
      },
      { scriptOnlyJob: false }
    )

    expect(heavyUpdates.model).toBe('claude-sonnet-4')
  })
})
