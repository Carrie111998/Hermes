import { act, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { $profiles } from '@/store/profile'
import { $projects } from '@/store/projects'
import type { ProfileInfo } from '@/types/hermes'

import { GroupsView } from '.'

const profile = (name: string, displayName?: string): ProfileInfo => ({
  display_name: displayName,
  has_env: true,
  is_default: name === 'default',
  model: null,
  name,
  path: `/profiles/${name}`,
  provider: null,
  skill_count: 0
})

describe('GroupsView', () => {
  beforeEach(() => {
    $projects.set([])
    $profiles.set([])
    HTMLElement.prototype.scrollIntoView = vi.fn()
    HTMLElement.prototype.animate = vi.fn(() => ({ cancel: vi.fn(), finished: Promise.resolve() }) as unknown as Animation)
  })

  it('does not require a view-local gateway event listener', async () => {
    render(<GroupsView navigate={vi.fn()} request={vi.fn(async () => ({ rooms: [] }))} roomId={null} />)
    await screen.findByText('No group rooms yet.')
  })

  it('selects existing profiles by display name and sends canonical names', async () => {
    $profiles.set([
      profile('default', 'Hermes'),
      profile('planner', 'Launch Planner'),
      profile('reviewer', 'Risk Reviewer')
    ])

    const request = vi.fn(async (method: string) => method === 'group.room.list'
      ? { rooms: [] }
      : method === 'group.room.create'
        ? { room: { id: 'r-picker', name: 'Launch', profiles: ['planner'], messages: [] } }
        : { ok: true })

    render(<GroupsView navigate={vi.fn()} request={request} roomId={null} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create room' }))
    fireEvent.change(screen.getByLabelText('Room name'), { target: { value: 'Launch' } })
    fireEvent.change(screen.getByRole('textbox', { name: 'Search profiles' }), { target: { value: 'launch' } })
    fireEvent.click(screen.getByRole('checkbox', { name: /Launch Planner.*planner/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))

    await waitFor(() => expect(request).toHaveBeenCalledWith('group.room.create', {
   name: 'Launch', profiles: ['planner'], trigger_tokens: 128000, max_history_tokens: 96000, tail_message_count: 20
 }))
 })

 it('configures room compression limits and shows the effective policy', async () => {
 $profiles.set([profile('planner')])

 const request = vi.fn(async (method: string) => method === 'group.room.list'
   ? { rooms: [] }
   : method === 'group.room.create'
     ? { room: {
       id: 'r-context', name: 'Context', profiles: ['planner'], messages: [],
       trigger_tokens: 64000, max_history_tokens: 40000, tail_message_count: 20
     } }
     : { ok: true })

 const navigate = vi.fn()

 const { unmount } = render(<GroupsView navigate={navigate} request={request} roomId={null} />)
 fireEvent.click(await screen.findByRole('button', { name: 'Create room' }))
 fireEvent.change(screen.getByLabelText('Room name'), { target: { value: 'Context' } })
 fireEvent.click(screen.getByRole('checkbox', { name: 'planner (planner)' }))
 fireEvent.change(screen.getByLabelText('Compression trigger tokens'), { target: { value: '64000' } })
 fireEvent.change(screen.getByLabelText('Recent history token budget'), { target: { value: '40000' } })
 fireEvent.change(screen.getByLabelText('Recent messages to keep'), { target: { value: '20' } })
 fireEvent.click(screen.getByRole('button', { name: 'Create' }))

 await waitFor(() => expect(request).toHaveBeenCalledWith('group.room.create', {
   name: 'Context', profiles: ['planner'], trigger_tokens: 64000, max_history_tokens: 40000, tail_message_count: 20
 }))
 unmount()

 render(<GroupsView navigate={navigate} request={async method => method === 'group.room.get'
   ? { room: {
     id: 'r-context', name: 'Context', profiles: ['planner'], messages: [],
     trigger_tokens: 64000, max_history_tokens: 40000, tail_message_count: 20
   } }
   : { ok: true }} roomId="r-context" />)
 expect(await screen.findByText(/64,000/)).toBeTruthy()
 expect(screen.getByText(/40,000/)).toBeTruthy()
 expect(screen.getByText(/20 messages/)).toBeTruthy()
 })

  it('selects a project workspace for creation and shows it in room details', async () => {
    $profiles.set([profile('planner')])
    $projects.set([{
      id: 'p_launch', slug: 'launch', name: 'Launch project', description: null, icon: null, color: null,
      board_slug: null, primary_path: '/work/launch', archived: false, created_at: 1,
      folders: [{ path: '/work/launch', label: null, is_primary: true, added_at: 1 }]
    }])

    const request = vi.fn(async (method: string) => method === 'group.room.list'
      ? { rooms: [] }
      : method === 'group.room.create'
        ? { room: { id: 'r-work', name: 'Launch', profiles: ['planner'], workspace: '/work/launch', messages: [] } }
        : { ok: true })

    const { unmount } = render(<GroupsView navigate={vi.fn()} request={request} roomId={null} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create room' }))
    fireEvent.change(screen.getByLabelText('Room name'), { target: { value: 'Launch' } })
    fireEvent.click(screen.getByRole('checkbox', { name: 'planner (planner)' }))
    fireEvent.click(screen.getByLabelText('Workspace'))
    fireEvent.click(await screen.findByRole('option', { name: /Launch project.*\/work\/launch/ }))
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(request).toHaveBeenCalledWith('group.room.create', {
      name: 'Launch', profiles: ['planner'], workspace: '/work/launch',
      trigger_tokens: 128000, max_history_tokens: 96000, tail_message_count: 20
    }))
    unmount()

    render(<GroupsView navigate={vi.fn()} request={async method => method === 'group.room.get'
      ? { room: { id: 'r-work', name: 'Launch', profiles: ['planner'], workspace: '/work/launch', messages: [] } }
      : { ok: true }} roomId="r-work" />)
    expect(await screen.findByText('/work/launch')).toBeTruthy()
  })

  it('shows a workspace selected from the folder browser even when it is not a saved project', async () => {
    $profiles.set([profile('planner')])
    let resolveWorkspace!: (path: string) => void
    const pickWorkspace = vi.fn(() => new Promise<string>(resolve => {resolveWorkspace = resolve}))

    render(<GroupsView navigate={vi.fn()} pickWorkspace={pickWorkspace} request={vi.fn(async () => ({ rooms: [] }))} roomId={null} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Create room' }))
    fireEvent.click(screen.getByRole('button', { name: 'Browse…' }))

    await waitFor(() => expect(pickWorkspace).toHaveBeenCalledOnce())
    await act(async () => {resolveWorkspace('/work/custom')})
    expect(screen.getByRole('combobox', { name: 'Workspace' }).textContent).toContain('/work/custom')
  })

  it('loads older history with cursor pagination and shows compression context', async () => {
    const request = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method !== 'group.room.get') {return { ok: true }}

      if (params?.cursor === 'older-1') {
        return { room: { id: 'r-page', name: 'Paged', profiles: [], messages: [
          { id: 'm1', seq: 1, role: 'user', content: 'Oldest' },
          { id: 'm2', seq: 2, role: 'assistant', content: 'Boundary' }
        ] }, has_more: false }
      }

      return { room: {
        id: 'r-page', name: 'Paged', profiles: [], context_status: 'compressed', compression_count: 3, summary: 'Earlier context summary',
        messages: [{ id: 'm2', seq: 2, role: 'assistant', content: 'Boundary' }, { id: 'm3', seq: 3, role: 'assistant', content: 'Latest' }]
      }, cursor: 'older-1', has_more: true }
    })

    render(<GroupsView navigate={vi.fn()} request={request} roomId="r-page" />)
    fireEvent.click(await screen.findByRole('button', { name: /Context summary/ }))
    expect(screen.getByText('Compressed 3 times')).toBeTruthy()
    expect(await screen.findByText('Earlier context summary')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Load earlier messages' }))
    expect(await screen.findByText('Oldest')).toBeTruthy()
    expect(screen.getAllByText('Boundary')).toHaveLength(1)
    expect(request).toHaveBeenCalledWith('group.room.get', { room_id: 'r-page', cursor: 'older-1', before_seq: 2 })
  })

  it('creates a multi-profile room, sends @all, and stops the room', async () => {
    $profiles.set([profile('planner'), profile('reviewer')])

    const request = vi.fn(async (method: string) => {
      if (method === 'group.room.list') {return { rooms: [] }}

      if (method === 'group.room.create') {return { room: { id: 'r1', name: 'Launch', profiles: ['planner', 'reviewer'], messages: [] } }}

      if (method === 'group.room.get') {return { room: { id: 'r1', name: 'Launch', profiles: ['planner', 'reviewer'], messages: [], running: true } }}

      return { ok: true }
    })

    const navigate = vi.fn()
    render(<GroupsView navigate={navigate} request={request} roomId={null} />)

    fireEvent.click(await screen.findByRole('button', { name: 'Create room' }))
    fireEvent.change(screen.getByLabelText('Room name'), { target: { value: 'Launch' } })
    fireEvent.click(screen.getByRole('checkbox', { name: 'planner (planner)' }))
    fireEvent.click(screen.getByRole('checkbox', { name: 'reviewer (reviewer)' }))
    fireEvent.click(screen.getByRole('button', { name: 'Create' }))
    await waitFor(() => expect(navigate).toHaveBeenCalledWith('/groups/r1'))

    render(<GroupsView navigate={navigate} request={request} roomId="r1" />)
    const composer = await screen.findByLabelText('Message the room')
    fireEvent.change(composer, { target: { value: '@all ship it' } })
    fireEvent.click(screen.getByRole('button', { name: 'Send' }))
    await waitFor(() => expect(request).toHaveBeenCalledWith('group.message.send', {
      room_id: 'r1', content: '@all ship it', mentions: ['all']
    }))
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }))
    await waitFor(() => expect(request).toHaveBeenCalledWith('group.run.interrupt', { room_id: 'r1' }))
  })

  it('shows room agents after @ and highlights the selected mention', async () => {
    const request = vi.fn(async (method: string) => method === 'group.room.get'
      ? { room: { id: 'r-mentions', name: 'Mentions', profiles: ['planner', 'reviewer'], messages: [] } }
      : { ok: true })

    render(<GroupsView navigate={vi.fn()} request={request} roomId="r-mentions" />)
    const composer = await screen.findByLabelText('Message the room')
    fireEvent.change(composer, { target: { selectionStart: 1, value: '@' } })

    expect(screen.getByRole('option', { name: '@planner' })).toBeTruthy()
    expect(screen.getByRole('option', { name: '@reviewer' })).toBeTruthy()
    fireEvent.click(screen.getByRole('option', { name: '@planner' }))

    expect((composer as HTMLTextAreaElement).value).toBe('@planner ')
    expect(composer.parentElement?.querySelector('[data-group-mention]')).toBeNull()
    expect((composer as HTMLTextAreaElement).className).not.toContain('text-transparent')
    expect((composer as HTMLTextAreaElement).className).not.toContain('desktop-input-chrome')
    expect((composer as HTMLTextAreaElement).value).toBe('@planner ')
  })

  it('shows profile Chinese display names while keeping canonical mention ids', async () => {
    $profiles.set([
      profile('architect', '技术架构师'),
      profile('requirements', '需求分析师')
    ])

    const request = vi.fn(async (method: string) => method === 'group.room.get'
      ? { room: { id: 'r-zh-profiles', name: '中文名称', profiles: ['architect', 'requirements'], messages: [] } }
      : { ok: true })

    render(<GroupsView navigate={vi.fn()} request={request} roomId="r-zh-profiles" />)
    const composer = await screen.findByLabelText('Message the room')
    expect(await screen.findByText('技术架构师')).toBeTruthy()
    expect(screen.getByText('需求分析师')).toBeTruthy()

    fireEvent.change(composer, { target: { selectionStart: 2, value: '@技' } })
    fireEvent.click(screen.getByRole('option', { name: '@architect 技术架构师' }))
    expect((composer as HTMLTextAreaElement).value).toBe('@architect ')
  })

  it('uses Enter to accept a mention, Enter to send, and Shift+Enter for a newline', async () => {
    const request = vi.fn(async (method: string) => method === 'group.room.get'
      ? { room: { id: 'r-keys', name: 'Keys', profiles: ['planner', 'reviewer'], messages: [] } }
      : { ok: true })

    render(<GroupsView navigate={vi.fn()} request={request} roomId="r-keys" />)
    const composer = await screen.findByLabelText('Message the room')
    fireEvent.change(composer, { target: { selectionStart: 2, value: '@p' } })

    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false)
    expect((composer as HTMLTextAreaElement).value).toBe('@planner ')
    expect(request).not.toHaveBeenCalledWith('group.message.send', expect.anything())

    fireEvent.change(composer, { target: { selectionStart: 15, value: '@planner hello' } })
    expect(fireEvent.keyDown(composer, { key: 'Enter', shiftKey: true })).toBe(true)
    expect(request).not.toHaveBeenCalledWith('group.message.send', expect.anything())

    expect(fireEvent.keyDown(composer, { key: 'Enter' })).toBe(false)
    await waitFor(() => expect(request).toHaveBeenCalledWith('group.message.send', {
      room_id: 'r-keys', content: '@planner hello', mentions: ['planner']
    }))
  })

  it('lets the user drag the message composer vertically without an expand button', async () => {
    const request = vi.fn(async (method: string) => method === 'group.room.get'
      ? { room: { id: 'r-expand', name: 'Expand', profiles: ['planner'], messages: [] } }
      : { ok: true })

    render(<GroupsView navigate={vi.fn()} request={request} roomId="r-expand" />)
    const composer = await screen.findByLabelText('Message the room')
    expect((composer as HTMLTextAreaElement).className).toContain('resize-y')
    expect((composer as HTMLTextAreaElement).className).toContain('max-h-[60dvh]')
    expect(screen.queryByRole('button', { name: 'Expand composer' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Collapse composer' })).toBeNull()
  })

  it('renders agent tool calls inside an assistant-ui runtime instead of crashing the workspace', async () => {
    const request = vi.fn(async (method: string) => method === 'group.room.get'
      ? { room: { id: 'r-tools', name: 'Tools', profiles: ['planner'], messages: [{
        id: 'm-tool', role: 'assistant', profile: 'planner', status: 'streaming', content: '', tools: [{
          toolCallId: 'tool-1', toolName: 'terminal', args: { command: 'pwd' }, status: 'running'
        }]
      }] } }
      : { ok: true })

    render(<GroupsView navigate={vi.fn()} request={request} roomId="r-tools" />)

    expect(await screen.findByText(/Running/i)).toBeTruthy()
  })

  it('copies a user question and confirms restore-and-rerun from its checkpoint', async () => {
    const copyText = vi.fn().mockResolvedValue(undefined)

    const request = vi.fn(async (method: string) => method === 'group.room.get'
      ? { room: { id: 'r-actions', name: 'Actions', profiles: ['planner'], messages: [
        { id: 'u1', seq: 4, role: 'user', status: 'complete', content: '@planner design an app' },
        { id: 'a1', seq: 5, role: 'assistant', profile: 'planner', status: 'complete', content: 'old answer' }
      ] } }
      : method === 'group.message.rewind'
        ? { room: { id: 'r-actions', name: 'Actions', profiles: ['planner'], messages: [
          { id: 'u2', seq: 6, role: 'user', status: 'complete', content: '@planner design an app' }
        ] } }
        : { ok: true })

    render(<GroupsView copyText={copyText} navigate={vi.fn()} request={request} roomId="r-actions" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Copy question' }))
    expect(copyText).toHaveBeenCalledWith('@planner design an app')

    fireEvent.click(screen.getByRole('button', { name: 'Restore checkpoint' }))
    expect(await screen.findByText('Restore to this checkpoint?')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Restore & rerun' }))

    await waitFor(() => expect(request).toHaveBeenCalledWith('group.message.rewind', {
      room_id: 'r-actions', seq: 4, content: '@planner design an app'
    }))
    expect(await screen.findByText('@planner design an app')).toBeTruthy()
    expect(screen.queryByText('old answer')).toBeNull()
  })

  it('shows an agent clarification question and sends the selected answer', async () => {
    const request = vi.fn(async (method: string) => method === 'group.room.get'
      ? { room: { id: 'r-clarify', name: 'Clarify', profiles: ['planner'], messages: [{
        id: 'm1', role: 'assistant', profile: 'planner', status: 'clarify', content: '',
        runtime_session_id: 'runtime-1', clarify: {
          request_id: 'ask-1', question: 'Which platform?', choices: ['iOS', 'Android']
        }
      }] } }
      : { ok: true })

    render(<GroupsView navigate={vi.fn()} request={request} roomId="r-clarify" />)
    expect(await screen.findByText('Which platform?')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'iOS' }))
    fireEvent.click(screen.getByRole('button', { name: 'Continue' }))

    await waitFor(() => expect(request).toHaveBeenCalledWith('clarify.respond', {
      request_id: 'ask-1', answer: 'iOS'
    }))
  })
})
