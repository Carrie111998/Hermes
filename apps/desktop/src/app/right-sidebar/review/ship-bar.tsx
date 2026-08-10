import { useStore } from '@nanostores/react'
import { useState } from 'react'

import { requestComposerSubmit } from '@/app/chat/composer/focus'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { ConfirmDialog } from '@/components/ui/confirm-dialog'
import { GenerateButton } from '@/components/ui/generate-button'
import { Textarea } from '@/components/ui/textarea'
import { Tip } from '@/components/ui/tooltip'
import { useI18n } from '@/i18n'
import { notifyError } from '@/store/notifications'
import {
  $reviewCommitMsgBusy,
  $reviewFiles,
  $reviewPushRequest,
  $reviewShipBusy,
  $reviewShipInfo,
  cancelCommitMessage,
  cancelPushApproval,
  commitChanges,
  confirmPushApproval,
  createOrOpenPr,
  generateCommitMessage,
  requestPushApproval
} from '@/store/review'

const ICON = '0.85rem'

const SAFE_AGENT_SHIP_PROMPT =
  'Run the relevant tests, create a local commit, then stop and request push approval. Do not push or create a pull request.'

export function ReviewShipBar() {
  const { t } = useI18n()
  const c = t.statusStack.coding
  const files = useStore($reviewFiles)
  const ship = useStore($reviewShipInfo)
  const pushRequest = useStore($reviewPushRequest)
  const busy = useStore($reviewShipBusy)
  const generating = useStore($reviewCommitMsgBusy)
  const [message, setMessage] = useState('')
  const prLabel = ship.pr?.url ? c.openPr : c.createPr

  const hasFiles = files.length > 0
  const canCommit = hasFiles && message.trim().length > 0 && !busy
  const canGenerate = hasFiles && !generating && !busy

  if (!hasFiles && !ship.pushAvailable) {
    return null
  }

  const runCommit = () => {
    if (!canCommit) {
      return
    }

    void commitChanges(message)
      .then(() => setMessage(''))
      .catch(err => notifyError(err, c.commit))
  }

  const runGenerate = () => {
    if (!canGenerate) {
      return
    }

    void generateCommitMessage(message)
      .then(text => text && setMessage(text))
      .catch(err => notifyError(err, c.generateCommitMessage))
  }

  return (
    <>
      <div className="flex shrink-0 flex-col gap-1.5 p-2" data-suppress-pane-reveal-side="">
        {hasFiles && (
          <>
            <div className="relative">
              <Textarea
                className="field-sizing-content max-h-40 min-h-0 resize-none pr-9"
                disabled={generating}
                onChange={event => setMessage(event.target.value)}
                onKeyDown={event => {
                  if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
                    event.preventDefault()
                    runCommit()
                  }
                }}
                placeholder={c.commitPlaceholder}
                rows={1}
                size="sm"
                value={message}
              />
              <GenerateButton
                className="absolute top-px right-px h-6 w-8 rounded-l-none rounded-r-[2px]"
                disabled={!canGenerate}
                generating={generating}
                generatingLabel={c.stopGenerating}
                iconSize={ICON}
                label={c.generateCommitMessage}
                onCancel={cancelCommitMessage}
                onGenerate={runGenerate}
              />
            </div>

            <Button disabled={!canCommit} onClick={runCommit} size="sm">
              <Codicon name="check" size={ICON} />
              {c.commit}
            </Button>
          </>
        )}

        {ship.pushAvailable && (
          <Button
            disabled={busy}
            onClick={() => void requestPushApproval().catch(err => notifyError(err, 'Push approval'))}
            size="sm"
            variant="secondary"
          >
            <Codicon name="cloud-upload" size={ICON} />
            Review and approve push
          </Button>
        )}

        <div className="relative flex min-w-0 items-center">
          {hasFiles && (
            <Button
              className="min-w-0 flex-1 justify-center px-7 text-[0.7rem] text-muted-foreground/85 hover:text-foreground"
              disabled={busy}
              onClick={() => requestComposerSubmit(SAFE_AGENT_SHIP_PROMPT, { target: 'main' })}
              size="sm"
              variant="ghost"
            >
              <span className="truncate underline underline-offset-2">Prepare with agent</span>
            </Button>
          )}
          <Tip label={ship.ghReady ? prLabel : c.ghMissing}>
            <span className="absolute inset-y-0 right-0 flex items-center">
              <Button
                aria-label={prLabel}
                className="size-7 text-muted-foreground/80 hover:text-foreground"
                disabled={!ship.ghReady || busy || ship.pushAvailable}
                onClick={() => void createOrOpenPr().catch(err => notifyError(err, prLabel))}
                size="icon-xs"
                variant="ghost"
              >
                <Codicon name="git-pull-request" size={ICON} />
              </Button>
            </span>
          </Tip>
        </div>
      </div>

      <ConfirmDialog
        busyLabel="Pushing approved commit…"
        confirmLabel="Approve exact push"
        description={
          pushRequest ? (
            <span className="mt-2 grid gap-1.5 text-left font-mono text-[0.68rem]">
              <span>Commit: {pushRequest.commitSha}</span>
              <span>Target: {pushRequest.remoteUrl} → {pushRequest.destinationBranch}</span>
              <span>Destination fingerprint: {pushRequest.remoteUrlDigest.slice(0, 16)}…</span>
              <span title={pushRequest.changeSetDigest}>Digest: {pushRequest.changeSetDigest.slice(0, 20)}…</span>
              <span>Expires: {new Date(pushRequest.expiresAt).toLocaleString()}</span>
            </span>
          ) : undefined
        }
        doneLabel="Push complete"
        onClose={cancelPushApproval}
        onConfirm={confirmPushApproval}
        open={Boolean(pushRequest)}
        title="Approve Git push"
      />
    </>
  )
}
