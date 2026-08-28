import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const source = readFileSync(new URL('../plugin.js', import.meta.url), 'utf8')

function between(start, end) {
  const from = source.indexOf(start)
  const to = source.indexOf(end, from)

  assert.notEqual(from, -1, `missing ${start}`)
  assert.notEqual(to, -1, `missing ${end}`)

  return source.slice(from, to)
}

function load() {
  const context = { Map, Object }
  const roomKey = between('function groupChatRoomKey(', '/** Lift any historical projection shape')
  const drafts = between('const groupComposerDrafts = new Map()', 'function GroupChatWorkspace(')

  vm.runInNewContext(
    `${roomKey}\n${drafts}\nglobalThis.drafts = {
      clearGroupComposerDraft,
      completeGroupComposerSend,
      groupComposerDraftKey,
      groupComposerDraftSnapshot,
      migrateGroupComposerDraft,
      updateGroupComposerDraft
    }`,
    context
  )

  return context.drafts
}

test('workspace retirement and re-registration restore the exact room draft', () => {
  const drafts = load()
  const key = drafts.groupComposerDraftKey('Launch room', { roomId: 'room-1' })
  const attachment = { data: 'data:image/png;base64,abc', kind: 'image', name: 'plan.png' }

  drafts.updateGroupComposerDraft(key, state => ({
    ...state,
    activeReplyThread: 'thread-1',
    main: 'main draft',
    pendingAttachments: { main: [attachment], 'thread-1': [attachment] },
    replies: { 'thread-1': 'reply draft' }
  }))

  // Dropping the component reference simulates pane retirement. A fresh
  // registration reads the same module-scope, roomId-qualified snapshot.
  const remounted = drafts.groupComposerDraftSnapshot(key)

  assert.equal(remounted.main, 'main draft')
  assert.equal(remounted.replies['thread-1'], 'reply draft')
  assert.equal(remounted.activeReplyThread, 'thread-1')
  assert.equal(remounted.pendingAttachments.main[0].name, 'plan.png')
})

test('legacy name-keyed drafts migrate when an immutable room id appears', () => {
  const drafts = load()
  const legacy = drafts.groupComposerDraftKey('Launch room', {})
  const current = drafts.groupComposerDraftKey('Renamed room', { roomId: 'room-1' })

  drafts.updateGroupComposerDraft(legacy, state => ({ ...state, main: 'keep me' }))
  drafts.migrateGroupComposerDraft(legacy, current)

  assert.equal(drafts.groupComposerDraftSnapshot(current).main, 'keep me')
  assert.equal(drafts.groupComposerDraftSnapshot(legacy).main, '')
})

test('a send can detect newer typing without clearing the original draft first', () => {
  const drafts = load()
  const key = 'id:room-1'

  drafts.updateGroupComposerDraft(key, state => ({ ...state, main: 'send this' }))
  const before = drafts.groupComposerDraftSnapshot(key)
  drafts.updateGroupComposerDraft(key, state => ({ ...state, main: 'newer typing' }))

  assert.notEqual(drafts.groupComposerDraftSnapshot(key).revision, before.revision)
  assert.equal(drafts.groupComposerDraftSnapshot(key).main, 'newer typing')
})

test('successful upload clears only submitted files while preserving newer typing and files', () => {
  const drafts = load()
  const key = 'id:room-1'
  const submitted = { name: 'plan.pdf' }
  const newer = { name: 'notes.txt' }
  drafts.updateGroupComposerDraft(key, state => ({
    ...state,
    main: 'send this',
    pendingAttachments: { main: [submitted] }
  }))
  const before = drafts.groupComposerDraftSnapshot(key)
  drafts.updateGroupComposerDraft(key, state => ({
    ...state,
    main: 'send this and keep my newer edit',
    pendingAttachments: { main: [submitted, newer] }
  }))

  const completed = drafts.completeGroupComposerSend(key, before, [submitted])

  assert.equal(completed.main, 'send this and keep my newer edit')
  assert.equal(completed.pendingAttachments.main.map(item => item.name).join(','), 'notes.txt')
})

test('disband removes only that room draft', () => {
  const drafts = load()

  drafts.updateGroupComposerDraft('id:a', state => ({ ...state, main: 'a' }))
  drafts.updateGroupComposerDraft('id:b', state => ({ ...state, main: 'b' }))
  drafts.clearGroupComposerDraft('id:a')

  assert.equal(drafts.groupComposerDraftSnapshot('id:a').main, '')
  assert.equal(drafts.groupComposerDraftSnapshot('id:b').main, 'b')
})

test('GroupChatWorkspace owns composer state through the room draft store', () => {
  const workspace = between('function GroupChatWorkspace(', '/** Live closers for group-chat MAIN-window tabs')

  assert.match(workspace, /groupComposerDraftKey\(group, room\)/)
  assert.match(workspace, /await Promise\.resolve\(/)
  assert.match(workspace, /completeGroupComposerSend\(composerKeyRef\.current, before, images/)
  assert.match(workspace, /sendingComposer === 'main' \? 'Sending…' : 'New Thread'/)
  assert.match(workspace, /clearGroupComposerDraft\(composerKeyRef\.current\)/)
  assert.doesNotMatch(workspace, /useState\(''\).*?draft/)
  assert.doesNotMatch(workspace, /useState\(\{\}\).*?replyDrafts/)
})
