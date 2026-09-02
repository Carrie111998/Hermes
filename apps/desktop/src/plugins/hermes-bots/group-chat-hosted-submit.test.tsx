import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { translateBots } from './i18n-test-helper'
import type { Attachment, GroupMember } from './types'

const { host, sendToGroupChatDurably } = vi.hoisted(() => ({
  host: {} as Record<string, unknown>,
  sendToGroupChatDurably: vi.fn(
    async (_group: string, _members: GroupMember[], _text: string, _thread: null | string, images: Attachment[]) => {
      images[0].uploadId = 'stable-upload-id'
      throw new Error('The connected device rejected this action.')
    }
  )
}))

vi.mock('@hermes/plugin-sdk', async () => {
  const { pluginSdkMock } = await import('./group-test-utils')
  const base = await pluginSdkMock(host)

  return {
    ...base,
    Button: (props: React.ComponentProps<'button'>) => <button type={props.type || 'button'} {...props} />,
    cn: (...values: unknown[]) => values.filter(Boolean).join(' '),
    Codicon: ({ name }: { name: string }) => <span aria-hidden data-icon={name} />,
    ConfirmDialog: () => null,
    CopyButton: () => null,
    Dialog: () => null,
    DialogContent: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    DialogDescription: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    DialogFooter: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    DialogHeader: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    DialogTitle: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    Input: (props: React.ComponentProps<'input'>) => <input {...props} />,
    relativeTime: () => 'now',
    RowButton: (props: React.ComponentProps<'button'>) => <button type="button" {...props} />,
    Tip: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
    useI18n: () => ({ t: { common: { cancel: 'Cancel', save: 'Save' } } }),
    usePluginI18n: () => translateBots
  }
})

vi.mock('./group-chat-parts', () => ({
  GroupClarifyCard: () => null,
  GroupImageControls: () => null,
  GroupMentionInput: (props: { 'aria-label'?: string; value?: string }) => (
    <textarea aria-label={props['aria-label']} readOnly value={props.value} />
  )
}))

vi.mock('./group-rounds', () => ({
  sendToGroupChatDurably,
  stopGroupThread: vi.fn()
}))

const MEMBERS: GroupMember[] = [
  { connectionId: 'gateway-a', name: 'research', sourceScoped: true, targetProfile: 'research' },
  { connectionId: 'gateway-a', name: 'builder', sourceScoped: true, targetProfile: 'builder' }
]

beforeEach(() => {
  vi.resetModules()
  Object.assign(host, { notify: vi.fn() })
  Object.defineProperty(Element.prototype, 'scrollIntoView', {
    configurable: true,
    value: vi.fn()
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('hosted Group Chat composer durability', () => {
  it('restores text, attachments, and retry upload identity after terminal rejection', async () => {
    const [{ GroupChatWorkspace }, chat, panes] = await Promise.all([
      import('./group-chat-view'),
      import('./group-chat'),
      import('./group-panes')
    ])

    const room = {
      continuityMode: 'gateway' as const,
      hosted: 'install:home',
      hostedConnectionId: 'gateway-a',
      hostedEpoch: 1,
      hostedSeq: 0,
      hostedStatus: { label: 'Ready', state: 'ready' },
      log: [],
      members: MEMBERS,
      roomId: 'room-1',
      watermarks: {}
    }

    const key = panes.groupComposerDraftKey('Core', room)

    chat.$groupChats.set({ Core: room })
    panes.updateGroupComposerDraft(key, draft => ({
      ...draft,
      main: 'Review this file',
      pendingAttachments: {
        ...draft.pendingAttachments,
        main: [{ data: 'data:image/png;base64,YQ==', kind: 'image', name: 'proof.png' }]
      }
    }))
    render(<GroupChatWorkspace group="Core" members={MEMBERS} />)

    fireEvent.click(screen.getByRole('button', { name: 'New Thread' }))
    await waitFor(() => expect(sendToGroupChatDurably).toHaveBeenCalledTimes(1))
    await waitFor(() => expect(panes.groupComposerDraftSnapshot(key).main).toBe('Review this file'))

    expect(panes.groupComposerDraftSnapshot(key).pendingAttachments.main).toEqual([
      expect.objectContaining({ name: 'proof.png', uploadId: 'stable-upload-id' })
    ])
    expect(chat.$groupChats.get().Core.log).toEqual([])
    expect(host.notify).toHaveBeenCalledWith(
      expect.objectContaining({ kind: 'error', message: 'The connected device rejected this action.' })
    )
  })
})
