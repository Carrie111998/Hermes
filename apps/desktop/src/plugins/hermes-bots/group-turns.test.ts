import { beforeEach, describe, expect, it, vi } from 'vitest'

import type * as groupChat from './group-chat'
import type * as groupRounds from './group-rounds'
import { createGroupGateway, drain, runTimersInline, scriptedStorage } from './group-test-utils'
import type { GatewayOptions, ScriptedGateway } from './group-test-utils'
import type * as groupTurns from './group-turns'
import type { Attachment, GroupChat, GroupMember } from './types'

// One member's turn: resolve its per-room session, stage attachments, submit,
// and poll until the gateway says the member is done. The session is the
// fragile part — it is addressed by a STORED id that outlives every runtime
// id, and the socket it rides can be reaped underneath a turn in flight.

const { host } = vi.hoisted(() => ({ host: {} as Record<string, unknown> }))

vi.mock('@hermes/plugin-sdk', async () => {
  const { pluginSdkMock } = await import('./group-test-utils')

  return pluginSdkMock(host)
})

interface Room {
  chat: typeof groupChat
  gateway: ScriptedGateway
  rounds: typeof groupRounds
  turns: typeof groupTurns
}

async function loadRoom(options: GatewayOptions = {}): Promise<Room> {
  vi.resetModules()
  const gateway = createGroupGateway(options)

  for (const key of Object.keys(host)) {
    delete host[key]
  }

  Object.assign(host, gateway.host)

  const [chat, rounds, turns, shared] = await Promise.all([
    import('./group-chat'),
    import('./group-rounds'),
    import('./group-turns'),
    import('./shared')
  ])

  shared.setPluginCtx(scriptedStorage(gateway.storage))

  return { chat, gateway, rounds, turns }
}

const LOCAL_MEMBER: GroupMember = { name: 'helper', title: '' }
const ROUTED_MEMBER: GroupMember = { connectionId: 'mini', name: 'helper', remoteSource: true }
const IMG: Attachment = { data: 'data:image/png;base64,iVBORw0KGgo=', kind: 'image', name: 'shot.png' }

const log = (room: Room, group: string) => room.chat.$groupChats.get()[group]?.log || []

beforeEach(() => {
  runTimersInline()
})

describe('session resolution', () => {
  it('pins session titles to the roomId, with a legacy fallback to the display name', async () => {
    const room = await loadRoom()

    // Rooms persisted before roomIds keep name-based titles so their existing
    // "Group: <name>" sessions keep resolving after an upgrade.
    room.chat.updateGroupChat('Legacy', current => current)
    const legacy = await room.turns.ensureGroupChatSession('Legacy', { name: 'research', title: '' })

    expect(room.gateway.sessions.get(String(legacy.stored))?.title).toBe('Group: Legacy')

    // New rooms pin the title to the immutable roomId, never the display name.
    room.chat.updateGroupChat('New', current => {
      current.roomId = 'r-abc'

      return current
    })
    const fresh = await room.turns.ensureGroupChatSession('New', { name: 'research', title: '' })

    expect(room.gateway.sessions.get(String(fresh.stored))?.title).toBe('Group: r-abc')
  })

  it('creates member sessions with the room_plumbing + follow_profile_config contracts', async () => {
    // The PR #97008 contracts: room member sessions always rebuild from the
    // member profile's CURRENT config on resume, never a stale stored
    // model/provider pin. Dropping either param silently regresses rooms to
    // the server's hidden + "Group: " title legacy fallback.
    const room = await loadRoom()

    room.chat.updateGroupChat('Contract', current => {
      current.roomId = 'r-contract'

      return current
    })
    const handle = await room.turns.ensureGroupChatSession('Contract', { name: 'research', title: '' })

    expect(room.gateway.sessions.get(String(handle.stored))?.contracts).toEqual({
      follow_profile_config: true,
      room_plumbing: true
    })
  })

  it('mints fresh member sessions when a same-name group is recreated after disband', async () => {
    const room = await loadRoom()
    const member: GroupMember = { name: 'research', title: '' }

    room.chat.updateGroupChat('Alpha', current => {
      current.roomId = 'r-one'

      return current
    })
    const first = await room.turns.ensureGroupChatSession('Alpha', member)

    // Disband: the room record is gone; the member's gateway session survives.
    const rooms = { ...room.chat.$groupChats.get() }
    delete rooms.Alpha
    room.chat.$groupChats.set(rooms)

    // Recreate under the same display name with a freshly minted roomId.
    room.chat.updateGroupChat('Alpha', current => {
      current.roomId = 'r-two'

      return current
    })
    const second = await room.turns.ensureGroupChatSession('Alpha', member)

    expect(first.stored).not.toBe(second.stored)
    expect(room.gateway.sessions.get(String(first.stored))?.title).toBe('Group: r-one')
    expect(room.gateway.sessions.get(String(second.stored))?.title).toBe('Group: r-two')
  })

  it('fails closed on a transient resume failure instead of forking the member session', async () => {
    // Mirrors findExistingCanonicalChat's fix (87b645f52c): a resume that
    // fails for any reason OTHER than "genuinely doesn't exist" (JSON-RPC code
    // 4007) must surface, not be read as "no session, mint a new one" — that
    // would fork the member's real history and silently overwrite
    // room.sessions[key] so the old session becomes unreachable.
    const room = await loadRoom()
    const member: GroupMember = { name: 'research', title: '' }

    room.chat.updateGroupChat('Alpha', current => {
      current.roomId = 'r-one'

      return current
    })
    const first = await room.turns.ensureGroupChatSession('Alpha', member)
    const before = room.chat.$groupChats.get().Alpha.sessions

    // A real gateway error on the NEXT resume of the member's own stored
    // session — backend still warming up, a network blip, not "not found".
    const request = host.request as (method: string, params: Record<string, unknown>) => Promise<unknown>

    host.request = async (method: string, params: Record<string, unknown>) => {
      if (method === 'session.resume' && params.session_id === first.stored) {
        throw Object.assign(new Error('gateway temporarily unavailable'), { code: 5000 })
      }

      return request(method, params)
    }

    await expect(room.turns.ensureGroupChatSession('Alpha', member)).rejects.toThrow(/Could not check .*group session/)

    host.request = request

    // Nothing was forked: the room's session pointer is untouched, and the
    // original session is still the only one on record for this member.
    expect(room.chat.$groupChats.get().Alpha.sessions).toEqual(before)
    expect(room.gateway.sessions.size).toBe(1)
  })

  it('falls through a genuine 4007 to the title lookup, then creates', async () => {
    const room = await loadRoom()

    room.chat.updateGroupChat('Alpha', current => {
      current.roomId = 'r-one'
      // A stored sid pointing at a session the gateway no longer has (e.g. an
      // out-of-band deletion) is a genuine 4007 on the first target.
      current.sessions = { research: 'sid-gone' }

      return current
    })

    const result = await room.turns.ensureGroupChatSession('Alpha', { name: 'research', title: '' })

    expect(result.stored).toBeTruthy()
    expect(result.stored).not.toBe('sid-gone')
  })
})

