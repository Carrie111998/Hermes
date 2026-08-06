import { describe, expect, it } from 'vitest'

import { buildWorkspaceContextResearchPrompt } from './workspace-context'

describe('buildWorkspaceContextResearchPrompt', () => {
  it('binds the search to selected sources and requires verifiable citations', () => {
    const prompt = buildWorkspaceContextResearchPrompt({
      projectId: 'project-1',
      projectLabel: 'Launch',
      query: 'What did we decide about onboarding?',
      slackChannelIds: ['C123ABC'],
      sources: ['notion', 'slack']
    })

    expect(prompt).toContain('Project: Launch (project-1)')
    expect(prompt).toContain('Notion and Slack')
    expect(prompt).toContain('source URL or Slack permalink')
    expect(prompt).toContain('C123ABC')
    expect(prompt).toContain('Never search an unlisted channel, IM, or MPIM')
    expect(prompt).toContain('Never invent a citation')
    expect(prompt).toContain('Do not modify the repository')
    expect(prompt).toContain('Do not write to Notion or Slack')
    expect(prompt).toContain('Wait for the user')
    expect(prompt).toContain('What did we decide about onboarding?')
  })

  it('deduplicates sources and rejects an empty query', () => {
    const prompt = buildWorkspaceContextResearchPrompt({
      projectId: 'project-1',
      projectLabel: 'Launch',
      query: 'pricing',
      sources: ['notion', 'notion']
    })

    expect(prompt).toContain('Notion only')
    expect(prompt).not.toContain('Slack permalink for every finding')
    expect(() =>
      buildWorkspaceContextResearchPrompt({
        projectId: 'project-1',
        projectLabel: 'Launch',
        query: '   ',
        sources: ['notion']
      })
    ).toThrow('query')
  })

  it('requires at least one supported source', () => {
    expect(() =>
      buildWorkspaceContextResearchPrompt({
        projectId: 'project-1',
        projectLabel: 'Launch',
        query: 'pricing',
        sources: []
      })
    ).toThrow('source')
  })

  it('requires explicit project Slack channel bindings', () => {
    expect(() => buildWorkspaceContextResearchPrompt({
      projectId: 'project-1',
      projectLabel: 'Launch',
      query: 'pricing',
      sources: ['slack']
    })).toThrow('channel')
  })
})
