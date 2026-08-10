import { beforeEach, describe, expect, it } from 'vitest'

import {
  $workspaceContextBindings,
  projectSlackChannelIds,
  setProjectSlackChannelIds
} from './workspace-context'

describe('workspace context bindings', () => {
  beforeEach(() => {
    $workspaceContextBindings.set({})
  })

  it('normalizes and isolates Slack channel IDs per project', () => {
    setProjectSlackChannelIds('project-a', [' c123abc ', 'G456DEF', 'C123ABC'])
    setProjectSlackChannelIds('project-b', ['C999XYZ'])

    expect(projectSlackChannelIds('project-a')).toEqual(['C123ABC', 'G456DEF'])
    expect(projectSlackChannelIds('project-b')).toEqual(['C999XYZ'])
  })

  it('rejects DM IDs and removes a binding when no channels remain', () => {
    expect(() => setProjectSlackChannelIds('project-a', ['D123ABC'])).toThrow(/channel ID/)

    setProjectSlackChannelIds('project-a', ['C123ABC'])
    setProjectSlackChannelIds('project-a', [])

    expect(projectSlackChannelIds('project-a')).toEqual([])
  })
})
