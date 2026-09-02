import type { CSSProperties } from 'react'
import { useCallback, useEffect, useState } from 'react'

import { TITLEBAR_HEIGHT } from '@/app/shell/titlebar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Loader } from '@/components/ui/loader'
import { useI18n } from '@/i18n'

type BotDesktopProvider = 'docker' | 'wsl'
type BotDesktopInfo = Awaited<ReturnType<Window['hermesDesktop']['botDesktop']['info']>>

interface BotDesktopSurfaceProps {
  profile: string
}

function providerLabel(provider: BotDesktopProvider, copy: ReturnType<typeof useI18n>['t']['desktop']['botDesktop']) {
  return provider === 'docker' ? copy.docker : copy.wsl
}

function safeProfile(raw: string | null | undefined) {
  return String(raw || '').trim() || 'default'
}

export function BotDesktopSurface({ profile }: BotDesktopSurfaceProps) {
  const { t } = useI18n()
  const copy = t.desktop.botDesktop
  const [provider, setProvider] = useState<BotDesktopProvider>('wsl')
  const [info, setInfo] = useState<BotDesktopInfo | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const key = safeProfile(profile)

  const start = useCallback(
    async (nextProvider: BotDesktopProvider) => {
      setBusy(true)
      setError(null)
      setProvider(nextProvider)

      try {
        const result = await window.hermesDesktop.botDesktop.start(key, nextProvider)
        const nextInfo = result.info || (await window.hermesDesktop.botDesktop.info(key, nextProvider))
        setInfo(nextInfo)

        if (!result.ok) {
          setError(result.error || nextInfo.error || copy.startFailed(copy.unavailable))
        }
      } catch (reason) {
        setError(copy.startFailed(reason instanceof Error ? reason.message : String(reason)))
      } finally {
        setBusy(false)
      }
    },
    [copy, key]
  )

  useEffect(() => {
    let active = true

    void window.hermesDesktop.botDesktop
      .info(key, 'wsl')
      .then(current => {
        if (!active) {
          return
        }

        setInfo(current)

        if (current.provider === 'docker' || current.provider === 'wsl') {
          setProvider(current.provider)
        }

        // Embedded workspaces are opened before a runtime exists. Start the
        // default provider once so the pane shows a real desktop, while a
        // failed/unsupported runtime remains a deliberate user-visible state.
        if (current.supported && !current.running && !current.error) {
          void window.hermesDesktop.botDesktop.start(key, 'wsl').then(result => {
            if (!active) {
              return
            }

            setInfo(result.info || null)

            if (!result.ok) {
              setError(result.error || result.info?.error || copy.startFailed(copy.unavailable))
            }
          })
        }
      })
      .catch(reason => {
        if (active) {
          setError(copy.startFailed(reason instanceof Error ? reason.message : String(reason)))
        }
      })

    return () => {
      active = false
    }
  }, [copy, key])

  const refresh = useCallback(async () => {
    try {
      const current = await window.hermesDesktop.botDesktop.info(key, provider)
      setInfo(current)
      setError(current.error || null)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    }
  }, [key, provider])

  const stop = useCallback(async () => {
    setBusy(true)
    setError(null)

    try {
      const result = await window.hermesDesktop.botDesktop.stop(key)
      setInfo(result.info || null)

      if (!result.ok) {
        setError(result.error || copy.unavailable)
      }
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : String(reason))
    } finally {
      setBusy(false)
    }
  }, [copy.unavailable, key])

  const revealWorkspace = useCallback(async () => {
    const result = await window.hermesDesktop.botDesktop.revealWorkspace(key)

    if (!result.ok) {
      setError(result.error || copy.unavailable)
    }
  }, [copy.unavailable, key])

  const running = Boolean(info?.running && info.viewerUrl)
  const statusLabel = info?.supported === false ? copy.unavailable : running ? copy.running : copy.notRunning
  const statusVariant = info?.supported === false ? 'warn' : running ? 'default' : 'muted'

  return (
    <section className="flex min-h-0 flex-1 flex-col bg-(--ui-bg-chrome) text-(--ui-text-primary)">
      <header className="flex shrink-0 flex-wrap items-center gap-2 border-b border-(--ui-stroke-secondary) px-3 py-2">
        <div className="mr-auto min-w-0">
          <div className="flex items-center gap-2">
            <Codicon className="text-primary" name="vm" />
            <h1 className="truncate text-sm font-semibold">{copy.title}</h1>
            <Badge size="xs" variant="outline">
              {copy.profile(key)}
            </Badge>
            <Badge size="xs" variant={statusVariant}>
              {statusLabel}
            </Badge>
          </div>
          <p className="mt-1 max-w-3xl text-[0.6875rem] text-(--ui-text-tertiary)">{copy.viewerHint}</p>
        </div>

        <div className="flex items-center gap-1.5">
          <span className="text-[0.6875rem] text-(--ui-text-tertiary)">{copy.runtime}</span>
          {(['wsl', 'docker'] as const).map(option => (
            <Button
              aria-pressed={provider === option}
              key={option}
              onClick={() => void start(option)}
              size="xs"
              variant={provider === option ? 'secondary' : 'ghost'}
            >
              {providerLabel(option, copy)}
            </Button>
          ))}
          <Button disabled={busy} onClick={() => void refresh()} size="icon-xs" variant="ghost">
            <Codicon name="refresh" />
            <span className="sr-only">{copy.refresh}</span>
          </Button>
          <Button disabled={busy} onClick={() => void revealWorkspace()} size="sm" variant="outline">
            <Codicon name="folder-opened" />
            {copy.revealWorkspace}
          </Button>
          {running ? (
            <Button disabled={busy} onClick={() => void stop()} size="sm" variant="destructive">
              <Codicon name="debug-stop" />
              {copy.stop}
            </Button>
          ) : null}
        </div>
      </header>

      <div className="relative min-h-0 flex-1 overflow-hidden bg-black/20">
        {busy ? (
          <div className="absolute inset-0 z-10 grid place-items-center bg-(--ui-bg-chrome)/75 backdrop-blur-sm">
            <Loader label={copy.starting} />
          </div>
        ) : null}

        {running ? (
          <iframe
            className="size-full border-0 bg-black"
            key={info?.viewerUrl}
            sandbox="allow-forms allow-pointer-lock allow-same-origin allow-scripts"
            src={info?.viewerUrl}
            title={copy.viewerTitle(key)}
          />
        ) : (
          <div className="grid h-full place-items-center p-6 text-center">
            <div className="max-w-lg space-y-3">
              <Codicon
                className="text-(--ui-text-quaternary)"
                name={info?.supported === false ? 'warning' : 'vm'}
                size="2rem"
              />
              <p className="text-sm font-medium">{error || info?.error || copy.notRunning}</p>
              <p className="text-xs leading-relaxed text-(--ui-text-tertiary)">{copy.viewerHint}</p>
              <Button disabled={busy} onClick={() => void start(provider)} size="sm">
                <Codicon name="play" />
                {copy.start}
              </Button>
            </div>
          </div>
        )}
      </div>
    </section>
  )
}

/** Specialized renderer loaded by `?win=bot-desktop`. */
export function BotDesktopRoot() {
  const { t } = useI18n()
  const params = new URLSearchParams(window.location.search)
  const profile = safeProfile(params.get('profile'))

  return (
    <div
      className="flex h-screen min-h-0 w-screen flex-col bg-(--ui-bg-chrome)"
      data-contrib-shell=""
      style={{ '--titlebar-height': `${TITLEBAR_HEIGHT}px` } as CSSProperties}
    >
      <div aria-hidden="true" className="relative shrink-0 bg-(--ui-bg-chrome)" style={{ height: TITLEBAR_HEIGHT }}>
        <div className="pointer-events-none absolute inset-0 [-webkit-app-region:drag]" />
      </div>
      <BotDesktopSurface profile={profile} />
      <span className="sr-only">{t.desktop.botDesktop.title}</span>
    </div>
  )
}
