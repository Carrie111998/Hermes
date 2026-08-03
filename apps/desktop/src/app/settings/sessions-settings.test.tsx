// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import {
  JOURNAL_PERSIST_THROTTLE_MS,
  JOURNAL_STORAGE_KEY,
  persistInFlightTurnState,
  readInFlightTurnJournal
} from '@/lib/inflight-turn-journal'
import { enqueueQueuedPrompt, getQueuedPrompts } from '@/store/composer-queue'
import type { SessionInfo } from '@/types/hermes'

import { SessionsSettings } from './sessions-settings'

/**
 * The archived-sessions delete is the SECOND delete surface. Its journal purge
 * and its queued-prompt clear are call-site wiring: the journal module tests
 * call `purgeInFlightTurnJournals` directly and `composer-queue` tests call
 * `clearQueuedPrompts` directly, so both can be deleted from this component with
 * every other suite still green. That is exactly how the original defect shipped
 * — the sidebar delete cleared the composer queue, this one cleared neither.
 *
 * Both stores are keyed on either the stored tip or the durable lineage root
 * (`resolveComposerSessionKey`), so both ids have to be cleared, and both are
 * asserted here.
 */
const mocks = vi.hoisted(() => ({
  deleteSession: vi.fn(),
  listAllProfileSessions: vi.fn(),
  notifyError: vi.fn(),
  setSessionArchived: vi.fn()
}))

// Partial mock: `@/store/profile` subscribes to `setApiRequestProfile` at import
// time, so a bare factory drops real exports the store graph needs.
vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  deleteSession: (id: string, profile?: null | string) => mocks.deleteSession(id, profile),
  getHermesConfigRecord: vi.fn().mockResolvedValue({}),
  listAllProfileSessions: (limit: number, offset: number, archived: string) =>
    mocks.listAllProfileSessions(limit, offset, archived),
  saveHermesConfig: vi.fn().mockResolvedValue(undefined),
  setSessionArchived: (id: string, archived: boolean, profile?: null | string) =>
    mocks.setSessionArchived(id, archived, profile)
}))

vi.mock('@/store/notifications', () => ({
  notify: vi.fn(),
  notifyError: (...args: unknown[]) => mocks.notifyError(...args)
}))

vi.mock('@/i18n', () => ({
  useI18n: () => ({
    t: {
      settings: {
        sessions: {
          archivedIntro: 'Archived chats',
          archivedTitle: 'Archived',
          autoArchiveDaysLabel: 'Days',
          autoArchiveDaysUnit: 'days',
          autoArchiveDesc: 'Hide old chats',
          autoArchiveFailed: 'Auto-archive failed',
          autoArchiveTitle: 'Auto-archive',
          change: 'Change',
          choose: 'Choose',
          clear: 'Clear',
          clearDirFailed: 'Clear failed',
          defaultDirDesc: 'Default folder',
          defaultDirTitle: 'Default project folder',
          defaultDirUpdated: 'Updated',
          defaultsTo: (label: string) => `Defaults to ${label}`,
          deleteConfirm: (title: string) => `Delete ${title}?`,
          deleteFailed: 'Delete failed',
          deletePermanently: 'Delete permanently',
          emptyArchivedDesc: 'No archived chats',
          emptyArchivedTitle: 'Nothing archived',
          failedLoad: 'Load failed',
          loading: 'Loading',
          messages: (count: number) => `${count} messages`,
          notSet: 'Not set',
          restored: 'Restored',
          unarchive: 'Unarchive',
          unarchiveFailed: 'Unarchive failed',
          updateDirFailed: 'Update failed'
        }
      }
    }
  })
}))

const STORED_ID = 'stored-archived'
const LINEAGE_ROOT_ID = 'root-archived'
const SECRET_PROMPT = 'PGPASSWORD=hunter2 psql -h prod'

function archivedSession(overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    _lineage_root_id: LINEAGE_ROOT_ID,
    archived: true,
    ended_at: null,
    id: STORED_ID,
    input_tokens: 0,
    is_active: false,
    last_active: 1,
    message_count: 2,
    model: null,
    output_tokens: 0,
    preview: 'archived preview',
    source: 'desktop',
    started_at: 1,
    title: 'Archived chat',
    tool_call_count: 0,
    ...overrides
  } as SessionInfo
}

/** Journal a live turn the way `useSessionStateCache` does, then let the
 *  throttle land so the unredacted tail is genuinely on disk. */
