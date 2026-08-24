import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import {
  decideWisdomDraft,
  getWisdomEvents,
  type ProfileScope,
  reviewWisdomDraft,
  suggestWisdomSkill,
  type WisdomCandidateEvent,
  type WisdomDraftReview,
  type WisdomLocalScan,
  type WisdomPreparedDraft
} from '@/hermes'
import { useI18n } from '@/i18n'
import { notifyError } from '@/store/notifications'

const MAX_INLINE_REVIEW_BYTES = 256 * 1024

function SafeSkillPreview({ text }: { text: string }) {
  return (
    <div className="space-y-2 whitespace-pre-wrap text-xs text-muted-foreground">
      {text.split('\n').map((line, index) => {
        const heading = /^(#{1,4})\s+(.+)$/.exec(line)

        if (heading) {
          return <h3 className="font-medium text-foreground" key={index}>{heading[2]}</h3>
        }

        if (/^[-*+]\s+/.test(line)) {
          return <div className="pl-3" key={index}>• {line.replace(/^[-*+]\s+/, '')}</div>
        }

        return <div key={index}>{line || '\u00a0'}</div>
      })}
    </div>
  )
}

export function WisdomCandidateCard({ profile, sessionId }: { profile?: ProfileScope; sessionId: string }) {
  const { t } = useI18n()
  const copy = t.skills.collective
  const [event, setEvent] = useState<null | WisdomCandidateEvent>(null)
  const [prepared, setPrepared] = useState<null | (WisdomPreparedDraft & { skill: string })>(null)
  const [description, setDescription] = useState('')
  const [specification, setSpecification] = useState('')
  const [review, setReview] = useState<null | WisdomDraftReview>(null)
  const [localScan, setLocalScan] = useState<null | WisdomLocalScan>(null)
  const [reviewMode, setReviewMode] = useState<'raw' | 'rendered'>('raw')
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    let active = true

    const refresh = async () => {
      try {
        const result = await getWisdomEvents(sessionId, profile)

        if (active) {
          setEvent(result.events[0] ?? null)
        }
      } catch {
        // Candidate promotion is an optional transcript enhancement. An
        // unavailable Wisdom plane must never make ordinary chat unusable.
        if (active) {
          setEvent(null)
        }
      }
    }

    void refresh()
    const timer = window.setInterval(() => void refresh(), 10_000)

    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [profile, sessionId])

  if (!event) {return null}

  const skill = event.payload.skill_name

  const openFullReview = () => {
    window.history.pushState(null, '', '/skills?tab=collective')
    window.dispatchEvent(new PopStateEvent('popstate'))
  }

  const prepare = async () => {
    setBusy(true)

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
      setBusy(false)
    }
  }

  const submit = async () => {
    if (!prepared) {return}
    setBusy(true)

    try {
      const result = await suggestWisdomSkill(prepared.skill, profile, {
        description,
        systemSpecification: JSON.parse(specification) as Record<string, unknown>
      })

      if (!('draft' in result)) {throw new Error('Gateway did not return an owner-private draft')}
      setLocalScan(result.local_scan)
      const exact = await reviewWisdomDraft(result.draft.id, false, profile)
      const size = exact.files.reduce((total, file) => total + new Blob([file.content_utf8]).size, 0)

      if (size > MAX_INLINE_REVIEW_BYTES) {
        openFullReview()

        return
      }

      setReview(exact)
      setReviewMode('raw')
      setPrepared(null)
    } catch (error) {
      notifyError(error, 'Owner-private submission failed')
    } finally {
      setBusy(false)
    }
  }

  const decide = async (decision: 'approve' | 'decline') => {
    if (!review) {return}
    setBusy(true)

    try {
      if (decision === 'approve') {
        const acknowledged = await reviewWisdomDraft(review.draft.id, true, profile)

        if (!acknowledged.receipt) {throw new Error('Complete-package review receipt was not created')}
      }

      await decideWisdomDraft(review.draft.id, decision, profile)
      setReview(null)
      setEvent(null)
    } catch (error) {
      notifyError(error, `Collective Wisdom ${decision} failed`)
    } finally {
      setBusy(false)
    }
  }

  return (
    <section
      aria-label="Collective Wisdom contribution proposal"
      className="mb-(--conversation-turn-gap) rounded-lg border border-emerald-600/40 bg-(--ui-chat-surface-background)"
    >
      <header className="flex items-center justify-between border-b border-(--ui-stroke-tertiary) px-4 py-3">
        <div>
          <div className="text-xs font-medium">{copy.proposalTitle}</div>
          <div className="font-mono text-[0.68rem] text-muted-foreground">{skill}</div>
        </div>
        <span className="text-[0.62rem] text-muted-foreground">{copy.localSuggestion}</span>
      </header>

      {!prepared && !review && (
        <div className="p-4">
          <p className="text-xs text-muted-foreground">{copy.proposalNotice}</p>
          <pre className="mt-3 max-h-28 overflow-auto whitespace-pre-wrap rounded-md bg-(--ui-bg-quinary) p-2 text-[0.65rem]">
            {JSON.stringify(event.payload.local_reasons, null, 2)}
          </pre>
          <div className="mt-3 flex justify-end gap-2">
            <Button onClick={openFullReview} size="sm" variant="outline">
              {copy.openCollective}
            </Button>
            <Button disabled={busy} onClick={() => void prepare()} size="sm">
              {copy.prepareExact}
            </Button>
          </div>
        </div>
      )}

      {prepared && (
        <div className="p-4">
          <p className="text-xs text-muted-foreground">{copy.specificationNotice}</p>
          <label className="mt-3 block text-[0.68rem]" htmlFor={`wisdom-description-${event.id}`}>
            {copy.ownerDescription}
          </label>
          <textarea
            className="mt-1 min-h-20 w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent p-2 text-xs"
            id={`wisdom-description-${event.id}`}
            maxLength={4096}
            onChange={input => setDescription(input.target.value)}
            value={description}
          />
          <label className="mt-3 block text-[0.68rem]" htmlFor={`wisdom-spec-${event.id}`}>
            {copy.systemSpecification}
          </label>
          <textarea
            className="mt-1 min-h-48 w-full rounded-md border border-(--ui-stroke-secondary) bg-transparent p-2 font-mono text-[0.65rem]"
            id={`wisdom-spec-${event.id}`}
            onChange={input => setSpecification(input.target.value)}
            spellCheck={false}
            value={specification}
          />
          <div className="mt-3 flex justify-end gap-2">
            <Button onClick={openFullReview} size="sm" variant="outline">
              {copy.openFullReview}
            </Button>
            <Button disabled={busy} onClick={() => void submit()} size="sm">
              {copy.sendPrivateReview}
            </Button>
          </div>
        </div>
      )}

      {review && (
        <div className="p-4">
          <div className="flex flex-wrap gap-2 text-[0.65rem]">
            <span className="rounded-full bg-emerald-600/10 px-2 py-1 text-emerald-700">
              {copy.serverEnforced}: {review.draft.scanVerdict || 'reviewed'}
            </span>
            <span className="rounded-full bg-(--ui-bg-quinary) px-2 py-1">
              {copy.localAdvisory}: {localScan ? 'available' : 'unavailable'}
            </span>
          </div>
          <p className="mt-3 text-xs text-muted-foreground">{copy.serverReviewNotice}</p>
          <div className="mt-3 grid gap-3 border-y border-(--ui-stroke-tertiary) py-3 text-[0.68rem]">
            <div>
              <strong>{copy.ownerCopyLabel}</strong>
              <p className="mt-1 whitespace-pre-wrap text-muted-foreground">
                {review.draft.authorDescription || copy.noDescription}
              </p>
            </div>
            <div>
              <strong>{copy.serverFactsLabel}</strong>
              <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-muted-foreground">
                {JSON.stringify({
                  verdict: review.draft.scanVerdict,
                  scan: review.draft.scan,
                  explanation: review.draft.explanation
                }, null, 2)}
              </pre>
            </div>
            <div>
              <strong>{copy.localAdvisory}</strong>
              <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-muted-foreground">
                {localScan ? JSON.stringify(localScan, null, 2) : 'Advisory scan unavailable.'}
              </pre>
            </div>
            <div>
              <strong>{copy.systemSpecification}</strong>
              <pre className="mt-1 max-h-40 overflow-auto whitespace-pre-wrap text-muted-foreground">
                {JSON.stringify(review.draft.systemSpec, null, 2)}
              </pre>
            </div>
          </div>
          <div className="my-3 grid gap-1 break-all font-mono text-[0.6rem]">
            <span>content {review.hashes.content}</span>
            <span>author description {review.hashes.author_description}</span>
            <span>package manifest {review.hashes.package_manifest}</span>
          </div>
          <div className="mb-2 flex justify-end gap-1">
            <Button onClick={() => setReviewMode('raw')} size="sm" variant={reviewMode === 'raw' ? 'default' : 'outline'}>Raw</Button>
            <Button onClick={() => setReviewMode('rendered')} size="sm" variant={reviewMode === 'rendered' ? 'default' : 'outline'}>Rendered</Button>
          </div>
          {reviewMode === 'rendered' && (
            <div className="border-t border-(--ui-stroke-tertiary) py-3">
              <SafeSkillPreview text={review.files.find(file => file.path === 'SKILL.md')?.content_utf8 || ''} />
            </div>
          )}
          {review.files
            .filter(file => reviewMode === 'raw' || file.path !== 'SKILL.md')
            .map(file => (
              <details className="border-t border-(--ui-stroke-tertiary) py-2" key={file.path} open>
                <summary className="cursor-pointer font-mono text-[0.68rem]">
                  {file.path} · {file.hash}
                </summary>
                <pre className="mt-2 max-h-64 overflow-auto whitespace-pre-wrap text-[0.65rem]">{file.content_utf8}</pre>
              </details>
            ))}
          <footer className="mt-3 flex justify-end gap-2 border-t border-(--ui-stroke-tertiary) pt-3">
            <Button disabled={busy} onClick={() => void decide('decline')} size="sm" variant="outline">
              {copy.decline}
            </Button>
            <Button disabled={busy} onClick={() => void decide('approve')} size="sm">
              {copy.approvePublish}
            </Button>
          </footer>
        </div>
      )}
    </section>
  )
}
