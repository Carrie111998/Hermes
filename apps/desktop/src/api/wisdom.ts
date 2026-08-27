import type { ActionResponse } from '@/types/hermes'

import { capabilityScoped, type ProfileScope } from './client'

export interface WisdomStatus {
  configured: boolean
  setup_required_reason?: 'not_configured' | 'organization_changed' | null
  gateway_available: boolean
  capability_advertised: boolean
  verified_org_id: null | string
  authenticated_org_id?: null | string
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
  contribution_state: 'new' | 'prepared'
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
  explanation?: null | string
  scan?: null | Record<string, unknown>
  scanVerdict: null | string
  systemSpec?: null | Record<string, unknown>
  updatedAt: string
}

export interface WisdomPreparedDraft {
  network_submission: false
  local_draft_id: string
  overlay_path: string
  drafted_description: string
  files: WisdomDraftReview['files']
  local_scan: WisdomLocalScan
  system_specification: Record<string, unknown>
  next_step: string
}

export interface WisdomLocalScan {
  guard: Record<string, unknown>
  skill_evaluator: Record<string, unknown>
}

export interface WisdomSubmittedDraft {
  draft: WisdomDraft
  local_scan: WisdomLocalScan
  notice: string
}

export interface WisdomDraftReview {
  draft: WisdomDraft & Record<string, unknown>
  effective_policy: Record<string, unknown>
  files: Array<{ content_utf8: string; hash: string; mode: 'exec' | 'file'; path: string }>
  hashes: { author_description: string; content: string; package_manifest: string }
  receipt: null | string
}

export interface WisdomEditedFile {
  path: string
  content_utf8: string
}

export interface WisdomRevisedDraft {
  draft: WisdomDraft & Record<string, unknown>
  local_scan: WisdomLocalScan
  notice: string
}

export interface WisdomSkillDetail {
  latest_version_detail?: Record<string, unknown>
  local_compatibility?: Record<string, unknown>
  skill: Record<string, unknown>
  versions: Array<Record<string, unknown>>
}

export interface WisdomVersionContent {
  commit: string
  content_hash: string
  files: Array<{ content_utf8: string; hash: string; mode: 'exec' | 'file'; path: string }>
}

export type WisdomUpdateMode = 'AUTO_WITH_NOTICE' | 'MANUAL' | 'REQUIRED'

export interface WisdomManagedInstall {
  skill_id: string
  slug: string
  version: number
  update_mode: WisdomUpdateMode
  state: string
  target_path: string
}

export interface WisdomInstallations {
  installations: WisdomManagedInstall[]
  notifications: Array<Record<string, unknown>>
}

export type WisdomInstallationCheckState =
  'archived' | 'current' | 'not_recorded' | 'taken_down' | 'update_available' | 'updated'

export interface WisdomInstallationCheck {
  skill_id: string
  state: WisdomInstallationCheckState
  plan?: WisdomActionPlan
  result?: Record<string, unknown>
}

export interface WisdomCheckResult {
  installations: WisdomInstallationCheck[]
  qualification_events?: unknown[]
  feed?: Record<string, unknown>
  owner_decisions?: Record<string, unknown>
  telegram?: Record<string, unknown>
}

export interface WisdomActionPlan {
  receipt?: string
  state?: string
  skill_id: string
  version?: number
  compatibility?: { outcome: string; reasons?: string[] }
  sensitive_expansion?: string[]
  modified?: boolean
  update_mode?: string
  allowed?: boolean
}

const request = <T>(path: string, profile?: ProfileScope, init?: { body?: unknown; method?: string }): Promise<T> =>
  window.hermesDesktop.api<T>({
    ...capabilityScoped(profile),
    path,
    method: init?.method,
    body: init?.body
  })

export const getWisdomStatus = (profile?: ProfileScope): Promise<WisdomStatus> => request('/api/wisdom/status', profile)

export const setupWisdom = (profile?: ProfileScope): Promise<ActionResponse> =>
  request('/api/wisdom/setup', profile, { method: 'POST', body: { accept_disclosure: true } })

export const scanWisdom = (skill?: string, profile?: ProfileScope): Promise<ActionResponse> =>
  request('/api/wisdom/scan', profile, { method: 'POST', body: { skill } })

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