describe('session-gone classification', () => {
  it('persists a recovery marker before a submitted turn can outlive the window', async () => {
    const room = await loadRoom({ turn: () => 'finished while the window was gone' })
    const request = host.request as (method: string, params: Record<string, unknown>) => Promise<unknown>

    host.request = async (method: string, params: Record<string, unknown>) => {
      if (method === 'prompt.submit') {
        const durableBeforeSubmit = (room.gateway.storage.get('group-chats') || {}) as Record<string, GroupChat>

        expect(durableBeforeSubmit.Room.stranded?.helper).toMatchObject({
          before: 0,
          phase: 'prepared',
          thread: 't1'
        })
      }

      return request(method, params)
    }

    const reply = await room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'keep working', 't1', [])

    expect(reply).toBe('finished while the window was gone')
    expect(room.gateway.calls).toHaveLength(1)

    const durable = (room.gateway.storage.get('group-chats') || {}) as Record<string, GroupChat>
    const marker = durable.Room.stranded?.helper

    expect(marker).toMatchObject({
      before: 0,
      phase: 'submitted',
      thread: 't1'
    })
    expect(typeof (marker as { recoveryId?: unknown }).recoveryId).toBe('string')
  })

  it('does not submit when the recovery boundary cannot be read back', async () => {
    const room = await loadRoom({ turn: () => 'must never run' })
    const shared = await import('./shared')

    shared.setPluginCtx({
      storage: {
        get: async () => null,
        set: async () => undefined
      }
    } as never)

    await expect(room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'keep working', 't1', [])).rejects.toThrow(
      /recovery state.*not sent/i
    )
    expect(room.gateway.calls).toHaveLength(0)
  })

  it('keeps polling when the post-submit persistence refresh fails', async () => {
    const room = await loadRoom({ turn: () => 'already accepted' })
    const shared = await import('./shared')
    const storage = new Map<string, unknown>()

    shared.setPluginCtx({
      storage: {
        get: async (key: string) => storage.get(key) ?? null,
        set: async (key: string, value: Record<string, GroupChat>) => {
          const marker = value?.Room?.stranded?.helper as { phase?: string } | undefined

          if (marker?.phase === 'submitted') {
            throw new Error('storage refresh failed')
          }

          storage.set(key, structuredClone(value))
        }
      }
    } as never)

    await expect(room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'keep working', 't1', [])).resolves.toBe(
      'already accepted'
    )
    expect(room.gateway.calls).toHaveLength(1)
    expect((storage.get('group-chats') as Record<string, GroupChat>).Room.stranded?.helper).toMatchObject({
      phase: 'prepared'
    })
  })

  it('reconciles a completed post-submit turn once after a simulated restart', async () => {
    const room = await loadRoom({ turn: () => 'finished while the window was gone' })

    await room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'keep working', 't1', [])

    const marker = room.chat.$groupChats.get().Room.stranded?.helper as { recoveryId: string }

    expect(log(room, 'Room')).toHaveLength(0)

    // The process-local poller is gone; cold hydration sees only the durable
    // room marker and the backend session that completed independently.
    await room.turns.recoverInterruptedGroupTurns()
    await room.turns.recoverInterruptedGroupTurns()

    expect(log(room, 'Room')).toHaveLength(1)
    expect(log(room, 'Room')[0]).toMatchObject({
      from: { kind: 'member', name: 'helper' },
      id: marker.recoveryId,
      text: 'finished while the window was gone',
      thread: 't1'
    })
    expect(room.chat.$groupChats.get().Room.stranded?.helper).toBeUndefined()
  })

  it('serializes overlapping turns for one room member until the first marker settles', async () => {
    const room = await loadRoom({ turn: ({ n }) => `reply ${n}` })
    const request = host.request as (method: string, params: Record<string, unknown>) => Promise<unknown>
    let releaseStart: () => void = () => undefined

    const blockedStart = new Promise<void>(resolve => {
      releaseStart = resolve
    })

    let starts = 0

    host.request = async (method: string, params: Record<string, unknown>) => {
      if (method === 'session.create') {
        starts += 1

        if (starts === 1) {
          await blockedStart
        }
      }

      return request(method, params)
    }

    const first = room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'first', 't1', [])

    await new Promise(resolve => setImmediate(resolve))

    const second = room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'second', 't1', [])

    await new Promise(resolve => setImmediate(resolve))
    expect(starts).toBe(1)

    releaseStart()
    await expect(first).resolves.toBe('reply 1')
    expect(room.gateway.calls).toHaveLength(1)

    const marker = room.chat.$groupChats.get().Room.stranded?.helper as { recoveryId: string }

    room.turns.settleGroupTurnRecovery('Room', LOCAL_MEMBER, marker.recoveryId)
    await expect(second).resolves.toBe('reply 2')
    expect(room.gateway.calls).toHaveLength(2)
  })

  it('treats 4001 and "not in memory" as recoverable, 4007 as not', async () => {
    const { turns } = await loadRoom()

    expect(turns.isSessionGoneError(Object.assign(new Error('x'), { code: 4001 }))).toBe(true)
    expect(turns.isSessionGoneError(new Error('session_id=rt-1 not in memory'))).toBe(true)
    expect(turns.isSessionGoneError(Object.assign(new Error('session not found'), { code: 4007 }))).toBe(false)
    expect(turns.isSessionGoneError(null)).toBe(false)
    expect(turns.isSessionGoneError(new Error('network blip'))).toBe(false)
  })

  it('recovers a 4001 on the first submit via the STORED id and delivers', async () => {
    const room = await loadRoom({
      failFirstSubmitWith: Object.assign(new Error("session-scoped RPC rejected: session_id='rt-1' not in memory"), {
        code: 4001
      }),
      turn: () => 'recovered reply'
    })

    const reply = await room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'hi there', 't1', [])

    expect(reply).toBe('recovered reply')
    // One failed submit + exactly one retry — never more.
    expect(room.gateway.rpcFor('prompt.submit')).toHaveLength(2)
    // The recovery re-resumed the durable stored id, not the dead runtime id.
    expect(room.chat.$groupChats.get().Room.sessions?.helper).toBeTruthy()
  })

  it('does not retry a persistent non-4001 submit failure', async () => {
    const room = await loadRoom({ failEverySubmitWith: new Error('backend exploded') })

    await expect(room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'hi', 't1', [])).rejects.toThrow(
      'backend exploded'
    )
    expect(room.gateway.rpcFor('prompt.submit')).toHaveLength(1)
  })
})