function journalTail(storedSessionId: string): void {
  vi.useFakeTimers()
  persistInFlightTurnState({
    awaitingResponse: false,
    busy: true,
    messages: [
      { id: 'u1', role: 'user', parts: [{ type: 'text', text: SECRET_PROMPT }] },
      { id: 'assistant-stream-1', role: 'assistant', parts: [{ type: 'text', text: 'working' }], pending: true }
    ],
    storedSessionId,
    streamId: 'assistant-stream-1',
    turnStartedAt: 1
  })
  vi.advanceTimersByTime(JOURNAL_PERSIST_THROTTLE_MS)
  vi.useRealTimers()
}

async function deleteArchivedRow(): Promise<void> {
  render(
    <MemoryRouter>
      <SessionsSettings />
    </MemoryRouter>
  )

  const button = await screen.findByLabelText('Delete permanently')
  fireEvent.click(button)
  await waitFor(() => expect(mocks.deleteSession).toHaveBeenCalledWith(STORED_ID, undefined))
}

describe('SessionsSettings archived delete purges local session state', () => {
  beforeEach(() => {
    window.localStorage.clear()
    mocks.deleteSession.mockReset().mockResolvedValue(undefined)
    mocks.listAllProfileSessions.mockReset().mockResolvedValue({ sessions: [archivedSession()] })
    mocks.notifyError.mockReset()
    vi.spyOn(window, 'confirm').mockReturnValue(true)
  })

  afterEach(() => {
    cleanup()
    window.localStorage.clear()
    vi.restoreAllMocks()
  })

  it('clears the journaled in-flight tail for the deleted session', async () => {
    journalTail(STORED_ID)
    expect(readInFlightTurnJournal(STORED_ID)).not.toBeNull()

    await deleteArchivedRow()

    await waitFor(() => expect(readInFlightTurnJournal(STORED_ID)).toBeNull())
    expect(window.localStorage.getItem(JOURNAL_STORAGE_KEY)).toBeNull()
  })

  // The journal keys on the stored tip, but a compression rotation can leave the
  // tail under the lineage root instead, so the delete has to name both.
  it('clears a tail journaled under the lineage root id', async () => {
    journalTail(LINEAGE_ROOT_ID)
    expect(readInFlightTurnJournal(LINEAGE_ROOT_ID)).not.toBeNull()

    await deleteArchivedRow()

    await waitFor(() => expect(readInFlightTurnJournal(LINEAGE_ROOT_ID)).toBeNull())
  })

  // The clear this surface was ALSO missing: the sidebar delete already dropped
  // queued prompts, the settings delete left them holding the user's text.
  it('clears queued prompts for the stored id and the lineage root', async () => {
    enqueueQueuedPrompt(STORED_ID, { attachments: [], text: 'queued under the tip' })
    enqueueQueuedPrompt(LINEAGE_ROOT_ID, { attachments: [], text: 'queued under the root' })
    expect(getQueuedPrompts(STORED_ID)).toHaveLength(1)
    expect(getQueuedPrompts(LINEAGE_ROOT_ID)).toHaveLength(1)

    await deleteArchivedRow()

    await waitFor(() => expect(getQueuedPrompts(STORED_ID)).toHaveLength(0))
    expect(getQueuedPrompts(LINEAGE_ROOT_ID)).toHaveLength(0)
  })

  it('leaves an unrelated session journal and queue intact', async () => {
    journalTail('stored-other')
    enqueueQueuedPrompt('stored-other', { attachments: [], text: 'someone else' })

    await deleteArchivedRow()

    await waitFor(() => expect(mocks.deleteSession).toHaveBeenCalled())
    expect(readInFlightTurnJournal('stored-other')).not.toBeNull()
    expect(getQueuedPrompts('stored-other')).toHaveLength(1)
  })

  // A failed delete keeps the row, so the local copies it would recover from
  // must survive too.
  it('keeps the journal and queue when the delete RPC fails', async () => {
    mocks.deleteSession.mockRejectedValueOnce(new Error('backend down'))
    journalTail(STORED_ID)
    enqueueQueuedPrompt(STORED_ID, { attachments: [], text: 'still queued' })

    await deleteArchivedRow()

    await waitFor(() => expect(mocks.notifyError).toHaveBeenCalled())
    expect(readInFlightTurnJournal(STORED_ID)).not.toBeNull()
    expect(getQueuedPrompts(STORED_ID)).toHaveLength(1)
  })
})
