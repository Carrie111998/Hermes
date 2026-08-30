/** `preview.act.request` — the drive_preview bridge.
 *
 *  Two shipped bugs had no coverage here at all, and both were invisible from
 *  the agent's side because the tool answered with a plausible sentence rather
 *  than failing loudly:
 *
 *  1. The gate compared the requesting session against the PRIMARY view's
 *     runtime. This bridge mounts once (wiring.tsx), so every tile — including
 *     every ⌘T tab — was refused permanently while the user was looking
 *     straight at it, and told "the session the user is looking at".
 *  2. `url` and `full` were on the wire but missing from the forwarded field
 *     list, so `navigate` reported "navigate needs a url" for a url it had
 *     been given, and `elements full=true` quietly answered with a delta.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { group } from '@/components/pane-shell/tree/model'
import { $layoutTree, noteActiveTreeGroup } from '@/components/pane-shell/tree/store'
import { $activeSessionId, $selectedStoredSessionId } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

/** The pane's navigate arm, at the far end of the real engine. Mocking here
 *  rather than mocking the engine itself keeps the whole relay under test —
 *  the handler loads the engine through a cache-busted dynamic import that a
 *  module mock on the engine's own specifier would not intercept. */
const navigate = vi.fn()

vi.mock('@/app/chat/right-rail/preview-nav', () => ({
  agentPreviewNav: () => ({ back: vi.fn(), forward: vi.fn(), navigate, reload: vi.fn() })
}))

const request = vi.fn(async () => undefined)

vi.mock('@/store/gateway', () => ({
  // `subscribe` is unused here but other store modules pulled in by the import
  // graph attach listeners at module scope.
  $gateway: { get: () => ({ request }), subscribe: () => () => undefined }
}))

const { handleDesktopBridgeEvent, previewActionFromPayload } = await import('./desktop-bridge')

const PRIMARY = 'runtime-primary'
const TILE = 'runtime-tile'

/** Minimal context: the act handler reads only these four fields. */
function actEvent(sessionId: string, payload: Record<string, unknown>) {
  return {
    event: { type: 'preview.act.request', session_id: sessionId },
    // isActiveEvent is what index.ts computes against the PRIMARY ref, which is
    // exactly the value the old gate trusted on its own.
    isActiveEvent: sessionId === $activeSessionId.get(),
    payload: { request_id: 'req-1', ...payload },
    sessionId
  } as never
}

/** The single answer this handler sends back over the gateway. */
async function answered() {
  await vi.waitFor(() => expect(request).toHaveBeenCalled())

  const [method, params] = request.mock.calls.at(-1) as unknown as [string, { text: string }]

  expect(method).toBe('preview.act.respond')

  return JSON.parse(params.text) as { error?: string; success: boolean }
}

beforeEach(() => {
  vi.clearAllMocks()
  $activeSessionId.set(PRIMARY)
  $selectedStoredSessionId.set('stored-primary')
  $sessionTiles.set([])
  $layoutTree.set(null)
})

describe('preview.act.request gate', () => {
  it('acts for the primary view when it holds focus', async () => {
    handleDesktopBridgeEvent(actEvent(PRIMARY, { action: 'navigate', url: 'https://example.com' }))

    expect(await answered()).toMatchObject({ success: true })
    expect(navigate).toHaveBeenCalledWith('https://example.com')
  })

  it('acts for a focused tile, whose runtime is never the primary ref', async () => {
    // What the user hit: a ⌘T tab is a tile with a runtime of its own, and the
    // bridge's activeSessionIdRef still points at the primary. The tile holds
    // the active pane while the primary selection stays put — that gap is what
    // makes $focusedRuntimeId answer with the tile rather than the primary.
    $sessionTiles.set([{ runtimeId: TILE, storedSessionId: 'stored-tile' }] as never)
    $layoutTree.set(
      group(['workspace', 'session-tile:stored-tile'], { active: 'session-tile:stored-tile', id: 'grp-main' })
    )
    noteActiveTreeGroup('grp-main')

    const ctx = actEvent(TILE, { action: 'navigate', url: 'https://example.com' })

    // The old gate's whole input, and it says no.
    expect((ctx as { isActiveEvent: boolean }).isActiveEvent).toBe(false)

    handleDesktopBridgeEvent(ctx)

    expect(await answered()).toMatchObject({ success: true })
    expect(navigate).toHaveBeenCalledWith('https://example.com')
  })

  it('still refuses a session that is neither primary nor focused', async () => {
    handleDesktopBridgeEvent(actEvent('runtime-background', { action: 'navigate', url: 'https://example.com' }))

    const result = await answered()

    expect(result.success).toBe(false)
    // Both ids named, so a bug report says which chat asked.
    expect(result.error).toContain('runtime-background')
    expect(result.error).toContain(PRIMARY)
    expect(navigate).not.toHaveBeenCalled()
  })
})

describe('previewActionFromPayload', () => {
  // Every optional field tools/drive_preview_tool.py can put on the wire. A
  // name dropped from the mapping is a feature that fails with a wrong reason.
  it('relays every field drive_preview sends', () => {
    expect(
      previewActionFromPayload({
        action: 'type',
        amount: 400,
        full: true,
        key: 'Enter',
        max: 50,
        ref: 'e7',
        request_id: 'req-1',
        selector: '#name',
        submit: true,
        text: 'محمد',
        to: 'bottom',
        url: 'https://example.com'
      } as never)
    ).toEqual({
      amount: 400,
      full: true,
      key: 'Enter',
      kind: 'type',
      max: 50,
      ref: 'e7',
      selector: '#name',
      submit: true,
      text: 'محمد',
      to: 'bottom',
      url: 'https://example.com'
    })
  })

  it('leaves absent fields undefined rather than inventing defaults', () => {
    expect(previewActionFromPayload({ action: 'reload' } as never)).toMatchObject({
      kind: 'reload',
      url: undefined
    })
  })
})
