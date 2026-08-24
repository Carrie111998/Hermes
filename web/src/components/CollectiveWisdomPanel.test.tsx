// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const {
  getWisdomStatus,
  getWisdomDiscovery,
  getWisdomCandidates,
  getWisdomDrafts,
  getWisdomSkill,
  suggestWisdomSkill,
  reviewWisdomDraft,
  decideWisdomDraft,
} = vi.hoisted(() => ({
  getWisdomStatus: vi.fn(),
  getWisdomDiscovery: vi.fn(),
  getWisdomCandidates: vi.fn(),
  getWisdomDrafts: vi.fn(),
  getWisdomSkill: vi.fn(),
  suggestWisdomSkill: vi.fn(),
  reviewWisdomDraft: vi.fn(),
  decideWisdomDraft: vi.fn(),
}));

vi.mock('@/lib/api', () => ({
  api: {
    decideWisdomDraft,
    getWisdomCandidates,
    getWisdomDiscovery,
    getWisdomDrafts,
    getWisdomSkill,
    getWisdomStatus,
    reviewWisdomDraft,
    suggestWisdomSkill
  }
}))

import { CollectiveWisdomPanel } from './CollectiveWisdomPanel'

beforeEach(() => {
  getWisdomStatus.mockResolvedValue({ verified_org_id: 'org-1' })
  getWisdomCandidates.mockResolvedValue({ candidates: [] })
  getWisdomDrafts.mockResolvedValue({ drafts: [] })
  getWisdomDiscovery.mockResolvedValue({ next_cursor: null, skills: [] })
})

afterEach(() => vi.clearAllMocks())

describe('CollectiveWisdomPanel', () => {
  it('escapes server-controlled text and labels server scan state explicitly', async () => {
    getWisdomDiscovery.mockResolvedValue({
      next_cursor: null,
      skills: [
        {
          id: 'skill-1',
          slug: '<img src=x onerror=alert(1)>',
          author_description: '<script>window.pwned=true</script>',
          latest_version: 1,
          install_count: 0,
          state: 'active'
        }
      ]
    })
    render(<CollectiveWisdomPanel profile="research" />)
    expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeTruthy()
    expect(screen.getByText('server scan passed')).toBeTruthy()
    expect(document.querySelector('script')).toBeNull()
  })

  it('keeps preparation local until explicit owner copy and System Specification submission', async () => {
    getWisdomCandidates.mockResolvedValue({
      candidates: [
        {
          local_skill_id: 'local-1',
          name: 'candidate-skill',
          eligibility: 'eligible',
          reason: null
        }
      ]
    })
    suggestWisdomSkill
      .mockResolvedValueOnce({
        network_submission: false,
        local_draft_id: 'local-1',
        overlay_path: '/private/overlay',
        drafted_description: 'Drafted copy',
        system_specification: { hermes: { minimum_version: '0.17.0' } },
        next_step: 'review'
      })
      .mockResolvedValueOnce({ draft: { id: 'draft-1' } })

    render(<CollectiveWisdomPanel profile="research" />)
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare' }))
    fireEvent.change(await screen.findByLabelText('Owner-authored description'), {
      target: { value: 'Owner approved' }
    })
    fireEvent.click(screen.getByRole('button', { name: 'Submit for owner-only server review' }))

    await waitFor(() => expect(suggestWisdomSkill).toHaveBeenCalledTimes(2))
    expect(suggestWisdomSkill.mock.calls[1]).toEqual([
      'candidate-skill',
      'research',
      'Owner approved',
      { hermes: { minimum_version: '0.17.0' } }
    ])
    expect(JSON.stringify(suggestWisdomSkill.mock.calls[1])).not.toMatch(/usage|refinement|ranking|stability/)
  })
})
