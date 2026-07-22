import { describe, expect, it } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { sessionMatchesSearch } from './session-search'

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    archived: false,
    cwd: '/home/user/projects/hermes-agent',
    ended_at: null,
    id: '20260603_090200_abcd12',
    input_tokens: 0,
    is_active: false,
    last_active: 1_000,
    message_count: 2,
    model: 'claude',
    output_tokens: 0,
    preview: 'Fix Desktop session search',
    source: 'cli',
    started_at: 1_000,
    title: 'Desktop Search Feature',
    tool_call_count: 0,
    ...overrides
  }
}

describe('sessionMatchesSearch', () => {
  it('matches loaded sessions by full and partial session id', () => {
    const session = makeSession()

    expect(sessionMatchesSearch(session, '20260603_090200_abcd12')).toBe(true)
    expect(sessionMatchesSearch(session, '090200')).toBe(true)
    expect(sessionMatchesSearch(session, 'ABCD12')).toBe(true)
  })

  it('matches projected compression sessions by lineage root id', () => {
    const session = makeSession({
      _lineage_root_id: '20260602_235959_root99',
      id: '20260603_010000_tip01'
    })

    expect(sessionMatchesSearch(session, 'root99')).toBe(true)
    expect(sessionMatchesSearch(session, '20260602')).toBe(true)
  })

  it('preserves title, preview, and workspace matching', () => {
    const session = makeSession()

    expect(sessionMatchesSearch(session, 'desktop search')).toBe(true)
    expect(sessionMatchesSearch(session, 'session search')).toBe(true)
    expect(sessionMatchesSearch(session, 'hermes-agent')).toBe(true)
  })

  it('matches sessions by source platform and aliases', () => {
    expect(sessionMatchesSearch(makeSession({ source: 'telegram' }), 'Telegram')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'whatsapp' }), 'WhatsApp')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'whatsapp' }), 'wa')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'slack' }), 'slack')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'bluebubbles' }), 'imessage')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'claude' }), 'anthropic')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ source: 'claude' }), 'claude code')).toBe(true)
  })

  it('matches unified sessions by bridge provider name', () => {
    expect(sessionMatchesSearch(makeSession({ bridge_provider: 'claude' }), 'Claude')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ bridge_provider: 'codex' }), 'Codex')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ bridge_provider: 'hermes' }), 'Hermes')).toBe(true)
  })

  it('matches unified sessions by human mirror-state label', () => {
    expect(sessionMatchesSearch(makeSession({ bridge_mirror_state: 'catalog_only' }), 'catalog only')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ bridge_mirror_state: 'continued' }), 'continued')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ bridge_mirror_state: 'diverged' }), 'diverged')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ bridge_mirror_state: 'failed' }), '失敗')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ bridge_mirror_state: 'queued' }), '已排队')).toBe(true)
    expect(sessionMatchesSearch(makeSession({ bridge_mirror_state: 'catalog_only' }), '僅目錄')).toBe(true)
  })

  it.each([
    ['pending', 'pending'],
    ['visible', 'visible'],
    ['retrying', 'retrying'],
    ['failed', 'failed']
  ] as const)('matches %s Codex sidebar delivery status', (state, query) => {
    const session = makeSession({ bridge_sidebar_state: state })

    expect(sessionMatchesSearch(session, query)).toBe(true)
    expect(sessionMatchesSearch(session, 'Codex sidebar')).toBe(true)
  })

  it('does not match unrelated queries', () => {
    expect(sessionMatchesSearch(makeSession(), 'totally-unrelated')).toBe(false)
  })
})
