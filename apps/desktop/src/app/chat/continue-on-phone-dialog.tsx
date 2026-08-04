import { useStore } from '@nanostores/react'
import * as QRCode from 'qrcode'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { CopyButton } from '@/components/ui/copy-button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle
} from '@/components/ui/dialog'
import { ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { useI18n } from '@/i18n'
import {
  type ContinueOnPhoneFailureReason,
  type ContinueOnPhoneResult,
  resolveContinueOnPhoneUrl
} from '@/lib/continue-on-phone'
import { notifyError } from '@/store/notifications'

import { $continueOnPhoneTarget, closeContinueOnPhone } from './continue-on-phone-state'

type ResolveUrl = (sessionId: string, profile?: string) => Promise<ContinueOnPhoneResult>
type GenerateQr = (url: string) => Promise<string>

interface ContinueOnPhoneDialogProps {
  generateQr?: GenerateQr
  onOpenChange: (open: boolean) => void
  open: boolean
  profile?: string
  resolveUrl?: ResolveUrl
  sessionId: string
}

type DialogState =
  | { phase: 'error'; reason: ContinueOnPhoneFailureReason }
  | { phase: 'idle' | 'loading' }
  | { expiresAt: number; phase: 'ready'; qrDataUrl: string; url: string }

const generateQrDataUrl: GenerateQr = url =>
  QRCode.toDataURL(url, {
    errorCorrectionLevel: 'M',
    margin: 3,
    width: 240
  })

export function ContinueOnPhoneDialog({
  generateQr = generateQrDataUrl,
  onOpenChange,
  open,
  profile,
  resolveUrl = resolveContinueOnPhoneUrl,
  sessionId
}: ContinueOnPhoneDialogProps) {
  const { t } = useI18n()
  const copy = t.sidebar.row
  const [requestAttempt, setRequestAttempt] = useState(0)
  const [state, setState] = useState<DialogState>({ phase: 'idle' })
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!open) {
      return
    }

    let cancelled = false
    setState({ phase: 'loading' })

    void resolveUrl(sessionId, profile)
      .then(async result => {
        if (!result.ok) {
          return result
        }

        return {
          expiresAt: result.expiresAt,
          ok: true as const,
          qrDataUrl: await generateQr(result.url),
          url: result.url
        }
      })
      .then(result => {
        if (cancelled) {
          return
        }

        setNow(Date.now())
        setState(
          result.ok
            ? { expiresAt: result.expiresAt, phase: 'ready', qrDataUrl: result.qrDataUrl, url: result.url }
            : { phase: 'error', reason: result.reason }
        )
      })
      .catch(() => {
        if (!cancelled) {
          setState({ phase: 'error', reason: 'handoff-failed' })
        }
      })

    return () => {
      cancelled = true
    }
  }, [generateQr, open, profile, requestAttempt, resolveUrl, sessionId])

  const expiresAt = state.phase === 'ready' ? state.expiresAt : null
  const remainingSeconds = expiresAt === null ? 0 : Math.max(0, Math.ceil((expiresAt - now) / 1_000))
  const isExpired = expiresAt !== null && remainingSeconds === 0

  useEffect(() => {
    if (!open || expiresAt === null) {
      return
    }

    const updateNow = () => setNow(Date.now())
    const interval = window.setInterval(updateNow, 1_000)
    document.addEventListener('visibilitychange', updateNow)

    return () => {
      window.clearInterval(interval)
      document.removeEventListener('visibilitychange', updateNow)
    }
  }, [expiresAt, open])

  const createNewCode = () => setRequestAttempt(value => value + 1)

  const handleOpenChange = (nextOpen: boolean) => {
    if (!nextOpen) {
      setState({ phase: 'idle' })
      setNow(Date.now())
    }

    onOpenChange(nextOpen)
  }

  const errorCopy = (reason: ContinueOnPhoneFailureReason) => {
    switch (reason) {
      case 'not-configured':
        return {
          description: copy.continueOnPhoneNotConfiguredDesc,
          retry: false,
          title: copy.continueOnPhoneNotConfiguredTitle
        }

      case 'insecure-url':
        return {
          description: copy.continueOnPhoneInsecureUrlDesc,
          retry: false,
          title: copy.continueOnPhoneInsecureUrlTitle
        }

      case 'unreachable':
        return {
          description: copy.continueOnPhoneUnreachableDesc,
          retry: true,
          title: copy.continueOnPhoneUnreachableTitle
        }

      case 'browser-auth-not-supported':
        return {
          description: copy.continueOnPhoneBrowserAuthDesc,
          retry: false,
          title: copy.continueOnPhoneBrowserAuthTitle
        }

      case 'handoff-failed':
        return {
          description: copy.continueOnPhoneTicketFailedDesc,
          retry: true,
          title: copy.continueOnPhoneTicketFailedTitle
        }
    }
  }

  const error = state.phase === 'error' ? errorCopy(state.reason) : null

  return (
    <Dialog onOpenChange={handleOpenChange} open={open}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>{copy.continueOnPhoneTitle}</DialogTitle>
          <DialogDescription>{copy.continueOnPhoneDesc}</DialogDescription>
        </DialogHeader>

        {state.phase === 'loading' && (
          <div className="grid min-h-72 place-items-center gap-3 py-8 text-center">
            <Loader className="size-12" label={copy.continueOnPhonePreparing} type="lemniscate-bloom" />
            <p className="text-sm text-(--ui-text-secondary)">{copy.continueOnPhonePreparing}</p>
          </div>
        )}

        {error && (
          <ErrorState className="py-8" description={error.description} title={error.title}>
            {error.retry && (
              <Button onClick={createNewCode} type="button" variant="secondary">
                {t.common.retry}
              </Button>
            )}
          </ErrorState>
        )}

        {state.phase === 'ready' && isExpired && (
          <ErrorState
            className="py-8"
            description={copy.continueOnPhoneExpiredDesc}
            title={copy.continueOnPhoneExpiredTitle}
          >
            <Button onClick={createNewCode} type="button">
              {copy.continueOnPhoneNewCode}
            </Button>
          </ErrorState>
        )}

        {state.phase === 'ready' && !isExpired && (
          <>
            <div className="grid justify-items-center gap-4 py-3">
              <img
                alt={copy.continueOnPhoneQrAlt}
                className="size-60 max-w-full"
                height={240}
                src={state.qrDataUrl}
                width={240}
              />
              <p className="text-center text-sm text-(--ui-text-secondary)">
                {copy.continueOnPhoneExpiresIn(remainingSeconds)}
              </p>
              <p className="text-center text-xs text-(--ui-text-tertiary)">
                {copy.continueOnPhoneAvailabilityHint}
              </p>
              <p className="max-w-full truncate text-xs text-(--ui-text-tertiary)">{state.url}</p>
            </div>
            <DialogFooter>
              <CopyButton
                buttonSize="sm"
                buttonVariant="secondary"
                label={copy.continueOnPhoneCopyLink}
                text={state.url}
              />
              <Button
                onClick={() => {
                  void window.hermesDesktop.openExternal(state.url).catch(error =>
                    notifyError(error, copy.continueOnPhoneOpenFailed)
                  )
                }}
                size="sm"
                type="button"
              >
                {copy.continueOnPhoneOpenBrowser}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  )
}

export function ContinueOnPhoneDialogHost() {
  const target = useStore($continueOnPhoneTarget)

  return (
    <ContinueOnPhoneDialog
      onOpenChange={open => {
        if (!open) {
          closeContinueOnPhone()
        }
      }}
      open={target !== null}
      profile={target?.profile}
      sessionId={target?.sessionId ?? ''}
    />
  )
}
