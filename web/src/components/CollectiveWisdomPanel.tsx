import { useCallback, useEffect, useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Loader2, Search, Sparkles } from 'lucide-react'

import { api } from '@/lib/api'
import type {
  WisdomCandidate,
  WisdomDiscovery,
  WisdomDraft,
  WisdomDraftReview,
  WisdomPreparedDraft,
  WisdomActionPlan,
  WisdomInstallations,
  WisdomSkillDetail,
  WisdomStatus,
  WisdomVersionContent
} from '@/lib/api'
import { Button } from '@nous-research/ui/ui/components/button'
import { Input } from '@nous-research/ui/ui/components/input'
import { useI18n } from '@/i18n'

interface Props {
  profile?: string
}

async function waitForWisdomAction(name: string): Promise<void> {
  for (let attempt = 0; attempt < 1200; attempt += 1) {
    const status = await api.getActionStatus(name, 80)
    if (!status.running) {
      if (status.exit_code !== 0) {
        throw new Error(status.lines.at(-1) || `Collective Wisdom action failed (${status.exit_code ?? 'unknown'})`)
      }
      return
    }
    await new Promise(resolve => setTimeout(resolve, 500))
  }
  throw new Error('Collective Wisdom action timed out')
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
  const [content, setContent] = useState<WisdomVersionContent | null>(null)
  const [installations, setInstallations] = useState<WisdomInstallations>({ installations: [], notifications: [] })
  const [actionPlan, setActionPlan] = useState<
    (WisdomActionPlan & { action: 'install' | 'update' | 'uninstall' }) | null
  >(null)
  const [acceptSensitive, setAcceptSensitive] = useState(false)
  const [acceptPartial, setAcceptPartial] = useState(false)
  const [preserveModified, setPreserveModified] = useState(false)
  const [review, setReview] = useState<WisdomDraftReview | null>(null)
  const [prepared, setPrepared] = useState<WisdomPreparedDraft | null>(null)
  const [preparedSkill, setPreparedSkill] = useState('')
  const [approvedDescription, setApprovedDescription] = useState('')
  const [approvedSpecification, setApprovedSpecification] = useState('')
  const [busy, setBusy] = useState<string | null>(null)
  const [error, setError] = useState<string | null>(null)

  const loadConfiguredData = useCallback(async () => {
    return Promise.all([
      api.getWisdomDiscovery(profile),
      api.getWisdomCandidates(profile),
      api.getWisdomDrafts(profile),
      api.getWisdomInstallations(profile)
    ])
  }, [profile])

  useEffect(() => {
    let cancelled = false
    api
      .getWisdomStatus(profile)
      .then(async nextStatus => {
        if (cancelled) return
        setStatus(nextStatus)
        if (nextStatus.configured) {
          const [nextDiscovery, nextCandidates, nextDrafts, nextInstallations] = await loadConfiguredData()
          if (cancelled) return
          setDiscovery(nextDiscovery)
          setCandidates(nextCandidates.candidates)
          setDrafts(nextDrafts.drafts)
          setInstallations(nextInstallations)
        }
        if (!cancelled) setError(null)
      })
      .catch(reason => !cancelled && setError(String(reason)))
      .finally(() => !cancelled && setBusy(null))
    return () => {
      cancelled = true
    }
  }, [loadConfiguredData, profile])

  const setupProfile = async () => {
    setBusy('setup')
    setError(null)
    try {
      const action = await api.setupWisdom(profile)
      await waitForWisdomAction(action.name)
      const nextStatus = await api.getWisdomStatus(profile)
      setStatus(nextStatus)
      const [nextDiscovery, nextCandidates, nextDrafts, nextInstallations] = await loadConfiguredData()
      setDiscovery(nextDiscovery)
      setCandidates(nextCandidates.candidates)
      setDrafts(nextDrafts.drafts)
      setInstallations(nextInstallations)
    } catch (reason) {
      setError(String(reason))
    } finally {
      setBusy(null)
    }
  }

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
      const detail = await api.getWisdomSkill(skillId, profile)
      setSelected(detail)
      const versions = detail.versions
        .map(version => Number(version.version))
        .filter(version => Number.isInteger(version) && version > 0)
      setContent(
        versions.length > 0 ? await api.getWisdomVersionContent(skillId, Math.max(...versions), profile) : null
      )
    } catch (reason) {
      setError(String(reason))
    } finally {
      setBusy(null)
    }
  }

  const selectedId = selected ? String(selected.skill.id || '') : ''
  const installed = installations.installations.find(item => item.skill_id === selectedId && item.state === 'active')

  const planManagedAction = async (action: 'install' | 'update' | 'uninstall') => {
    if (!selectedId) return
    setBusy(selectedId)
    setError(null)
    try {
      const plan =
        action === 'install'
          ? await api.planWisdomInstall(selectedId, profile)
          : action === 'update'
            ? await api.planWisdomUpdate(selectedId, profile)
            : { skill_id: selectedId, state: 'confirm_uninstall' }
      setActionPlan({ ...plan, action })
      setAcceptSensitive(false)
      setAcceptPartial(false)
      setPreserveModified(false)
    } catch (reason) {
      setError(String(reason))
    } finally {
      setBusy(null)
    }
  }

  const applyManagedAction = async () => {
    if (!actionPlan) return
    setBusy(actionPlan.skill_id)
    try {
      if (actionPlan.action === 'uninstall') {
        await api.uninstallWisdomSkill(actionPlan.skill_id, profile)
      } else if (!actionPlan.receipt) {
        throw new Error('Verified action receipt is missing')
      } else if (actionPlan.action === 'install') {
        await api.applyWisdomInstall(actionPlan.receipt, acceptPartial, profile)
      } else {
        await api.applyWisdomUpdate(actionPlan.receipt, { acceptSensitive, acceptPartial, preserveModified }, profile)
      }
      setInstallations(await api.getWisdomInstallations(profile))
      setActionPlan(null)
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

  if (status && !status.configured) {
    return (
      <section className="mx-auto max-w-2xl space-y-4 border border-border bg-muted/10 p-6" aria-label={copy.title}>
        <div>
          <h2 className="font-mondwest text-lg text-text-primary">{copy.title}</h2>
          <p className="mt-1 text-sm text-text-secondary">{copy.setup}</p>
        </div>
        <p className="text-sm leading-6 text-text-secondary">{copy.setupDisclosure}</p>
        {status.error && (
          <div role="alert" className="text-sm text-red-500">
            {status.error}
          </div>
        )}
        {error && (
          <div role="alert" className="text-sm text-red-500">
            {error}
          </div>
        )}
        <Button onClick={setupProfile} disabled={busy === 'setup'}>
          {busy === 'setup' ? copy.settingUp : copy.setupAction}
        </Button>
      </section>
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
        <div className="flex gap-2">
          <Button
            size="sm"
            outlined
            onClick={async () => {
              setBusy('scan')
              try {
                const action = await api.scanWisdom(undefined, profile)
                await waitForWisdomAction(action.name)
                const next = await api.getWisdomCandidates(profile)
                setCandidates(next.candidates)
              } catch (reason) {
                setError(String(reason))
              } finally {
                setBusy(null)
              }
            }}
          >
            {busy === 'scan' ? copy.checking : copy.scanLocal}
          </Button>
          <Button
            size="sm"
            outlined
            onClick={async () => {
              setBusy('check')
              try {
                await api.checkWisdom(profile)
                setInstallations(await api.getWisdomInstallations(profile))
              } catch (reason) {
                setError(String(reason))
              } finally {
                setBusy(null)
              }
            }}
          >
            {busy === 'check' ? copy.checking : copy.checkUpdates(installations.notifications.length)}
          </Button>
        </div>
      </div>

      {error && (
        <div role="alert" className="flex items-center gap-2 border border-red-500/40 px-3 py-2 text-sm">
          <AlertTriangle className="h-4 w-4" />
          {error}
        </div>
      )}

      {installations.notifications.length > 0 && (
        <div className="border border-blue-500/40 p-3 text-xs" aria-label="Collective Wisdom notifications">
          <div className="flex items-center justify-between gap-3">
            <div>
              <span>{copy.updatesReady(installations.notifications.length)}</span>
              <ul className="mt-2 space-y-1 text-text-tertiary">
                {installations.notifications.slice(0, 8).map((event, index) => (
                  <li key={String(event.event_id ?? index)}>
                    {String(event.kind ?? 'update')} · {String(event.skill_id ?? 'skill')}
                    {event.version ? ` · v${String(event.version)}` : ''}
                  </li>
                ))}
              </ul>
            </div>
            <Button
              size="sm"
              outlined
              onClick={async () => {
                await api.acknowledgeWisdomNotifications(profile)
                setInstallations(await api.getWisdomInstallations(profile))
              }}
            >
              {copy.markSeen}
            </Button>
          </div>
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
            {JSON.stringify(
              {
                skill: selected.skill,
                latest_version: selected.latest_version_detail,
                version_history: selected.versions,
                local_compatibility: selected.local_compatibility
              },
              null,
              2
            )}
          </pre>
          {content && (
            <div className="mt-4">
              <p className="break-all font-mono text-[11px]">content {content.content_hash}</p>
              {content.files.map(file => (
                <details key={file.path} className="border-t border-border py-2" open>
                  <summary className="cursor-pointer font-mono text-xs">
                    {file.path} · {file.hash}
                  </summary>
                  <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap text-xs">{file.content_utf8}</pre>
                </details>
              ))}
            </div>
          )}
          <div className="mt-4 flex flex-wrap justify-end gap-2">
            {installed ? (
              <>
                <span className="self-center text-xs text-text-tertiary">
                  {copy.installed(installed.version, installed.update_mode)}
                </span>
                <Button size="sm" outlined onClick={() => planManagedAction('uninstall')}>
                  {copy.uninstall}
                </Button>
                <Button size="sm" onClick={() => planManagedAction('update')}>
                  {copy.checkSkill}
                </Button>
              </>
            ) : (
              <Button size="sm" onClick={() => planManagedAction('install')}>
                {copy.install}
              </Button>
            )}
          </div>
        </div>
      )}

      {actionPlan && (
        <div className="border border-amber-500/50 p-4" role="dialog" aria-label="Verified managed action plan">
          <h3 className="font-mono text-base">{copy.confirmAction(actionPlan.action)}</h3>
          <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap text-xs">
            {JSON.stringify(actionPlan, null, 2)}
          </pre>
          {actionPlan.state === 'current' && <p className="mt-3 text-xs">This managed skill is already current.</p>}
          {actionPlan.compatibility && actionPlan.compatibility.outcome !== 'compatible' && (
            <label className="mt-3 flex gap-2 text-xs">
              <input
                type="checkbox"
                checked={acceptPartial}
                onChange={event => setAcceptPartial(event.target.checked)}
              />
              {copy.acceptCompatibility}
            </label>
          )}
          {(actionPlan.sensitive_expansion?.length ?? 0) > 0 && (
            <label className="mt-2 flex gap-2 text-xs">
              <input
                type="checkbox"
                checked={acceptSensitive}
                onChange={event => setAcceptSensitive(event.target.checked)}
              />
              {copy.acceptSensitive}
            </label>
          )}
          {actionPlan.modified && actionPlan.update_mode !== 'REQUIRED' && (
            <label className="mt-2 flex gap-2 text-xs">
              <input
                type="checkbox"
                checked={preserveModified}
                onChange={event => setPreserveModified(event.target.checked)}
              />
              {copy.preserveModified}
            </label>
          )}
          <div className="mt-4 flex justify-end gap-2">
            <Button size="sm" outlined onClick={() => setActionPlan(null)}>
              Cancel
            </Button>
            {actionPlan.state !== 'current' && (
              <Button size="sm" onClick={applyManagedAction}>
                {copy.confirmAction(actionPlan.action)}
              </Button>
            )}
          </div>
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
          <div className="mt-3 grid gap-3 border-y border-border py-3 text-xs">
            <div>
              <strong>Owner-authored description (not platform verified)</strong>
              <p className="mt-1 whitespace-pre-wrap text-text-secondary">
                {review.draft.authorDescription || 'No description.'}
              </p>
            </div>
            <div>
              <strong>Server-enforced scan and server-derived facts</strong>
              <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-text-secondary">
                {JSON.stringify(
                  {
                    verdict: review.draft.scanVerdict,
                    scan: review.draft.scan,
                    explanation: review.draft.explanation
                  },
                  null,
                  2
                )}
              </pre>
            </div>
            <div>
              <strong>System Specification (declarative only)</strong>
              <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-text-secondary">
                {JSON.stringify(review.draft.systemSpec, null, 2)}
              </pre>
            </div>
          </div>
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
              outlined
              disabled={busy === review.draft.id}
              onClick={async () => {
                setBusy(review.draft.id)
                try {
                  await api.decideWisdomDraft(review.draft.id, 'decline', profile)
                  setReview(null)
                } catch (reason) {
                  setError(String(reason))
                } finally {
                  setBusy(null)
                }
              }}
            >
              {copy.decline}
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
