import { capabilityScoped, type ProfileScope } from './client'

export interface WisdomStatus {
  configured: boolean
  gateway_available: boolean
  capability_advertised: boolean
  verified_org_id: null | string
  display_scopes: string[]
  error?: null | string
}

export interface WisdomCandidate {
  local_skill_id: string
  name: string
  path: string
  content_hash: string
  eligibility: 'eligible' | 'instruction_only_fork_required'
  reason: null | string
  qualification: string
}

export interface WisdomCandidateEvent {
  id: string
  kind: 'wisdom.candidate'
  session_id: null | string
  task_id: null | string
  content_hash: string
  payload: {
    skill_name: string
    qualification: string
    local_reasons: Record<string, unknown>
    consent_required: true
    networked: false
  }
}

export interface WisdomSkillSummary {
  id: string
  slug: string
  state: string
  latest_version: null | number
  author_description: null | string
  install_count: number
  scan_verdict?: null | string
  system_spec?: null | Record<string, unknown>
}

export interface WisdomDiscovery {
  skills: WisdomSkillSummary[]
  next_cursor: null | string
}

export interface WisdomDraft {
  id: string
  slug: string
  state: string
  authorDescription: null | string
  scanVerdict: null | string
  updatedAt: string
}

export interface WisdomPreparedDraft {
  network_submission: false
  local_draft_id: string
  overlay_path: string
  drafted_description: string
  system_specification: Record<string, unknown>
  next_step: string
}

export interface WisdomDraftReview {
  draft: WisdomDraft & Record<string, unknown>
  effective_policy: Record<string, unknown>
  files: Array<{ content_utf8: string; hash: string; mode: 'exec' | 'file'; path: string }>
  hashes: { author_description: string; content: string; package_manifest: string }
  receipt: null | string
}

export interface WisdomSkillDetail {
  skill: Record<string, unknown>
  versions: Array<Record<string, unknown>>
}

const request = <T>(path: string, profile?: ProfileScope, init?: { body?: unknown; method?: string }): Promise<T> =>
  window.hermesDesktop.api<T>({
    ...capabilityScoped(profile),
    path,
    method: init?.method,
    body: init?.body
  })

export const getWisdomStatus = (profile?: ProfileScope): Promise<WisdomStatus> => request('/api/wisdom/status', profile)

export const getWisdomCandidates = (profile?: ProfileScope): Promise<{ candidates: WisdomCandidate[] }> =>
  request('/api/wisdom/candidates', profile)

export const getWisdomEvents = (
  sessionId: string,
  profile?: ProfileScope
): Promise<{ events: WisdomCandidateEvent[] }> =>
  request(`/api/wisdom/events?session_id=${encodeURIComponent(sessionId)}`, profile)

export const getWisdomDiscovery = (profile?: ProfileScope): Promise<WisdomDiscovery> =>
  request('/api/wisdom/discovery', profile)

export const getWisdomDrafts = (profile?: ProfileScope): Promise<{ drafts: WisdomDraft[] }> =>
  request('/api/wisdom/drafts', profile)

export const getWisdomSkill = (skillId: string, profile?: ProfileScope): Promise<WisdomSkillDetail> =>
  request(`/api/wisdom/skills/${encodeURIComponent(skillId)}`, profile)

export const suggestWisdomSkill = (
  skill: string,
  profile?: ProfileScope,
  approval?: { description: string; systemSpecification: Record<string, unknown> }
): Promise<WisdomPreparedDraft | { draft: WisdomDraft }> =>
  request('/api/wisdom/suggest', profile, {
    method: 'POST',
    body: {
      skill,
      description: approval?.description,
      system_specification: approval?.systemSpecification
    }
  })

export const reviewWisdomDraft = (
  draftId: string,
  acknowledge: boolean,
  profile?: ProfileScope
): Promise<WisdomDraftReview> =>
  request('/api/wisdom/review', profile, {
    method: 'POST',
    body: { acknowledge, draft_id: draftId }
  })

export const decideWisdomDraft = (
  draftId: string,
  decision: 'approve' | 'decline',
  profile?: ProfileScope
): Promise<Record<string, unknown>> =>
  request(`/api/wisdom/${decision}`, profile, { method: 'POST', body: { draft_id: draftId } })
