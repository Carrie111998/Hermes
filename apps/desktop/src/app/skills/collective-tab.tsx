import { useQuery } from '@tanstack/react-query'
import { useMemo, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  decideWisdomDraft,
  getWisdomCandidates,
  getWisdomDiscovery,
  getWisdomDrafts,
  getWisdomSkill,
  getWisdomStatus,
  type ProfileScope,
  profileScopeKey,
  reviewWisdomDraft,
  suggestWisdomSkill,
  type WisdomDraftReview,
  type WisdomPreparedDraft
} from '@/hermes'
import { useI18n } from '@/i18n'
import { cn } from '@/lib/utils'
import { notifyError } from '@/store/notifications'

import { DetailColumn, ListColumn, ListStrip, MasterDetail } from '../master-detail'

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

  const status = useQuery({
    queryKey: ['wisdom-status', scope],
    queryFn: () => getWisdomStatus(profile),
    staleTime: 30_000
  })

  const discovery = useQuery({
    queryKey: ['wisdom-discovery', scope],
    queryFn: () => getWisdomDiscovery(profile),
    staleTime: 30_000
  })

  const candidates = useQuery({
    queryKey: ['wisdom-candidates', scope],
    queryFn: () => getWisdomCandidates(profile),
    staleTime: 15_000
  })

  const drafts = useQuery({
    queryKey: ['wisdom-drafts', scope],
    queryFn: () => getWisdomDrafts(profile),
    staleTime: 15_000
  })

  const detail = useQuery({
    queryKey: ['wisdom-detail', scope, selectedId],
    queryFn: () => getWisdomSkill(selectedId || '', profile),
    enabled: Boolean(selectedId),
    staleTime: 30_000
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
    if (!prepared) {return}
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
    if (!review) {return}
    setBusy(review.draft.id)

    try {
      const acknowledged = await reviewWisdomDraft(review.draft.id, true, profile)

      if (!acknowledged.receipt) {throw new Error('Gateway review receipt was not created')}
      await decideWisdomDraft(review.draft.id, 'approve', profile)
      setReview(null)
      await Promise.all([drafts.refetch(), discovery.refetch()])
    } catch (error) {
      notifyError(error, 'Wisdom publication failed')
    } finally {
      setBusy(null)
    }
  }

  if (status.isPending || discovery.isPending || candidates.isPending || drafts.isPending) {
    return <div className="grid h-full place-items-center text-xs text-muted-foreground">{copy.loading}</div>
  }

  if (status.isError || discovery.isError || candidates.isError || drafts.isError) {
    const error = status.error || discovery.error || candidates.error || drafts.error

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
        <div className="text-xs font-medium">{copy.title}</div>
        <div className="text-[0.65rem] text-muted-foreground">{statusCopy}</div>
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
                  {JSON.stringify(detail.data.versions, null, 2)}
                </pre>
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
            <Button disabled={busy === review.draft.id} onClick={() => void approve()} size="sm">
              {copy.approve}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