export const getWisdomVersionContent = (
  skillId: string,
  version: number,
  profile?: ProfileScope
): Promise<WisdomVersionContent> =>
  request(`/api/wisdom/skills/${encodeURIComponent(skillId)}/versions/${version}/content`, profile)

export const getWisdomInstallations = (profile?: ProfileScope): Promise<WisdomInstallations> =>
  request('/api/wisdom/installations', profile)

export const checkWisdom = (profile?: ProfileScope, applyAutomatic = true): Promise<WisdomCheckResult> =>
  request('/api/wisdom/check', profile, { method: 'POST', body: { apply_automatic: applyAutomatic } })

export const planWisdomInstall = (
  reference: string,
  profile?: ProfileScope,
  updateMode?: WisdomUpdateMode
): Promise<WisdomActionPlan> =>
  request('/api/wisdom/install/plan', profile, {
    method: 'POST',
    body: { reference, update_mode: updateMode }
  })

export const applyWisdomInstall = (
  receipt: string,
  acceptPartial: boolean,
  profile?: ProfileScope
): Promise<Record<string, unknown>> =>
  request('/api/wisdom/install/apply', profile, {
    method: 'POST',
    body: { accept_partial: acceptPartial, receipt }
  })

export const planWisdomUpdate = (skillId: string, profile?: ProfileScope): Promise<WisdomActionPlan> =>
  request('/api/wisdom/update/plan', profile, { method: 'POST', body: { skill_id: skillId } })

export const applyWisdomUpdate = (
  receipt: string,
  confirmations: { acceptPartial: boolean; acceptSensitive: boolean; preserveModified: boolean },
  profile?: ProfileScope
): Promise<Record<string, unknown>> =>
  request('/api/wisdom/update/apply', profile, {
    method: 'POST',
    body: {
      accept_partial: confirmations.acceptPartial,
      accept_sensitive: confirmations.acceptSensitive,
      preserve_modified: confirmations.preserveModified,
      receipt
    }
  })

export const uninstallWisdomSkill = (skillId: string, profile?: ProfileScope): Promise<Record<string, unknown>> =>
  request('/api/wisdom/uninstall', profile, { method: 'POST', body: { skill_id: skillId } })

export const acknowledgeWisdomNotifications = (
  profile?: ProfileScope
): Promise<{ events: Array<Record<string, unknown>> }> =>
  request('/api/wisdom/notifications', profile, { method: 'POST', body: { mark_seen: true } })

export const suggestWisdomSkill = (
  skill: string,
  profile?: ProfileScope,
  approval?: { description: string; systemSpecification: Record<string, unknown> },
  localSkillId?: string
): Promise<WisdomPreparedDraft | WisdomSubmittedDraft> =>
  request('/api/wisdom/suggest', profile, {
    method: 'POST',
    body: {
      skill,
      local_skill_id: localSkillId,
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

export const saveWisdomPreparedDraft = (
  draftId: string,
  authorDescription: string,
  files: WisdomEditedFile[],
  profile?: ProfileScope
): Promise<WisdomPreparedDraft> =>
  request('/api/wisdom/prepared/save', profile, {
    method: 'POST',
    body: { author_description: authorDescription, draft_id: draftId, files }
  })

export const dismissWisdomCandidate = (
  localSkillId: string,
  contentHash: string,
  profile?: ProfileScope
): Promise<{ dismissed: true }> =>
  request('/api/wisdom/candidates/dismiss', profile, {
    method: 'POST',
    body: { content_hash: contentHash, local_skill_id: localSkillId }
  })

export const reviseWisdomDraft = (
  draftId: string,
  authorDescription: string,
  files: WisdomEditedFile[],
  hashes: WisdomDraftReview['hashes'],
  profile?: ProfileScope
): Promise<WisdomRevisedDraft> =>
  request('/api/wisdom/revise', profile, {
    method: 'POST',
    body: {
      draft_id: draftId,
      author_description: authorDescription,
      files,
      expected_content_hash: hashes.content,
      expected_author_description_hash: hashes.author_description,
      expected_package_manifest_hash: hashes.package_manifest
    }
  })

export const decideWisdomDraft = (
  draftId: string,
  decision: 'approve' | 'decline',
  profile?: ProfileScope
): Promise<Record<string, unknown>> =>
  request(`/api/wisdom/${decision}`, profile, { method: 'POST', body: { draft_id: draftId } })