describe('per-turn socket lease', () => {
  it('is acquired before any session RPC and held across attach + submit', async () => {
    const room = await loadRoom({ turn: () => 'routed reply' })

    const reply = await room.turns.runGroupChatMemberTurn('Room', ROUTED_MEMBER, 'look at this', 't1', [IMG])

    expect(reply).toBe('routed reply')
    // The retain landed before the first session-scoped RPC on the route.
    expect(room.gateway.timeline[0]).toBe('retain')

    // The socket was NEVER disposed mid-turn: after every per-request lease
    // released, the turn lease still held the refcount above zero.
    for (const call of room.gateway.rpc) {
      expect(call.refcountAfter).toBeGreaterThanOrEqual(1)
    }

    // Exactly one disposal, via the turn lease's own release at the end.
    expect(room.gateway.disposals()).toBe(1)
    expect(room.gateway.timeline.at(-1)).toBe('release')
  })

  it('is released after the turn — the refcount returns to zero', async () => {
    const room = await loadRoom({ turn: () => 'done' })

    await room.turns.runGroupChatMemberTurn('Room', ROUTED_MEMBER, 'hi', 't1', [])

    expect(room.gateway.refcount()).toBe(0)
    expect(room.gateway.disposals()).toBe(1)
  })

  it('is released even when the turn fails', async () => {
    const room = await loadRoom({ failEverySubmitWith: new Error('backend exploded') })

    await expect(room.turns.runGroupChatMemberTurn('Room', ROUTED_MEMBER, 'hi', 't1', [])).rejects.toThrow()
    expect(room.gateway.refcount()).toBe(0)
  })

  it('is feature-detected: hosts without retainProfile still run the turn', async () => {
    const room = await loadRoom({ turn: () => 'legacy ok' })
    delete host.retainProfile

    expect(await room.turns.runGroupChatMemberTurn('Room', ROUTED_MEMBER, 'hi', 't1', [])).toBe('legacy ok')
  })
})

// #94376: a Codex intent-ack continuation nudge can land a substantive
// answer, then get a synthetic "(pass)" reply to the nudge itself.
describe('reply selection (#94376)', () => {
  it('surfaces a substantive answer followed by a synthetic continuation (pass)', async () => {
    const answer = "Yes. I welcomed them, and I'll review their first assignments with them."

    const room = await loadRoom({
      turn: () => [
        { content: answer, role: 'assistant' },
        {
          content:
            '[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]',
          role: 'user'
        },
        { content: '(pass)', role: 'assistant' }
      ]
    })

    expect(await room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'Did you welcome them?', 't1', [])).toBe(
      answer
    )
  })

  it('still reads a genuine pass-only turn as silent', async () => {
    const room = await loadRoom({ turn: () => '(pass)' })

    expect(await room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'anything new?', 't1', [])).toBe('(pass)')
  })
})

