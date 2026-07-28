import { describe, expect, it } from 'vitest'

import { sessionIdentityKey } from '@/lib/session-identity'
import type { SessionInfo } from '@/types/hermes'

import { buildRailTasks } from './activity'

const session = (id: string, profile: string, title: string): SessionInfo => ({
  archived: false,
  cwd: null,
  ended_at: null,
  id,
  input_tokens: 0,
  is_active: true,
  last_active: 1,
  message_count: 1,
  model: null,
  output_tokens: 0,
  preview: null,
  profile,
  source: null,
  started_at: 1,
  title,
  tool_call_count: 0
})

describe('buildRailTasks', () => {
  it('resolves a working session by compound identity when profiles share its stored id', () => {
    const sessions = [session('shared', 'alpha', 'Alpha task'), session('shared', 'beta', 'Beta task')]

    const tasks = buildRailTasks([sessionIdentityKey('shared', 'beta')], sessions, null, {})

    expect(tasks).toEqual([
      expect.objectContaining({
        id: `session:${sessionIdentityKey('shared', 'beta')}`,
        label: 'Beta task'
      })
    ])
  })
})