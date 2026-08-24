// @vitest-environment jsdom
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const {
  getWisdomStatus,
  getWisdomDiscovery,
  getWisdomCandidates,
  getWisdomDrafts,
  getWisdomSkill,
  getWisdomInstallations,
  getWisdomVersionContent,
  planWisdomInstall,
  applyWisdomInstall,
  suggestWisdomSkill,
  reviewWisdomDraft,
  decideWisdomDraft,
  setupWisdom,
  getActionStatus
} = vi.hoisted(() => ({
  getWisdomStatus: vi.fn(),
  getWisdomDiscovery: vi.fn(),
  getWisdomCandidates: vi.fn(),
  getWisdomDrafts: vi.fn(),
  getWisdomSkill: vi.fn(),
  getWisdomInstallations: vi.fn(),
  getWisdomVersionContent: vi.fn(),
  planWisdomInstall: vi.fn(),
  applyWisdomInstall: vi.fn(),
  suggestWisdomSkill: vi.fn(),
  reviewWisdomDraft: vi.fn(),
  decideWisdomDraft: vi.fn(),
  setupWisdom: vi.fn(),
  getActionStatus: vi.fn()
}))

vi.mock('@/lib/api', () => ({
  api: {
    decideWisdomDraft,
    setupWisdom,
    getActionStatus,
    getWisdomCandidates,
    getWisdomDiscovery,
    getWisdomDrafts,
    getWisdomSkill,
    getWisdomInstallations,
    getWisdomVersionContent,
    getWisdomStatus,
    reviewWisdomDraft,
    suggestWisdomSkill,
    planWisdomInstall,
    applyWisdomInstall
  }
}))

import { CollectiveWisdomPanel } from './CollectiveWisdomPanel'

beforeEach(() => {
  getWisdomStatus.mockResolvedValue({ configured: true, verified_org_id: 'org-1' })
  getWisdomCandidates.mockResolvedValue({ candidates: [] })
  getWisdomDrafts.mockResolvedValue({ drafts: [] })
  getWisdomDiscovery.mockResolvedValue({ next_cursor: null, skills: [] })
  getWisdomInstallations.mockResolvedValue({ installations: [], notifications: [] })
})

afterEach(() => vi.clearAllMocks())

describe('CollectiveWisdomPanel', () => {
  it('requires explicit disclosure setup before loading collective data', async () => {
    getWisdomStatus
      .mockResolvedValueOnce({ configured: false, verified_org_id: null })
      .mockResolvedValueOnce({ configured: true, verified_org_id: 'org-1' })
    setupWisdom.mockResolvedValue({ ok: true, name: 'wisdom-setup', pid: 1 })
    getActionStatus.mockResolvedValue({
      name: 'wisdom-setup',
      running: false,
      exit_code: 0,
      pid: 1,
      lines: []
    })

    render(<CollectiveWisdomPanel profile="research" />)
    expect(await screen.findByText(/Candidate qualification stays on this profile/)).toBeTruthy()
    expect(getWisdomDiscovery).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /set up this profile/ }))

    await waitFor(() => expect(setupWisdom).toHaveBeenCalledWith('research'))
    expect(getActionStatus).toHaveBeenCalledWith('wisdom-setup', 80)
    await waitFor(() => expect(getWisdomDiscovery).toHaveBeenCalledWith('research'))
  })

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

  it('shows exact version bytes and applies only a verified install receipt', async () => {
    getWisdomDiscovery.mockResolvedValue({
      next_cursor: null,
      skills: [
        {
          id: 'skill-1',
          slug: 'managed-skill',
          author_description: 'Does work',
          latest_version: 2,
          install_count: 0,
          state: 'active'
        }
      ]
    })
    getWisdomSkill.mockResolvedValue({ skill: { id: 'skill-1', slug: 'managed-skill' }, versions: [{ version: 2 }] })
    getWisdomVersionContent.mockResolvedValue({
      commit: 'sha256:commit',
      content_hash: 'sha256:content',
      files: [{ path: 'SKILL.md', mode: 'file', hash: 'sha256:file', content_utf8: '# Exact bytes' }]
    })
    planWisdomInstall.mockResolvedValue({
      receipt: 'wip_1',
      skill_id: 'skill-1',
      version: 2,
      compatibility: { outcome: 'compatible' }
    })
    applyWisdomInstall.mockResolvedValue({ installed: true })

    render(<CollectiveWisdomPanel profile="research" />)
    fireEvent.click(await screen.findByRole('button', { name: /managed-skill/ }))
    expect(await screen.findByText('# Exact bytes')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Install…' }))
    expect(await screen.findByText(/wip_1/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm install' }))
    await waitFor(() => expect(applyWisdomInstall).toHaveBeenCalledWith('wip_1', false, 'research'))
  })
})