// #90694: a member can block inside its own turn on a clarify question or a
// command approval. Those live in a hidden session the room can't see, so the
// poll mirrors them into the room and holds the turn open until they resolve.
describe('clarify and approvals (#90694)', () => {
  const CLARIFY = {
    choices: ['staging', 'prod'],
    multi_select: false,
    question: 'Which env should I target?',
    request_id: 'req-clarify-1'
  }

  const APPROVAL = {
    choices: ['once', 'session', 'deny'],
    command: 'rm -rf ./build',
    description: 'Clean the build directory',
    request_id: 'req-approval-1'
  }

  it('holds the turn open while a member is blocked on clarify, then lands the reply', async () => {
    const room = await loadRoom({
      clarifyUntil: { research: { payload: CLARIFY, until: 3 } },
      turn: () => 'targeting staging'
    })

    const thread = room.rounds.sendToGroupChat(
      'Core',
      [{ name: 'research', title: '' }],
      '@research deploy it',
      null,
      []
    )

    await drain(() => Boolean(room.chat.$groupChats.get().Core?.running))

    const replies = log(room, 'Core').filter(entry => entry.thread === thread && entry.from.kind === 'member')

    expect(replies).toHaveLength(1)
    expect(replies[0].text).toBe('targeting staging')
    expect(Object.keys(room.chat.$groupClarify.get())).toHaveLength(0)
    // The mirror pass ran while the question was blocking, badging the room.
    // A poll that never inspects pending_clarify leaves this unset — it is the
    // observable proof the gate executed.
    expect(room.chat.$groupNeedsYou.get().Core).toBe(true)
  })

  it('mirrors a question, badges needs-you, and is idempotent per request', async () => {
    const { chat, turns } = await loadRoom()
    const member: GroupMember = { name: 'research', title: '' }

    expect(turns.syncGroupClarify('Core', member, { pending_clarify: CLARIFY })).toBe(true)

    const mirrored = Object.values(chat.$groupClarify.get())

    expect(mirrored).toHaveLength(1)
    expect(mirrored[0].requestId).toBe('req-clarify-1')
    expect(mirrored[0].question).toBe('Which env should I target?')
    expect(mirrored[0].choices).toEqual(['staging', 'prod'])
    expect(chat.$groupNeedsYou.get().Core).toBe(true)

    // Same request again: no new entry, identity preserved.
    turns.syncGroupClarify('Core', member, { pending_clarify: CLARIFY })

    expect(Object.values(chat.$groupClarify.get())[0]).toBe(mirrored[0])

    // Question resolved server-side: the mirror clears.
    expect(turns.syncGroupClarify('Core', member, {})).toBe(false)
    expect(Object.keys(chat.$groupClarify.get())).toHaveLength(0)
  })

  it('never mirrors a question for older backends without pending_clarify', async () => {
    const { chat, turns } = await loadRoom()

    expect(turns.syncGroupClarify('Core', { name: 'research' }, { messages: [] })).toBe(false)
    expect(Object.keys(chat.$groupClarify.get())).toHaveLength(0)
  })

  it('routes an answer through clarify.respond and clears the mirror', async () => {
    const room = await loadRoom()
    const member: GroupMember = { name: 'research', title: '' }

    room.turns.syncGroupClarify('Core', member, { pending_clarify: CLARIFY })
    await room.turns.answerGroupClarify(Object.values(room.chat.$groupClarify.get())[0], member, 'staging')

    expect(room.gateway.rpcFor('clarify.respond').map(call => call.params)).toEqual([
      { answer: 'staging', request_id: 'req-clarify-1' }
    ])
    expect(Object.keys(room.chat.$groupClarify.get())).toHaveLength(0)
  })

  it('sends one respond per batch question, in order', async () => {
    const room = await loadRoom()
    const member: GroupMember = { name: 'research', title: '' }

    room.turns.syncGroupClarify('Core', member, {
      pending_clarify: {
        questions: [
          { choices: ['staging', 'prod'], qid: 'q0', question: 'Env?' },
          { choices: [], qid: 'q1', question: 'Region?' }
        ],
        request_id: 'req-batch-1'
      }
    })
    await room.turns.answerGroupClarify(Object.values(room.chat.$groupClarify.get())[0], member, {
      q0: 'staging',
      q1: 'eu-west'
    })

    expect(room.gateway.rpcFor('clarify.respond').map(call => call.params)).toEqual([
      { answer: 'staging', question_id: 'q0', request_id: 'req-batch-1' },
      { answer: 'eu-west', question_id: 'q1', request_id: 'req-batch-1' }
    ])
    expect(Object.keys(room.chat.$groupClarify.get())).toHaveLength(0)
  })

  it('clears only the disbanded room’s mirrored questions', async () => {
    const room = await loadRoom()

    room.turns.syncGroupClarify('Core', { name: 'research' }, { pending_clarify: CLARIFY })
    room.turns.syncGroupClarify('Other', { name: 'ops' }, { pending_clarify: { ...CLARIFY, request_id: 'req-2' } })
    room.turns.clearGroupClarify('Core')

    const remaining = Object.values(room.chat.$groupClarify.get())

    expect(remaining).toHaveLength(1)
    expect(remaining[0].group).toBe('Other')
  })

  it('holds the turn open on a command approval too', async () => {
    const room = await loadRoom({
      approvalUntil: { research: { payload: APPROVAL, until: 3 } },
      turn: () => 'build cleaned'
    })

    const thread = room.rounds.sendToGroupChat(
      'Core',
      [{ name: 'research', title: '' }],
      '@research clean up',
      null,
      []
    )

    await drain(() => Boolean(room.chat.$groupChats.get().Core?.running))

    const replies = log(room, 'Core').filter(entry => entry.thread === thread && entry.from.kind === 'member')

    expect(replies).toHaveLength(1)
    expect(replies[0].text).toBe('build cleaned')
    expect(Object.keys(room.chat.$groupClarify.get())).toHaveLength(0)
    expect(room.chat.$groupNeedsYou.get().Core).toBe(true)
  })

  it('mirrors an approval with its kind, command and server choices', async () => {
    const { chat, turns } = await loadRoom()

    expect(
      turns.syncGroupClarify(
        'Core',
        { name: 'research', title: '' },
        {
          pending_approval: APPROVAL,
          session_id: 'rt-research-1'
        }
      )
    ).toBe(true)

    const entry = Object.values(chat.$groupClarify.get())[0]

    expect(entry.kind).toBe('approval')
    expect(entry.command).toBe('rm -rf ./build')
    expect(entry.question).toBe('Clean the build directory')
    expect(entry.choices).toEqual(['once', 'session', 'deny'])
    expect(entry.sessionId).toBe('rt-research-1')
  })

  it('falls back to once/deny when the server sends no choice set', async () => {
    const { chat, turns } = await loadRoom()

    turns.syncGroupClarify('Core', { name: 'research' }, { pending_approval: { command: 'ls', request_id: 'req-a2' } })

    expect(Object.values(chat.$groupClarify.get())[0].choices).toEqual(['once', 'deny'])
  })

  it('routes approvals through approval.respond with the session and choice', async () => {
    const room = await loadRoom()
    const member: GroupMember = { name: 'research', title: '' }

    room.turns.syncGroupClarify('Core', member, { pending_approval: APPROVAL, session_id: 'rt-research-1' })
    await room.turns.answerGroupClarify(Object.values(room.chat.$groupClarify.get())[0], member, 'once')

    expect(room.gateway.rpcFor('approval.respond').map(call => call.params)).toEqual([
      { choice: 'once', request_id: 'req-approval-1', session_id: 'rt-research-1' }
    ])
    expect(room.gateway.rpcFor('clarify.respond')).toHaveLength(0)
    expect(Object.keys(room.chat.$groupClarify.get())).toHaveLength(0)
  })

  it('lets clarify outrank approval when a snapshot carries both', async () => {
    const { chat, turns } = await loadRoom()

    turns.syncGroupClarify('Core', { name: 'research' }, { pending_approval: APPROVAL, pending_clarify: CLARIFY })

    const entry = Object.values(chat.$groupClarify.get())[0]

    expect(entry.kind).toBe('clarify')
    expect(entry.requestId).toBe('req-clarify-1')
  })
})

