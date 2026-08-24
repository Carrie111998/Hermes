import { describe, expect, it } from 'vitest'

import type { CustomEndpoint } from '@/types/hermes'

import { endpointModels, formFromEndpoint, toPayload } from './custom-endpoints-settings'

const ENDPOINT: CustomEndpoint = {
  api_mode: 'codex_responses',
  base_url: 'https://responses.example/v1',
  discover_models: true,
  has_api_key: false,
  id: 'responses',
  model: 'gpt-5.6-sol',
  models: ['gpt-5.6-sol-high'],
  model_details: [
    {
      id: 'gpt-5.6-sol-high',
      canonical_model: 'gpt-5.6-sol',
      reasoning_effort: 'high'
    }
  ],
  name: 'Responses'
}

describe('custom endpoint transport and model metadata', () => {
  it('round-trips an endpoint transport through the edit form and payload', () => {
    const form = formFromEndpoint(ENDPOINT)

    expect(form.apiMode).toBe('codex_responses')
    expect(toPayload(form).api_mode).toBe('codex_responses')
  })

  it('sends string ids for old backends and additive metadata for new backends', () => {
    const models = endpointModels(ENDPOINT)
    const payload = toPayload(formFromEndpoint(ENDPOINT), models)

    expect(payload.models).toEqual(['gpt-5.6-sol-high'])
    expect(payload.model_details).toEqual(ENDPOINT.model_details)
  })

  it('sends an explicit empty catalogue after an empty discovery', () => {
    const payload = toPayload(formFromEndpoint(ENDPOINT), [])

    expect(payload.models).toEqual([])
    expect(payload.model_details).toEqual([])
  })

  it('falls back to Chat Completions and string catalogs from older backends', () => {
    const legacy = { ...ENDPOINT, api_mode: undefined, model_details: undefined }

    expect(formFromEndpoint(legacy).apiMode).toBe('chat_completions')
    expect(endpointModels(legacy)).toEqual([{ id: 'gpt-5.6-sol-high' }])
  })
})
