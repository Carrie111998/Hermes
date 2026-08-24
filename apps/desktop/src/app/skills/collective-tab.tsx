import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  acknowledgeWisdomNotifications,
  applyWisdomInstall,
  applyWisdomUpdate,
  checkWisdom,
  decideWisdomDraft,
  getActionStatus,
  getWisdomCandidates,
  getWisdomDiscovery,
  getWisdomDrafts,
  getWisdomInstallations,
  getWisdomSkill,
  getWisdomStatus,
  getWisdomVersionContent,
  planWisdomInstall,
  planWisdomUpdate,
  type ProfileScope,
  profileScopeKey,
  reviewWisdomDraft,
  scanWisdom,
  setupWisdom,
  suggestWisdomSkill,
  uninstallWisdomSkill,
  type WisdomActionPlan,
  type WisdomDraftReview,
  type WisdomPreparedDraft
} from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'

import { DetailColumn, ListColumn, ListStrip, MasterDetail } from '../master-detail'

async function waitForWisdomAction(name: string, profile: ProfileScope): Promise<void> {
  for (let attempt = 0; attempt < 1200; attempt += 1) {
    const status = await getActionStatus(name, 80, profile)

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

export function CollectiveTab({ profile, query }: { profile: ProfileScope; query: string }) {
  const { t } = useI18n()
  const copy = t.skills.collective
  const scope = profileScopeKey(profile)
  const [selectedId, setSelectedId] = useState<null | string>(null)
  const [prepared, setPrepared] = useState<null | (WisdomPreparedDraft & { skill: string })>(null)
  const [description, setDescription] = useState('')
  const [specification, setSpecification] = useState('')
  const [review, setReview] = useState<null | WisdomDraftReview>(null)
  const [busy, setBusy] = useState<null | string>(null)

  const [actionPlan, setActionPlan] = useState<
    null | (WisdomActionPlan & { action: 'install' | 'uninstall' | 'update' })
  >(null)

  const [acceptSensitive, setAcceptSensitive] = useState(false)
  const [acceptPartial, setAcceptPartial] = useState(false)
  const [preserveModified, setPreserveModified] = useState(false)

  const status = useQuery({
    queryKey: ['wisdom-status', scope],
    queryFn: () => getWisdomStatus(profile),
    staleTime: 30_000
  })

  const discovery = useQuery({
    queryKey: ['wisdom-discovery', scope],
    queryFn: () => getWisdomDiscovery(profile),
    staleTime: 30_000,
    enabled: status.data?.configured === true
  })

  const candidates = useQuery({
    queryKey: ['wisdom-candidates', scope],
    queryFn: () => getWisdomCandidates(profile),
    staleTime: 15_000,
    enabled: status.data?.configured === true
  })

  const drafts = useQuery({
    queryKey: ['wisdom-drafts', scope],
    queryFn: () => getWisdomDrafts(profile),
    staleTime: 15_000,
    enabled: status.data?.configured === true
  })

  const detail = useQuery({
    queryKey: ['wisdom-detail', scope, selectedId],
    queryFn: () => getWisdomSkill(selectedId || '', profile),
    enabled: status.data?.configured === true && Boolean(selectedId),
    staleTime: 30_000
  })

  const installations = useQuery({
    queryKey: ['wisdom-installations', scope],
    queryFn: () => getWisdomInstallations(profile),
    staleTime: 10_000,
    enabled: status.data?.configured === true
  })

  const latestSelectedVersion = useMemo(
    () => Math.max(0, ...(detail.data?.versions ?? []).map(version => Number(version.version) || 0)),
    [detail.data?.versions]
  )

  const content = useQuery({
    queryKey: ['wisdom-content', scope, selectedId, latestSelectedVersion],
    queryFn: () => getWisdomVersionContent(selectedId || '', latestSelectedVersion, profile),
    enabled: Boolean(selectedId && latestSelectedVersion),
    staleTime: 60_000
  })

  const rows = useMemo(() => {
    const needle = query.trim().toLocaleLowerCase()

    return (discovery.data?.skills ?? []).filter(
      skill =>
        !needle ||
        skill.slug.toLocaleLowerCase().includes(needle) ||
        (skill.author_description ?? '').toLocaleLowerCase().includes(needle)
    )
  }, [discovery.data?.skills, query])

  const prepare = async (skill: string) => {
    setBusy(skill)

    try {
      const result = await suggestWisdomSkill(skill, profile)

      if ('network_submission' in result) {
        setPrepared({ ...result, skill })
        setDescription(result.drafted_description)
        setSpecification(JSON.stringify(result.system_specification, null, 2))
      }
    } catch (error) {
      notifyError(error, 'Collective Wisdom preparation failed')
    } finally {
      setBusy(null)
    }
  }

  const submit = async () => {
    if (!prepared) {
      return
    }

    setBusy(prepared.local_draft_id)

    try {
      const systemSpecification = JSON.parse(specification) as Record<string, unknown>
      await suggestWisdomSkill(prepared.skill, profile, { description, systemSpecification })
      setPrepared(null)
      await drafts.refetch()
    } catch (error) {
      notifyError(error, 'Owner-private submission failed')
    } finally {
      setBusy(null)
    }
  }

  const openReview = async (draftId: string) => {
    setBusy(draftId)

    try {
      setReview(await reviewWisdomDraft(draftId, false, profile))
    } catch (error) {
      notifyError(error, 'Wisdom review failed')
    } finally {
      setBusy(null)
    }
  }

  const approve = async () => {
    if (!review) {
      return
    }

    setBusy(review.draft.id)

    try {
      const acknowledged = await reviewWisdomDraft(review.draft.id, true, profile)

      if (!acknowledged.receipt) {
        throw new Error('Gateway review receipt was not created')
      }

      await decideWisdomDraft(review.draft.id, 'approve', profile)
      setReview(null)
      await Promise.all([drafts.refetch(), discovery.refetch()])
    } catch (error) {
      notifyError(error, 'Wisdom publication failed')
    } finally {
      setBusy(null)
    }
  }

  const installed = installations.data?.installations.find(
    item => item.skill_id === selectedId && item.state === 'active'
  )

  const planManagedAction = async (action: 'install' | 'uninstall' | 'update') => {
    if (!selectedId) {
      return
    }

    setBusy(selectedId)

    try {
      const plan =
        action === 'install'
          ? await planWisdomInstall(selectedId, profile)
          : action === 'update'
            ? await planWisdomUpdate(selectedId, profile)
            : { skill_id: selectedId, state: 'confirm_uninstall' }

      setActionPlan({ ...plan, action })
      setAcceptSensitive(false)
      setAcceptPartial(false)
      setPreserveModified(false)
    } catch (error) {
      notifyError(error, `Wisdom ${action} planning failed`)
    } finally {
      setBusy(null)
    }
  }

  const applyManagedAction = async () => {
    if (!actionPlan) {
      return
    }

    setBusy(actionPlan.skill_id)

    try {
      if (actionPlan.action === 'uninstall') {
        await uninstallWisdomSkill(actionPlan.skill_id, profile)
      } else if (!actionPlan.receipt) {
        throw new Error('Verified action receipt is missing')
      } else if (actionPlan.action === 'install') {
        await applyWisdomInstall(actionPlan.receipt, acceptPartial, profile)
      } else {
        await applyWisdomUpdate(actionPlan.receipt, { acceptPartial, acceptSensitive, preserveModified }, profile)
      }

      setActionPlan(null)
      await Promise.all([installations.refetch(), detail.refetch()])
    } catch (error) {
      notifyError(error, `Wisdom ${actionPlan.action} failed`)
    } finally {
      setBusy(null)
    }
  }

  const setupProfile = async () => {
    setBusy('setup')

    try {
      const action = await setupWisdom(profile)
      await waitForWisdomAction(action.name, profile)
      await status.refetch()
      await Promise.all([discovery.refetch(), candidates.refetch(), drafts.refetch(), installations.refetch()])
    } catch (error) {
      notifyError(error, 'Collective Wisdom setup failed')
    } finally {
      setBusy(null)
    }
  }

  if (status.isPending) {
    return <div className="grid h-full place-items-center text-xs text-muted-foreground">{copy.loading}</div>
  }

  if (status.isError) {
    const error = status.error

    return (
      <div className="grid h-full place-items-center px-8 text-center text-xs text-muted-foreground">
        {copy.unavailable} {error instanceof Error ? error.message : ''}
      </div>
    )
  }

  if (!status.data.configured) {
    return (
      <div className="grid h-full place-items-center p-8">
        <section
          aria-label={copy.title}
          className="max-w-xl space-y-4 border border-(--ui-stroke-tertiary) p-5 text-sm"
        >
          <div>
            <h2 className="font-medium">{copy.title}</h2>
            <p className="mt-1 text-xs text-muted-foreground">{copy.setup}</p>
          </div>
          <p className="text-xs leading-5 text-muted-foreground">{copy.setupDisclosure}</p>
          {status.data.error && (
            <div className="text-xs text-destructive" role="alert">
              {status.data.error}
            </div>
          )}
          <Button disabled={busy === 'setup'} onClick={setupProfile} size="sm">
            {busy === 'setup' ? copy.settingUp : copy.setupAction}
          </Button>
        </section>
      </div>
    )
  }

  if (discovery.isPending || candidates.isPending || drafts.isPending || installations.isPending) {
    return <div className="grid h-full place-items-center text-xs text-muted-foreground">{copy.loading}</div>
  }

  if (discovery.isError || candidates.isError || drafts.isError || installations.isError) {
    const error = discovery.error || candidates.error || drafts.error || installations.error

    return (
      <div className="grid h-full place-items-center px-8 text-center text-xs text-muted-foreground">
        {copy.unavailable} {error instanceof Error ? error.message : ''}
      </div>
    )
  }

  const statusCopy = status.data?.verified_org_id ? `${status.data.verified_org_id} · ${copy.orgWide}` : copy.setup

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="shrink-0 border-b border-(--ui-stroke-tertiary) px-3 py-2">
        <div className="flex items-center justify-between gap-3">
          <div>
            <div className="text-xs font-medium">{copy.title}</div>
            <div className="text-[0.65rem] text-muted-foreground">{statusCopy}</div>
          </div>
          <div className="flex gap-2">
            <Button
              onClick={async () => {
                setBusy('scan')

                try {
                  const action = await scanWisdom(undefined, profile)
                  await waitForWisdomAction(action.name, profile)
                  await candidates.refetch()
                } catch (error) {
                  notifyError(error, 'Wisdom local scan failed')
                } finally {
                  setBusy(null)
                }
              }}
              size="sm"
              variant="outline"
            >
              {busy === 'scan' ? copy.checking : copy.scanLocal}
            </Button>
            <Button
              onClick={async () => {
                setBusy('check')

                try {
                  await checkWisdom(profile)
                  await installations.refetch()
                } catch (error) {
                  notifyError(error, 'Wisdom update check failed')
                } finally {
                  setBusy(null)
                }
              }}
              size="sm"
              variant="outline"
            >
              {busy === 'check' ? copy.checking : copy.checkUpdates(installations.data.notifications.length)}
            </Button>
          </div>
        </div>
        {installations.data.notifications.length > 0 && (
          <div className="mt-2 flex items-start justify-between gap-3 border-t border-(--ui-stroke-tertiary) pt-2 text-[0.65rem]">
            <ul className="min-w-0 space-y-1 text-muted-foreground">
              {installations.data.notifications.slice(0, 4).map((event, index) => (
                <li className="truncate" key={String(event.event_id ?? index)}>
                  {String(event.kind ?? 'update')} · {String(event.skill_id ?? 'skill')}
                  {event.version ? ` · v${String(event.version)}` : ''}
                </li>
              ))}
            </ul>
            <Button
              onClick={async () => {
                try {
                  await acknowledgeWisdomNotifications(profile)
                  await installations.refetch()
                } catch (error) {
                  notifyError(error, 'Could not acknowledge Wisdom notifications')
                }
              }}
              size="sm"
              variant="outline"
            >
              {copy.markSeen}
            </Button>
          </div>
        )}
      </div>
      <MasterDetail resizeId="collective-capabilities-split" split="wide">
        <ListColumn
          header={
            <ListStrip
              left={<span className="text-[0.68rem] text-muted-foreground">{copy.sharedSkills(rows.length)}</span>}
              right={
                candidates.data && candidates.data.candidates.length > 0 ? (
                  <span className="text-[0.62rem] text-muted-foreground">
                    {copy.localCandidates(candidates.data.candidates.length)}
                  </span>
                ) : undefined
              }
            />
          }
        >
          {rows.map(skill => (
            <button
              className={cn(
                'row-hover flex h-12 w-full shrink-0 items-center rounded-md px-2 text-left',
                selectedId === skill.id && 'bg-(--ui-row-active-background)'
              )}
              key={skill.id}
              onClick={() => setSelectedId(skill.id)}
              type="button"
            >
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[0.78rem] font-medium">{skill.slug}</span>
                <span className="block truncate text-[0.62rem] text-muted-foreground">
                  {skill.author_description || copy.noDescription}
                </span>
              </span>
              <span className="ml-2 shrink-0 text-[0.6rem] text-emerald-600">{copy.serverScanPassed}</span>
            </button>
          ))}
          {rows.length === 0 && (
            <div className="px-3 py-8 text-center text-xs text-muted-foreground">{copy.noShared}</div>
          )}
        </ListColumn>
        <DetailColumn footer={copy.authoritative}>
          <div className="space-y-5 p-4">
            {(candidates.data?.candidates ?? []).slice(0, 5).map(candidate => (
              <div
                className="flex items-start gap-3 border-b border-(--ui-stroke-tertiary) pb-3"
                key={candidate.local_skill_id}
              >
                <div className="min-w-0 flex-1">
                  <div className="truncate font-mono text-xs">{candidate.name}</div>
                  <div className="text-[0.65rem] text-muted-foreground">{candidate.reason || copy.localOnly}</div>
                </div>
                <Button
                  disabled={busy === candidate.name || candidate.eligibility !== 'eligible'}
                  onClick={() => void prepare(candidate.name)}
                  size="sm"
                  variant="outline"
                >
                  {copy.prepare}
                </Button>
              </div>
            ))}

            {(drafts.data?.drafts ?? []).map(draft => (
              <button
                className="flex w-full items-center justify-between border-b border-(--ui-stroke-tertiary) pb-3 text-left"
                key={draft.id}
                onClick={() => void openReview(draft.id)}
                type="button"
              >
                <span>
                  <span className="block font-mono text-xs">{draft.slug}</span>
                  <span className="text-[0.65rem] text-muted-foreground">{draft.state}</span>
                </span>
                <span className="text-[0.65rem]">{copy.reviewExact}</span>
              </button>
            ))}

            {detail.data && (
              <section aria-label="Collective Wisdom skill detail">
                <h2 className="font-mono text-base">{String(detail.data.skill.slug || detail.data.skill.id)}</h2>
                <p className="mt-2 text-sm text-muted-foreground">
                  {String(detail.data.skill.authorDescription || detail.data.skill.author_description || '')}
                </p>
                <h3 className="mt-5 text-xs font-medium">{copy.versionHistory}</h3>
                <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap text-[0.67rem] text-muted-foreground">
                  {JSON.stringify(
                    {
                      latest_version: detail.data.latest_version_detail,
                      version_history: detail.data.versions,
                      local_compatibility: detail.data.local_compatibility
                    },
                    null,
                    2
                  )}
                </pre>
                {content.data && (
                  <div className="mt-4">
                    <div className="break-all font-mono text-[0.62rem]">content {content.data.content_hash}</div>
                    {content.data.files.map(file => (
                      <details className="border-t border-(--ui-stroke-tertiary) py-2" key={file.path} open>
                        <summary className="cursor-pointer font-mono text-[0.68rem]">
                          {file.path} · {file.hash}
                        </summary>
                        <pre className="mt-2 max-h-72 overflow-auto whitespace-pre-wrap text-[0.65rem]">
                          {file.content_utf8}
                        </pre>
                      </details>
                    ))}
                  </div>
                )}
                <div className="mt-4 flex flex-wrap items-center justify-end gap-2">
                  {installed ? (
                    <>
                      <span className="text-[0.62rem] text-muted-foreground">
                        {copy.installed(installed.version, installed.update_mode)}
                      </span>
                      <Button onClick={() => void planManagedAction('uninstall')} size="sm" variant="outline">
                        {copy.uninstall}
                      </Button>
                      <Button onClick={() => void planManagedAction('update')} size="sm">
                        {copy.checkSkill}
                      </Button>
                    </>
                  ) : (
                    <Button onClick={() => void planManagedAction('install')} size="sm">
                      {copy.install}
                    </Button>
                  )}
                </div>
              </section>
            )}
          </div>
        </DetailColumn>
      </MasterDetail>

      {prepared && (
        <div className="absolute inset-6 z-20 overflow-auto border border-(--ui-stroke-secondary) bg-background p-5 shadow-xl">
          <h2 className="font-mono text-sm">{copy.prepareTitle}</h2>
          <p className="mt-1 text-xs text-muted-foreground">{copy.prepareNotice}</p>
          <label className="mt-4 block text-xs" htmlFor="desktop-wisdom-description">
            {copy.ownerDescription}
          </label>
          <textarea
            className="mt-1 min-h-20 w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent p-2 text-xs"
            id="desktop-wisdom-description"
            maxLength={4096}
            onChange={event => setDescription(event.target.value)}
            value={description}
          />
          <label className="mt-4 block text-xs" htmlFor="desktop-wisdom-spec">
            {copy.systemSpecification}
          </label>
          <textarea
            className="mt-1 min-h-56 w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent p-2 font-mono text-[0.67rem]"
            id="desktop-wisdom-spec"
            onChange={event => setSpecification(event.target.value)}
            spellCheck={false}
            value={specification}
          />
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={() => setPrepared(null)} size="sm" variant="outline">
              {copy.cancel}
            </Button>
            <Button disabled={busy === prepared.local_draft_id} onClick={() => void submit()} size="sm">
              {copy.submit}
            </Button>
          </div>
        </div>
      )}

      {review && (
        <div className="absolute inset-6 z-20 overflow-auto border border-emerald-600/50 bg-background p-5 shadow-xl">
          <h2 className="font-mono text-sm">{review.draft.slug}</h2>
          <p className="mt-1 text-xs text-muted-foreground">{copy.readEvery}</p>
          <div className="mt-3 grid gap-3 border-y border-(--ui-stroke-tertiary) py-3 text-xs">
            <div>
              <strong>{copy.ownerCopyLabel}</strong>
              <p className="mt-1 whitespace-pre-wrap text-muted-foreground">
                {review.draft.authorDescription || copy.noDescription}
              </p>
            </div>
            <div>
              <strong>{copy.serverFactsLabel}</strong>
              <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-muted-foreground">
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
              <strong>{copy.systemSpecification}</strong>
              <pre className="mt-1 max-h-48 overflow-auto whitespace-pre-wrap text-muted-foreground">
                {JSON.stringify(review.draft.systemSpec, null, 2)}
              </pre>
            </div>
          </div>
          <div className="my-3 grid gap-1 break-all font-mono text-[0.62rem]">
            <span>content {review.hashes.content}</span>
            <span>author description {review.hashes.author_description}</span>
            <span>package manifest {review.hashes.package_manifest}</span>
          </div>
          {review.files.map(file => (
            <details className="border-t border-(--ui-stroke-tertiary) py-2" key={file.path} open>
              <summary className="cursor-pointer font-mono text-xs">
                {file.path} · {file.hash}
              </summary>
              <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-[0.67rem]">{file.content_utf8}</pre>
            </details>
          ))}
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={() => setReview(null)} size="sm" variant="outline">
              {copy.close}
            </Button>
            <Button
              disabled={busy === review.draft.id}
              onClick={async () => {
                setBusy(review.draft.id)

                try {
                  await decideWisdomDraft(review.draft.id, 'decline', profile)
                  setReview(null)
                  await drafts.refetch()
                } catch (error) {
                  notifyError(error, 'Wisdom decline failed')
                } finally {
                  setBusy(null)
                }
              }}
              size="sm"
              variant="outline"
            >
              {copy.decline}
            </Button>
            <Button disabled={busy === review.draft.id} onClick={() => void approve()} size="sm">
              {copy.approve}
            </Button>
          </div>
        </div>
      )}

      {actionPlan && (
        <div
          aria-label="Verified managed action plan"
          className="absolute inset-6 z-30 overflow-auto border border-amber-600/50 bg-background p-5 shadow-xl"
          role="dialog"
        >
          <h2 className="font-mono text-sm">{copy.confirmAction(actionPlan.action)}</h2>
          <pre className="mt-3 max-h-64 overflow-auto whitespace-pre-wrap text-[0.65rem]">
            {JSON.stringify(actionPlan, null, 2)}
          </pre>
          {actionPlan.state === 'current' && <p className="mt-3 text-xs">{copy.alreadyCurrent}</p>}
          {actionPlan.compatibility && actionPlan.compatibility.outcome !== 'compatible' && (
            <label className="mt-3 flex gap-2 text-xs">
              <input
                checked={acceptPartial}
                onChange={event => setAcceptPartial(event.target.checked)}
                type="checkbox"
              />
              {copy.acceptCompatibility}
            </label>
          )}
          {(actionPlan.sensitive_expansion?.length ?? 0) > 0 && (
            <label className="mt-2 flex gap-2 text-xs">
              <input
                checked={acceptSensitive}
                onChange={event => setAcceptSensitive(event.target.checked)}
                type="checkbox"
              />
              {copy.acceptSensitive}
            </label>
          )}
          {actionPlan.modified && actionPlan.update_mode !== 'REQUIRED' && (
            <label className="mt-2 flex gap-2 text-xs">
              <input
                checked={preserveModified}
                onChange={event => setPreserveModified(event.target.checked)}
                type="checkbox"
              />
              {copy.preserveModified}
            </label>
          )}
          <div className="mt-4 flex justify-end gap-2">
            <Button onClick={() => setActionPlan(null)} size="sm" variant="outline">
              Cancel
            </Button>
            {actionPlan.state !== 'current' && (
              <Button onClick={() => void applyManagedAction()} size="sm">
                {copy.confirmAction(actionPlan.action)}
              </Button>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