// A turn that outlives its deadline leaves a "stranded" marker. The member is
// still working; the next round harvests whatever landed instead of throwing
// the finished work away.
describe('stranded harvest', () => {
  const seedSession = (room: Room, stored: string, profile: string, title: string, messages: string[][]) => {
    room.gateway.sessions.set(stored, {
      messages: messages.map(([role, content]) => ({ content, role })),
      profile,
      runtime: `rt-${profile}`,
      stored,
      title
    })
  }

  it('posts a late reply into the room and clears the marker', async () => {
    const room = await loadRoom()

    room.chat.updateGroupChat('Late', current => {
      current.sessions = { research: 'sid-research' }
      current.stranded = { research: 0 }

      return current
    })
    // The member's session finished after we stopped waiting.
    seedSession(room, 'sid-research', 'research', 'Group: Late', [
      ['user', 'the turn prompt'],
      ['assistant', 'Here is the full research result, delivered late.']
    ])

    await room.turns.harvestStrandedGroupReply('Late', { name: 'research', title: '' })

    expect(log(room, 'Late')).toHaveLength(1)
    expect(log(room, 'Late')[0].from.name).toBe('research')
    expect(log(room, 'Late')[0].text).toMatch(/delivered late/)
    expect(room.chat.$groupChats.get().Late.stranded?.research).toBeUndefined()
  })

  it('recovers exactly once when the reply append persisted before marker cleanup', async () => {
    const room = await loadRoom()
    const recoveryId = 'recovery-cold-1'

    room.chat.updateGroupChat('Cold', current => {
      current.log = [
        {
          at: 1,
          from: { kind: 'member', name: 'research' },
          id: recoveryId,
          text: 'Recovered after restart.',
          thread: 't-cold'
        }
      ]
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: 1,
          phase: 'submitted',
          recoveryId,
          thread: 't-cold'
        }
      }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: Cold', [
      ['user', 'the turn prompt'],
      ['assistant', 'Recovered after restart.']
    ])

    await room.turns.harvestStrandedGroupReply('Cold', { name: 'research', title: '' })

    expect(log(room, 'Cold')).toHaveLength(1)
    expect(log(room, 'Cold')[0].id).toBe(recoveryId)
    expect(room.chat.$groupChats.get().Cold.stranded?.research).toBeUndefined()
  })

  it('does not clear a newer turn marker while an older reply is being harvested', async () => {
    const room = await loadRoom()
    const request = host.request as (method: string, params: Record<string, unknown>) => Promise<unknown>

    room.chat.updateGroupChat('Cold', current => {
      current.members = [{ name: 'research', title: '' }]
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: 0,
          phase: 'submitted',
          recoveryId: 'recovery-old',
          thread: 't-old'
        }
      }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: Cold', [
      ['user', 'the old prompt'],
      ['assistant', 'the old reply']
    ])

    host.request = async (method: string, params: Record<string, unknown>) => {
      const result = await request(method, params)

      if (method === 'session.resume') {
        room.chat.updateGroupChat('Cold', current => {
          current.stranded = {
            research: {
              before: 2,
              phase: 'submitted',
              recoveryId: 'recovery-new',
              thread: 't-new'
            }
          }

          return current
        })
      }

      return result
    }

    await room.turns.harvestStrandedGroupReply('Cold', { name: 'research', title: '' })

    expect(log(room, 'Cold')).toHaveLength(0)
    expect(room.chat.$groupChats.get().Cold.stranded?.research).toMatchObject({ recoveryId: 'recovery-new' })
  })

  it('only recovers markers that existed when cold reconciliation started', async () => {
    const room = await loadRoom({ busyResumes: { research: 1 } })
    let injected = false

    room.chat.updateGroupChat('Cold', current => {
      current.members = [
        { name: 'research', title: '' },
        { name: 'builder', title: '' }
      ]
      current.sessions = { research: 'sid-research', builder: 'sid-builder' }
      current.stranded = {
        research: {
          before: 0,
          phase: 'submitted',
          recoveryId: 'recovery-on-hydrate',
          thread: 't-old'
        }
      }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: Cold', [])
    seedSession(room, 'sid-builder', 'builder', 'Group: Cold', [])
    vi.stubGlobal('setTimeout', (fn: () => void) => {
      if (!injected) {
        injected = true
        room.chat.updateGroupChat('Cold', current => {
          current.stranded = {
            ...(current.stranded || {}),
            builder: {
              before: 0,
              phase: 'submitted',
              recoveryId: 'recovery-after-hydrate',
              thread: 't-new'
            }
          }

          return current
        })
      }

      fn()

      return 0
    })

    await room.turns.recoverInterruptedGroupTurns()

    expect(log(room, 'Cold').map(entry => entry.id)).not.toContain('recovery-after-hydrate')
    expect(room.chat.$groupChats.get().Cold.stranded?.builder).toMatchObject({
      recoveryId: 'recovery-after-hydrate'
    })
  })

  it('waits through a running submitted turn and recovers its reply once', async () => {
    const room = await loadRoom({ busyResumes: { research: 2 } })

    room.chat.updateGroupChat('Cold', current => {
      current.members = [{ name: 'research', title: '' }]
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: 1,
          phase: 'submitted',
          recoveryId: 'recovery-running',
          thread: 't-cold'
        }
      }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: Cold', [
      ['user', 'the turn prompt'],
      ['assistant', 'finished after reconnect']
    ])

    await room.turns.recoverInterruptedGroupTurns()

    expect(room.gateway.rpcFor('session.resume')).toHaveLength(3)
    expect(log(room, 'Cold').map(entry => entry.id)).toEqual(['recovery-running'])
    expect(room.chat.$groupChats.get().Cold.stranded?.research).toBeUndefined()
  })

  it('ends a permanently busy recovery after the bounded poll budget', async () => {
    const room = await loadRoom({ busyResumes: { research: 100 } })

    room.chat.updateGroupChat('Cold', current => {
      current.members = [{ name: 'research', title: '' }]
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: 0,
          phase: 'submitted',
          recoveryId: 'recovery-bounded',
          thread: 't-cold'
        }
      }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: Cold', [])

    await room.turns.recoverInterruptedGroupTurns()

    expect(room.gateway.rpcFor('session.resume')).toHaveLength(60)
    expect(log(room, 'Cold').map(entry => entry.id)).toEqual(['recovery-bounded'])
    expect(room.chat.$groupChats.get().Cold.stranded?.research).toBeUndefined()
  })

  it('ends a permanently unreachable recovery after the same bounded poll budget', async () => {
    const room = await loadRoom()
    const request = host.request as (method: string, params: Record<string, unknown>) => Promise<unknown>
    let resumes = 0

    room.chat.updateGroupChat('Cold', current => {
      current.members = [{ name: 'research', title: '' }]
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: 0,
          phase: 'submitted',
          recoveryId: 'recovery-unreachable',
          thread: 't-cold'
        }
      }

      return current
    })

    host.request = async (method: string, params: Record<string, unknown>) => {
      if (method === 'session.resume') {
        resumes += 1
        throw new Error('source offline')
      }

      return request(method, params)
    }

    await room.turns.recoverInterruptedGroupTurns()

    expect(resumes).toBe(60)
    expect(log(room, 'Cold').map(entry => entry.id)).toEqual(['recovery-unreachable'])
    expect(room.chat.$groupChats.get().Cold.stranded?.research).toBeUndefined()
  })

  it('cancels a pending recovery delay on plugin disposal', async () => {
    const room = await loadRoom({ busyResumes: { research: 100 } })

    room.chat.updateGroupChat('Cold', current => {
      current.members = [{ name: 'research', title: '' }]
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: 0,
          phase: 'submitted',
          recoveryId: 'recovery-disposed',
          thread: 't-cold'
        }
      }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: Cold', [])
    vi.stubGlobal('setTimeout', () => 1)
    vi.stubGlobal('clearTimeout', () => undefined)

    const recovery = room.turns.recoverInterruptedGroupTurns()

    await new Promise(resolve => setImmediate(resolve))
    expect(room.gateway.rpcFor('session.resume')).toHaveLength(1)

    room.turns.stopInterruptedGroupTurnRecovery()
    await recovery

    expect(room.gateway.rpcFor('session.resume')).toHaveLength(1)
    expect(log(room, 'Cold')).toHaveLength(0)
    expect(room.chat.$groupChats.get().Cold.stranded?.research).toMatchObject({
      recoveryId: 'recovery-disposed'
    })
  })

  it('rejects malformed recovery metadata without reading hidden session history', async () => {
    const room = await loadRoom()

    room.chat.updateGroupChat('Cold', current => {
      current.members = [{ name: 'research', title: '' }]
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: -1,
          phase: 'submitted',
          recoveryId: 'malformed-recovery',
          thread: 't-cold'
        }
      } as never

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: Cold', [
      ['assistant', 'private prior session answer']
    ])

    await room.turns.recoverInterruptedGroupTurns()

    expect(room.gateway.rpcFor('session.resume')).toHaveLength(0)
    expect(log(room, 'Cold')).toHaveLength(1)
    expect(log(room, 'Cold')[0].text).toMatch(/status is unknown/i)
    expect(log(room, 'Cold')[0].text).not.toContain('private prior session answer')
    expect(room.chat.$groupChats.get().Cold.stranded?.research).toBeUndefined()
  })

  it('classifies a pre-dispatch process loss as terminally unknown', async () => {
    const room = await loadRoom()

    room.chat.updateGroupChat('Cold', current => {
      current.members = [{ name: 'research', title: '' }]
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: 0,
          phase: 'prepared',
          recoveryId: 'recovery-pre-dispatch',
          thread: 't-cold'
        }
      }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: Cold', [])

    await room.turns.recoverInterruptedGroupTurns()

    expect(log(room, 'Cold')).toHaveLength(1)
    expect(log(room, 'Cold')[0]).toMatchObject({
      from: { kind: 'member', name: 'research' },
      id: 'recovery-pre-dispatch',
      thread: 't-cold'
    })
    expect(log(room, 'Cold')[0].text).toMatch(/status is unknown.*restarted/i)
    expect(room.chat.$groupChats.get().Cold.stranded?.research).toBeUndefined()
  })

  it('commits a recovered reply to the immutable room after a rename', async () => {
    const room = await loadRoom()
    const request = host.request as (method: string, params: Record<string, unknown>) => Promise<unknown>

    room.chat.updateGroupChat('Cold', current => {
      current.members = [{ name: 'research', title: '' }]
      current.roomId = 'room-cold'
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: 1,
          phase: 'submitted',
          recoveryId: 'recovery-renamed',
          thread: 't-cold'
        }
      }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: room-cold', [
      ['user', 'the turn prompt'],
      ['assistant', 'finished after rename']
    ])

    host.request = async (method: string, params: Record<string, unknown>) => {
      const result = await request(method, params)

      if (method === 'session.resume') {
        const rooms = { ...room.chat.$groupChats.get() }

        rooms.Renamed = rooms.Cold
        delete rooms.Cold
        room.chat.$groupChats.set(rooms)
      }

      return result
    }

    await room.turns.harvestStrandedGroupReply('Cold', { name: 'research', title: '' })

    expect(room.chat.$groupChats.get().Cold).toBeUndefined()
    expect(log(room, 'Renamed').map(entry => entry.id)).toEqual(['recovery-renamed'])
    expect(room.chat.$groupChats.get().Renamed.stranded?.research).toBeUndefined()
  })

  it('does not recreate a room disbanded while recovery is awaiting the backend', async () => {
    const room = await loadRoom()
    const request = host.request as (method: string, params: Record<string, unknown>) => Promise<unknown>

    room.chat.updateGroupChat('Cold', current => {
      current.members = [{ name: 'research', title: '' }]
      current.roomId = 'room-cold'
      current.sessions = { research: 'sid-research' }
      current.stranded = {
        research: {
          before: 1,
          phase: 'submitted',
          recoveryId: 'recovery-disbanded',
          thread: 't-cold'
        }
      }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: room-cold', [
      ['user', 'the turn prompt'],
      ['assistant', 'must not resurrect']
    ])

    host.request = async (method: string, params: Record<string, unknown>) => {
      const result = await request(method, params)

      if (method === 'session.resume') {
        room.chat.$groupChats.set({})
      }

      return result
    }

    await room.turns.harvestStrandedGroupReply('Cold', { name: 'research', title: '' })

    expect(room.chat.$groupChats.get()).toEqual({})
  })

  it('persists a real timed-out phase before returning control to the room', async () => {
    const room = await loadRoom({ turn: () => 'finished beyond the local deadline' })
    let now = 0

    vi.spyOn(Date, 'now').mockImplementation(() => {
      now += 200000

      return now
    })

    await expect(room.turns.runGroupChatMemberTurn('Room', LOCAL_MEMBER, 'keep working', 't1', [])).resolves.toBeNull()

    expect(room.chat.$groupChats.get().Room.stranded?.helper).toMatchObject({
      before: 0,
      phase: 'timed-out',
      thread: 't1'
    })

    await room.turns.harvestStrandedGroupReply('Room', LOCAL_MEMBER)

    expect(log(room, 'Room')[0].text).toBe('finished beyond the local deadline')
    expect(room.chat.$groupChats.get().Room.stranded?.helper).toBeUndefined()
  })

  it('prefers the substantive answer over a trailing synthetic (pass)', async () => {
    const room = await loadRoom()

    // #94376 class bug at the second call site: the late-landing turn ends
    // with a Codex intent-ack continuation nudge that gets a synthetic
    // "(pass)" — the harvest must still surface the substantive answer.
    room.chat.updateGroupChat('Rescue', current => {
      current.sessions = { research: 'sid-research' }
      current.stranded = { research: 0 }

      return current
    })
    seedSession(room, 'sid-research', 'research', 'Group: Rescue', [
      ['user', 'the turn prompt'],
      ['assistant', 'Here is the full research result, delivered late.'],
      [
        'user',
        '[System: Continue now. Execute the required tool calls and only send your final answer after completing the task.]'
      ],
      ['assistant', '(pass)']
    ])

    await room.turns.harvestStrandedGroupReply('Rescue', { name: 'research', title: '' })

    expect(log(room, 'Rescue')).toHaveLength(1)
    expect(log(room, 'Rescue')[0].text).toMatch(/delivered late/)
    expect(room.chat.$groupChats.get().Rescue.stranded?.research).toBeUndefined()
  })

  it('consumes the marker without posting when the late reply is a pass', async () => {
    const room = await loadRoom()

    room.chat.updateGroupChat('Quiet2', current => {
      current.sessions = { builder: 'sid-builder' }
      current.stranded = { builder: 2 }

      return current
    })
    seedSession(room, 'sid-builder', 'builder', 'Group: Quiet2', [
      ['user', 'p1'],
      ['user', 'prompt'],
      ['assistant', '(pass)']
    ])

    await room.turns.harvestStrandedGroupReply('Quiet2', { name: 'builder', title: '' })

    expect(log(room, 'Quiet2')).toHaveLength(0)
    expect(room.chat.$groupChats.get().Quiet2.stranded?.builder).toBeUndefined()
  })

  it('never re-submits into a member the harvest just confirmed is still running', async () => {
    // research is confirmed busy on exactly its first two session.resume calls
    // — the number of harvest-only touches the FIXED code makes across two
    // rounds. Without the responder guard research gets re-selected and picks
    // up two EXTRA resume calls of its own before its first post-resubmit
    // poll, which then reports done — so the unguarded run still finishes fast
    // while proving the resubmission happened.
    const room = await loadRoom({
      busyResumes: { research: 2 },
      turn: ({ profile }) => (profile === 'builder' ? 'builder here, all good' : '(pass)')
    })

    // The marker's pre-thread bare-number shape is still supported: presence
    // in `stranded` is what the round-loop guard checks, not the value shape.
    room.gateway.sessions.set('sid-research', {
      messages: [],
      profile: 'research',
      runtime: 'rt-research',
      stored: 'sid-research',
      title: 'Group: Grind'
    })
    room.chat.updateGroupChat('Grind', current => {
      current.log = [{ at: 1, from: { kind: 'user', name: 'You' }, id: 'u1', text: '@research @builder status?' }]
      current.sessions = { research: 'sid-research' }
      current.stranded = { research: 0 }
      current.watermarks = { 'legacy::builder': 0, 'legacy::research': 0 }

      return current
    })

    await room.rounds.runGroupChatRounds(
      'Grind',
      [
        { name: 'research', title: '' },
        { name: 'builder', title: '' }
      ],
      'legacy'
    )

    expect(room.gateway.calls.filter(call => call.profile === 'research')).toHaveLength(0)
    // The marker survives untouched — the harvest confirmed research is still
    // running, so there is nothing to consume yet.
    expect(room.chat.$groupChats.get().Grind.stranded?.research).toBe(0)
    expect(room.gateway.calls.filter(call => call.profile === 'builder')).toHaveLength(1)
  })
})

describe('room record', () => {
  it('persists the roomId alongside the room and survives another room’s disband', async () => {
    const room = await loadRoom()

    room.chat.updateGroupChat('Keep', current => {
      current.log = [{ at: 1, from: { kind: 'user', name: 'You' }, id: 'k1', text: 'hi' }]
      current.roomId = 'r-keep'

      return current
    })
    room.chat.updateGroupChat('Gone', current => {
      current.roomId = 'r-gone'

      return current
    })

    const rooms = { ...room.chat.$groupChats.get() }
    delete rooms.Gone
    room.chat.$groupChats.set(rooms)
    await room.chat.persistGroupChatRooms(rooms)

    const durable = (room.gateway.storage.get('group-chats') || {}) as Record<string, GroupChat>

    expect(durable.Keep.roomId).toBe('r-keep')
    expect('Gone' in durable).toBe(false)
  })
})
