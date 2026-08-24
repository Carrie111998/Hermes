// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type * as HermesApi from '@/hermes'

const getWisdomStatus = vi.fn()
const getWisdomDiscovery = vi.fn()
const getWisdomCandidates = vi.fn()
const getWisdomDrafts = vi.fn()
const getWisdomSkill = vi.fn()
const getWisdomInstallations = vi.fn()
const getWisdomVersionContent = vi.fn()
const suggestWisdomSkill = vi.fn()
const reviewWisdomDraft = vi.fn()
const decideWisdomDraft = vi.fn()
const planWisdomInstall = vi.fn()
const applyWisdomInstall = vi.fn()
const setupWisdom = vi.fn()
const getActionStatus = vi.fn()

vi.mock('@/hermes', async importOriginal => ({
  ...(await importOriginal<typeof HermesApi>()),
  decideWisdomDraft,
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
  applyWisdomInstall,
  setupWisdom,
  getActionStatus
}))

vi.mock('@/store/notifications', () => ({ notifyError: vi.fn() }))

const scope = { connectionId: 'gateway-a', profile: 'research' }

async function renderTab() {
  const { CollectiveTab } = await import('./collective-tab')
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })

  return render(
    <QueryClientProvider client={client}>
      <CollectiveTab profile={scope} query="" />
    </QueryClientProvider>
  )
}

afterEach(() => vi.clearAllMocks())

function mockInstallations() {
  getWisdomInstallations.mockResolvedValue({ installations: [], notifications: [] })
}

describe('CollectiveTab', () => {
  it('keeps collective reads disabled until disclosure setup is accepted', async () => {
    getWisdomStatus
      .mockResolvedValueOnce({ configured: false, verified_org_id: null })
      .mockResolvedValueOnce({ configured: true, verified_org_id: 'org-1' })
    getWisdomDiscovery.mockResolvedValue({ next_cursor: null, skills: [] })
    getWisdomCandidates.mockResolvedValue({ candidates: [] })
    getWisdomDrafts.mockResolvedValue({ drafts: [] })
    mockInstallations()
    setupWisdom.mockResolvedValue({ ok: true, name: 'wisdom-setup', pid: 1 })
    getActionStatus.mockResolvedValue({
      name: 'wisdom-setup',
      running: false,
      exit_code: 0,
      pid: 1,
      lines: []
    })

    await renderTab()
    expect(await screen.findByText(/Candidate qualification stays on this profile/)).toBeTruthy()
    expect(getWisdomDiscovery).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: /set up this profile/ }))

    await waitFor(() => expect(setupWisdom).toHaveBeenCalledWith(scope))
    expect(getActionStatus).toHaveBeenCalledWith('wisdom-setup', 80, scope)
  })

  it('scopes reads to the selected connection/profile and renders hostile text as text', async () => {
    mockInstallations()
    getWisdomStatus.mockResolvedValue({ configured: true, verified_org_id: 'org-1' })
    getWisdomCandidates.mockResolvedValue({ candidates: [] })
    getWisdomDrafts.mockResolvedValue({ drafts: [] })
    getWisdomDiscovery.mockResolvedValue({
      next_cursor: null,
      skills: [
        {
          id: 'skill-1',
          slug: '<img src=x onerror=alert(1)>',
          author_description: '<script>window.pwned=true</script>',
          install_count: 0,
          latest_version: 1,
          state: 'active'
        }
      ]
    })

    await renderTab()

    expect(await screen.findByText('<img src=x onerror=alert(1)>')).toBeTruthy()
    expect(document.querySelector('script')).toBeNull()
    expect(getWisdomDiscovery).toHaveBeenCalledWith(scope)
    expect(getWisdomCandidates).toHaveBeenCalledWith(scope)
  })

  it('prepares locally, accepts explicit owner fields, then submits without local evidence', async () => {
    mockInstallations()
    getWisdomStatus.mockResolvedValue({ configured: true, verified_org_id: 'org-1' })
    getWisdomDiscovery.mockResolvedValue({ next_cursor: null, skills: [] })
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
    getWisdomDrafts.mockResolvedValue({ drafts: [] })
    suggestWisdomSkill
      .mockResolvedValueOnce({
        network_submission: false,
        local_draft_id: 'local:draft',
        overlay_path: '/private/overlay',
        drafted_description: 'Drafted copy',
        system_specification: { hermes: { minimum_version: '0.17.0' } },
        next_step: 'review'
      })
      .mockResolvedValueOnce({ draft: { id: 'draft-1' } })

    await renderTab()
    fireEvent.click(await screen.findByRole('button', { name: 'Prepare' }))
    const description = await screen.findByLabelText('Owner-authored description')
    fireEvent.change(description, { target: { value: 'Approved owner copy' } })
    fireEvent.click(screen.getByRole('button', { name: 'Submit for owner-only server review' }))

    await waitFor(() => expect(suggestWisdomSkill).toHaveBeenCalledTimes(2))
    const payload = suggestWisdomSkill.mock.calls[1][2]
    expect(payload).toEqual({
      description: 'Approved owner copy',
      systemSpecification: { hermes: { minimum_version: '0.17.0' } }
    })
    expect(JSON.stringify(payload)).not.toMatch(/usage|refinement|candidate|ranking|stability/)
  })

  it('requires a verified plan before applying a managed install', async () => {
    mockInstallations()
    getWisdomStatus.mockResolvedValue({ configured: true, verified_org_id: 'org-1' })
    getWisdomCandidates.mockResolvedValue({ candidates: [] })
    getWisdomDrafts.mockResolvedValue({ drafts: [] })
    getWisdomDiscovery.mockResolvedValue({
      next_cursor: null,
      skills: [
        {
          id: 'skill-1',
          slug: 'managed-skill',
          author_description: 'Does work',
          install_count: 0,
          latest_version: 2,
          state: 'active'
        }
      ]
    })
    getWisdomSkill.mockResolvedValue({ skill: { id: 'skill-1', slug: 'managed-skill' }, versions: [{ version: 2 }] })
    getWisdomVersionContent.mockResolvedValue({ commit: 'sha256:commit', content_hash: 'sha256:content', files: [] })
    planWisdomInstall.mockResolvedValue({
      receipt: 'wip_1',
      skill_id: 'skill-1',
      version: 2,
      compatibility: { outcome: 'compatible' }
    })
    applyWisdomInstall.mockResolvedValue({ installed: true })

    await renderTab()
    fireEvent.click(await screen.findByRole('button', { name: /managed-skill/ }))
    fireEvent.click(await screen.findByRole('button', { name: 'Install…' }))
    expect(await screen.findByText(/wip_1/)).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Confirm install' }))
    await waitFor(() => expect(applyWisdomInstall).toHaveBeenCalledWith('wip_1', false, scope))
  })
})
