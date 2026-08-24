import { useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Loader2, Search, Sparkles } from 'lucide-react'

import { api } from '@/lib/api'
import type {
  WisdomCandidate,
  WisdomDiscovery,
  WisdomDraft,
  WisdomDraftReview,
  WisdomPreparedDraft,
  WisdomSkillDetail,
  WisdomStatus
} from '@/lib/api'
import { Button } from '@nous-research/ui/ui/components/button'
import { Input } from '@nous-research/ui/ui/components/input'
import { useI18n } from '@/i18n'

interface Props {
  profile?: string
}

export function CollectiveWisdomPanel({ profile }: Props) {
  const { t } = useI18n()
  const copy = t.skills.wisdom
  const [status, setStatus] = useState<WisdomStatus | null>(null)
  const [discovery, setDiscovery] = useState<WisdomDiscovery>({ skills: [], next_cursor: null })
  const [candidates, setCandidates] = useState<WisdomCandidate[]>([])
  const [drafts, setDrafts] = useState<WisdomDraft[]>([])
  const [query, setQuery] = useState('')
  const [selected, setSelected] = useState<WisdomSkillDetail | null>(null)
  const [review, setReview] = useState<WisdomDraftReview | null>(null)
  const [prepared, setPrepared] = useState<WisdomPreparedDraft | null>(null)
  const [preparedSkill, setPreparedSkill] = useState('')
  const [approvedDescription, setApprovedDescription] = useState('')
  const [approvedSpecification, setApprovedSpecification] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    let cancelled = false
    Promise.all([
      api.getWisdomStatus(profile),
      api.getWisdomDiscovery(profile),
      api.getWisdomCandidates(profile),
      api.getWisdomDrafts(profile)
    ])
      .then(([nextStatus, nextDiscovery, nextCandidates, nextDrafts]) => {
        if (cancelled) return
        setError(null)
        setStatus(nextStatus)
        setDiscovery(nextDiscovery)
        setCandidates(nextCandidates.candidates)
        setDrafts(nextDrafts.drafts)
      })
      .catch(reason => !cancelled && setError(String(reason)))
      .finally(() => !cancelled && setBusy(null))
    return () => {
      cancelled = true
    }
  }, [profile])

  const filtered = useMemo(() => {
    const normalized = query.trim().toLowerCase()
    return discovery.skills.filter(
      skill =>
        !normalized ||
        skill.slug.toLowerCase().includes(normalized) ||
        (skill.author_description || '').toLowerCase().includes(normalized)
    )
  }, [discovery.skills, query])

  const openSkill = async (skillId: string) => {
    setBusy(skillId)
    setError(null)
    try {
      setSelected(await api.getWisdomSkill(skillId, profile))
    } catch (reason) {
      setError(String(reason))
    } finally {
      setBusy(null)
    }
  }

  const prepare = async (candidate: WisdomCandidate) => {
    setBusy(candidate.local_skill_id)
    setError(null)
    try {
      const result = await api.suggestWisdomSkill(candidate.name, profile)
      if ('network_submission' in result) {
        setPrepared(result)
        setPreparedSkill(candidate.name)
        setApprovedDescription(result.drafted_description)
        setApprovedSpecification(JSON.stringify(result.system_specification, null, 2))
      }
    } catch (reason) {
      setError(String(reason))
    } finally {
      setBusy(null)
    }
  }

  const openReview = async (draftId: string) => {
    setBusy(draftId)
    setError(null)
    try {
      setReview(await api.reviewWisdomDraft(draftId, false, profile))
    } catch (reason) {
      setError(String(reason))
    } finally {
      setBusy(null)
    }
  }

  if (!status && !error) {
    return (
      <div className="flex items-center justify-center py-24" aria-label={copy.loading}>
        <Loader2 className="h-5 w-5 animate-spin" />
      </div>
    )
  }

  if (error && !status) {
    return (
      <div role="alert" className="border border-border px-4 py-8 text-sm text-text-secondary">
        {copy.unavailable} {error}
      </div>
    )
  }

  return (
    <section className="space-y-4" aria-label={copy.title}>
      <div className="flex flex-col gap-3 border border-border bg-muted/10 p-4 sm:flex-row sm:items-center sm:justify-between">
        <div>
          <h2 className="font-mondwest text-lg text-text-primary">{copy.title}</h2>
          <p className="text-xs text-text-secondary">
            {status?.verified_org_id ? `${status.verified_org_id} · org-wide collective` : copy.setup}
          </p>
        </div>
        <div className="relative w-full sm:max-w-sm">
          <Search className="absolute left-3 top-2.5 h-4 w-4 text-text-tertiary" />
          <Input
            aria-label={copy.search}
            value={query}
            onChange={event => setQuery(event.target.value)}
            placeholder={copy.search}
            className="pl-9"
          />
        </div>
      </div>

      {error && (
        <div role="alert" className="flex items-center gap-2 border border-red-500/40 px-3 py-2 text-sm">
          <AlertTriangle className="h-4 w-4" />
          {error}
        </div>
      )}

      {(candidates.length > 0 || drafts.length > 0) && (
        <div className="grid gap-3 border border-border p-4 lg:grid-cols-2">
          <div>
            <h3 className="mb-2 text-sm font-medium">{copy.potential}</h3>
            <div className="space-y-2">
              {candidates.slice(0, 6).map(candidate => (
                <div
                  key={candidate.local_skill_id}
                  className="flex items-start justify-between gap-3 border-t border-border py-2 first:border-0"
                >
                  <div className="min-w-0">
                    <p className="truncate font-mono text-sm">{candidate.name}</p>
                    <p className="text-xs text-text-tertiary">{candidate.reason || copy.localOnly}</p>
                  </div>
                  <Button
                    size="sm"
                    outlined
                    disabled={busy === candidate.local_skill_id || candidate.eligibility !== 'eligible'}
                    onClick={() => prepare(candidate)}
                    prefix={busy === candidate.local_skill_id ? <Loader2 className="animate-spin" /> : <Sparkles />}
                  >
                    {copy.prepare}
                  </Button>
                </div>
              ))}
            </div>
          </div>
          <div>
            <h3 className="mb-2 text-sm font-medium">{copy.ownerReview}</h3>
            {drafts.length === 0 ? (
              <p className="text-xs text-text-tertiary">{copy.noDrafts}</p>
            ) : (
              drafts.map(draft => (
                <button
                  key={draft.id}
                  type="button"
                  className="flex w-full items-center justify-between border-t border-border py-2 text-left first:border-0 focus-visible:outline focus-visible:outline-2"
                  onClick={() => openReview(draft.id)}
                >
                  <span>
                    <span className="block font-mono text-sm">{draft.slug}</span>
                    <span className="text-xs text-text-tertiary">{draft.state}</span>
                  </span>
                  <span className="text-xs">{copy.reviewExact}</span>
                </button>
              ))
            )}
          </div>
        </div>
      )}

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-3">
        {filtered.map(skill => (
          <button
            key={skill.id}
            type="button"
            onClick={() => openSkill(skill.id)}
            className="min-h-44 border border-border bg-muted/10 p-4 text-left transition-colors hover:bg-muted/30 focus-visible:outline focus-visible:outline-2"
          >
            <div className="mb-3 flex items-center justify-between gap-3">
              <span className="font-mono font-semibold">{skill.slug}</span>
              <span className="flex items-center gap-1 text-xs text-emerald-400">
                <CheckCircle2 className="h-3 w-3" /> {copy.serverScanPassed}
              </span>
            </div>
            <p className="line-clamp-3 text-sm text-text-secondary">{skill.author_description || copy.noDescription}</p>
            <div className="mt-4 flex items-center justify-between border-t border-border pt-3 text-xs text-text-tertiary">
              <span>v{skill.latest_version ?? '—'}</span>
              <span>
                {skill.install_count} {copy.managedInstalls}
              </span>
            </div>
          </button>
        ))}
      </div>

      {selected && (
        <div className="border border-border p-4" aria-live="polite">
          <div className="flex items-center justify-between">
            <h3 className="font-mono text-base">
              {String(selected.skill.slug || selected.skill.id || 'Skill detail')}
            </h3>
            <Button size="sm" outlined onClick={() => setSelected(null)}>
              {copy.close}
            </Button>
          </div>
          <pre className="mt-4 max-h-80 overflow-auto whitespace-pre-wrap text-xs text-text-secondary">
            {JSON.stringify(selected, null, 2)}
          </pre>
        </div>
      )}

      {prepared && (
        <div className="border border-cyan-500/40 p-4" aria-label="Prepare owner-private Wisdom draft">
          <h3 className="font-mono text-base">{copy.prepareTitle}</h3>
          <p className="mt-1 text-xs text-text-secondary">{copy.prepareNotice}</p>
          <label className="mt-4 block text-xs font-medium" htmlFor="wisdom-author-description">
            {copy.ownerDescription}
          </label>
          <textarea
            id="wisdom-author-description"
            className="mt-1 min-h-24 w-full border border-border bg-transparent p-3 text-sm focus-visible:outline focus-visible:outline-2"
            maxLength={4096}
            value={approvedDescription}
            onChange={event => setApprovedDescription(event.target.value)}
          />
          <label className="mt-4 block text-xs font-medium" htmlFor="wisdom-system-specification">
            {copy.systemSpecification}
          </label>
          <textarea
            id="wisdom-system-specification"
            className="mt-1 min-h-64 w-full border border-border bg-transparent p-3 font-mono text-xs focus-visible:outline focus-visible:outline-2"
            spellCheck={false}
            value={approvedSpecification}
            onChange={event => setApprovedSpecification(event.target.value)}
          />
          <p className="mt-2 break-all font-mono text-[11px] text-text-tertiary">
            {copy.localOverlay}: {prepared.overlay_path}
          </p>
          <div className="mt-4 flex justify-end gap-2">
            <Button size="sm" outlined onClick={() => setPrepared(null)}>
              {copy.cancel}
            </Button>
            <Button
              size="sm"
              disabled={busy === prepared.local_draft_id}
              onClick={async () => {
                setBusy(prepared.local_draft_id)
                setError(null)
                try {
                  const specification = JSON.parse(approvedSpecification) as Record<string, unknown>
                  await api.suggestWisdomSkill(preparedSkill, profile, approvedDescription, specification)
                  const response = await api.getWisdomDrafts(profile)
                  setDrafts(response.drafts)
                  setPrepared(null)
                } catch (reason) {
                  setError(reason instanceof Error ? reason.message : String(reason))
                } finally {
                  setBusy(null)
                }
              }}
            >
              {busy === prepared.local_draft_id ? copy.submitting : copy.submit}
            </Button>
          </div>
        </div>
      )}

      {review && (
        <div className="border border-emerald-500/40 p-4" aria-label="Owner review exact content">
          <h3 className="font-mono text-base">{review.draft.slug}</h3>
          <p className="mt-1 text-xs text-text-secondary">{copy.readEvery}</p>
          <div className="my-3 grid gap-1 font-mono text-[11px]">
            <span>content {review.hashes.content}</span>
            <span>author description {review.hashes.author_description}</span>
            <span>package manifest {review.hashes.package_manifest}</span>
          </div>
          {review.files.map(file => (
            <details key={file.path} className="border-t border-border py-2" open>
              <summary className="cursor-pointer font-mono text-xs">
                {file.path} · {file.hash}
              </summary>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-xs">{file.content_utf8}</pre>
            </details>
          ))}
          <div className="mt-4 flex justify-end gap-2">
            <Button size="sm" outlined onClick={() => setReview(null)}>
              {copy.close}
            </Button>
            <Button
              size="sm"
              onClick={async () => {
                setBusy(review.draft.id)
                try {
                  await api.reviewWisdomDraft(review.draft.id, true, profile)
                  await api.decideWisdomDraft(review.draft.id, 'approve', profile)
                  setReview(null)
                } catch (reason) {
                  setError(String(reason))
                } finally {
                  setBusy(null)
                }
              }}
            >
              {busy === review.draft.id ? copy.publishing : copy.approve}
            </Button>
          </div>
        </div>
      )}
    </section>
  )
}
