// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'

const getWisdomEvents = vi.fn()
const suggestWisdomSkill = vi.fn()
const reviewWisdomDraft = vi.fn()
const decideWisdomDraft = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  decideWisdomDraft,
  getWisdomEvents,
  reviewWisdomDraft,
  suggestWisdomSkill
}))

vi.mock('@/store/notifications', () => ({ notifyError: vi.fn() }))

const candidate = {
  id: 'event-1',
  kind: 'wisdom.candidate',
  session_id: 'session-1',
  task_id: 'task-1',
  content_hash: 'sha256:local',
  payload: {
    skill_name: 'safe-skill',
    qualification: 'refinement',
    local_reasons: { meaningful_refinements: 3 },
    consent_required: true,
    networked: false
  }
}

async function renderCard() {
  const { WisdomCandidateCard } = await import('./wisdom-candidate-card')

  return render(<WisdomCandidateCard profile="research" sessionId="session-1" />)
}

afterEach(() => vi.clearAllMocks())

describe('WisdomCandidateCard', () => {
  it('hydrates a durable session event without inventing an expiry or publisher device', async () => {
    getWisdomEvents.mockResolvedValue({ events: [candidate] })
    await renderCard()
    expect(await screen.findByText('safe-skill')).toBeTruthy()
    expect(screen.getByText(/meaningful_refinements/)).toBeTruthy()
    expect(screen.queryByText(/expires/i)).toBeNull()
    expect(screen.queryByText(/publisher device|from device/i)).toBeNull()
  })

  it('requires prepare, explicit submit, complete review, and a fresh receipt before approval', async () => {
    getWisdomEvents.mockResolvedValue({ events: [candidate] })
    suggestWisdomSkill
      .mockResolvedValueOnce({
        network_submission: false,
        local_draft_id: 'local-1',
        overlay_path: '/private/overlay',
        drafted_description: 'Owner copy',
        system_specification: { hermes: { minimum_version: '0.17.0' } },
        next_step: 'review'
      })
      .mockResolvedValueOnce({
        draft: { id: 'draft-1' },
        local_scan: {
          guard: { allowed: true, findings: [] },
          skill_evaluator: { status: 'available', findings: [] }
        },
        notice: 'owner private'
      })
    reviewWisdomDraft
      .mockResolvedValueOnce({
        draft: {
          id: 'draft-1',
          slug: 'safe-skill',
          authorDescription: 'Owner-authored claim',
          scanVerdict: 'pass',
          scan: { verdict: 'pass' },
          explanation: 'Server facts only',
          systemSpec: { runtime: { sandbox: true } }
        },
        effective_policy: {},
        files: [
          { path: 'SKILL.md', mode: 'file', hash: 'sha256:file', content_utf8: '# Safe' },
          { path: 'skill.manifest.json', mode: 'file', hash: 'sha256:manifest', content_utf8: '{}' }
        ],
        hashes: { content: 'sha256:content', author_description: 'sha256:copy', package_manifest: 'sha256:manifest' },
        receipt: null
      })
      .mockResolvedValueOnce({ receipt: 'receipt-1' })
    decideWisdomDraft.mockResolvedValue({ ok: true })

    await renderCard()
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare exact package' }))
    fireEvent.click(await screen.findByRole('button', { name: 'Send for owner-only server review' }))
    expect(await screen.findByText(/sha256:content/)).toBeTruthy()
    expect(screen.getByText(/server enforced: pass/)).toBeTruthy()
    expect(screen.getByText('Owner-authored claim')).toBeTruthy()
    expect(screen.getByText(/"status": "available"/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Rendered' }))
    expect(screen.getByText('Safe')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Raw' }))
    fireEvent.click(screen.getByRole('button', { name: 'Approve & publish' }))

    await waitFor(() => expect(decideWisdomDraft).toHaveBeenCalledWith('draft-1', 'approve', 'research'))
    expect(reviewWisdomDraft.mock.calls[1].slice(0, 2)).toEqual(['draft-1', true])
  })
})
