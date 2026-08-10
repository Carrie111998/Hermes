import { afterEach, describe, expect, it, vi } from 'vitest'

import { $connection } from '@/store/session'
import type { SessionInfo, SessionMessage } from '@/types/hermes'

import { artifactImageSrc, collectArtifactsForSession, loadArtifactsForSessions } from './artifact-utils'

function makeSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    ended_at: null,
    id: 'session-1',
    input_tokens: 0,
    is_active: false,
    last_active: 1000,
    message_count: 1,
    model: null,
    output_tokens: 0,
    preview: null,
    source: null,
    started_at: 1000,
    title: 'Session',
    tool_call_count: 0,
    ...overrides
  }
}

describe('collectArtifactsForSession', () => {
  afterEach(() => {
    vi.unstubAllGlobals()
    vi.clearAllMocks()
    $connection.set(null)
  })

  it('indexes plain https links from assistant text', () => {
    const artifacts = collectArtifactsForSession(makeSession(), [
      {
        content: 'Reference: https://example.com/docs/getting-started',
        role: 'assistant',
        timestamp: 2000
      }
    ])

    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({
      href: 'https://example.com/docs/getting-started',
      kind: 'link',
      value: 'https://example.com/docs/getting-started'
    })
  })

  it('indexes http links present in tool JSON payloads', () => {
    const messages: SessionMessage[] = [
      {
        content: JSON.stringify({ source_url: 'https://example.com/changelog/latest' }),
        role: 'tool',
        timestamp: 3000
      }
    ]

    const artifacts = collectArtifactsForSession(makeSession({ id: 'session-2' }), messages)

    expect(artifacts).toHaveLength(1)
    expect(artifacts[0]).toMatchObject({
      href: 'https://example.com/changelog/latest',
      kind: 'link',
      value: 'https://example.com/changelog/latest'
    })
  })

  it('normalizes second-precision timestamps to ms for the artifacts page', () => {
    // Message timestamps, session.last_active and session.started_at are all
    // persisted as epoch SECONDS (see chat-runtime messageCreatedAt * 1000 and
    // session-date-groups * SECOND). The artifacts page renders with
    // `new Date(timestamp)` which reads ms, so the collector must convert.
    // 1781271088s = 2026-06-12; fed to new Date() raw it lands on 1970-01-21
    // (#83380).
    const messages: SessionMessage[] = [
      {
        content: 'https://example.com/artifact',
        role: 'assistant',
        timestamp: 1781271088
      }
    ]

    const [artifact] = collectArtifactsForSession(makeSession(), messages)

    expect(artifact.timestamp).toBe(1781271088 * 1000)
    expect(new Date(artifact.timestamp).getUTCFullYear()).toBe(2026)
  })

  it('falls back to session last_active (seconds) when the message has no timestamp', () => {
    const [artifact] = collectArtifactsForSession(
      makeSession({ last_active: 1781271088 }),
      [{ content: 'https://example.com/artifact', role: 'assistant', timestamp: 0 }]
    )

    expect(artifact.timestamp).toBe(1781271088 * 1000)
  })

  it('keeps Date.now() (ms) as the fallback when no timestamp source exists', () => {
    const now = 1_720_000_000_000
    vi.spyOn(Date, 'now').mockReturnValue(now)

    const [artifact] = collectArtifactsForSession(
      makeSession({ last_active: 0, started_at: 0 }),
      [{ content: 'https://example.com/artifact', role: 'assistant', timestamp: 0 }]
    )

    expect(artifact.timestamp).toBe(now)
  })

  it('resolves local file image artifacts through the desktop fs bridge', async () => {
    const readFileDataUrl = vi.fn(async () => 'data:image/png;base64,TE9DQUw=')
    vi.stubGlobal('window', { hermesDesktop: { readFileDataUrl } })

    // Local desktop (connection mode != 'remote'): a local image_generate
    // output path must be read through the Electron bridge, not left as a
    // file:// URL the renderer cannot load (#83380).
    const path = '/home/me/.hermes/cache/image_generate/out.png'

    await expect(artifactImageSrc(path)).resolves.toBe('data:image/png;base64,TE9DQUw=')
    expect(readFileDataUrl).toHaveBeenCalledWith(path)
  })

  it('resolves remote image artifact thumbnails through the desktop fs bridge', async () => {
    const api = vi.fn(async ({ path }: { path: string }) => {
      if (path.startsWith('/api/fs/read-data-url?')) {
        return { dataUrl: 'data:image/jpeg;base64,cmVtb3Rl' }
      }

      throw new Error(`unexpected path ${path}`)
    })

    vi.stubGlobal('window', { hermesDesktop: { api } })
    $connection.set({ baseUrl: 'https://gw', mode: 'remote', token: 'secret' } as never)

    const path = '/Users/me/.hermes/skills/work-esab/references/images/manual-step03.jpeg'

    await expect(artifactImageSrc(path)).resolves.toBe('data:image/jpeg;base64,cmVtb3Rl')

    expect(api).toHaveBeenCalledWith({
      path: '/api/fs/read-data-url?path=%2FUsers%2Fme%2F.hermes%2Fskills%2Fwork-esab%2Freferences%2Fimages%2Fmanual-step03.jpeg'
    })
  })
})

describe('loadArtifactsForSessions', () => {
  it('loads transcripts serially and continues after a session fails', async () => {
    const sessions = [
      makeSession({ id: 'session-1' }),
      makeSession({ id: 'session-2' }),
      makeSession({ id: 'session-3' })
    ]

    const callOrder: string[] = []
    let activeLoads = 0
    let maxActiveLoads = 0

    const result = await loadArtifactsForSessions(sessions, async session => {
      activeLoads += 1
      maxActiveLoads = Math.max(maxActiveLoads, activeLoads)
      callOrder.push(`start:${session.id}`)

      try {
        await Promise.resolve()

        if (session.id === 'session-2') {
          throw new Error('Session transcript exceeds the Desktop safe-load limit')
        }

        return [
          {
            content: `https://example.com/${session.id}.png`,
            role: 'assistant',
            timestamp: 2000
          }
        ]
      } finally {
        callOrder.push(`end:${session.id}`)
        activeLoads -= 1
      }
    })

    expect(maxActiveLoads).toBe(1)
    expect(callOrder).toEqual([
      'start:session-1',
      'end:session-1',
      'start:session-2',
      'end:session-2',
      'start:session-3',
      'end:session-3'
    ])
    expect(result.artifacts.map(artifact => artifact.sessionId)).toEqual(['session-1', 'session-3'])
    expect(result.failures).toHaveLength(1)
    expect(result.failures[0]?.session.id).toBe('session-2')
  })
})
