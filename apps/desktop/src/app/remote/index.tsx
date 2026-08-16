import { useStore } from '@nanostores/react'
import { useEffect, useState } from 'react'

import { Button } from '@/components/ui/button'
import { ErrorBanner } from '@/components/ui/error-state'
import { Field, FieldHint } from '@/components/ui/field'
import { GlyphSpinner } from '@/components/ui/glyph-spinner'
import { Input } from '@/components/ui/input'
import { LogView } from '@/components/ui/log-view'
import { useI18n } from '@/i18n'
import {
  $remoteAttach,
  attachToSession,
  detachSession,
  disconnect,
  pairWithCode,
  refreshSessions,
  type RemoteSession,
  type RemoteSessionEvent,
  sendRemoteChat
} from '@/store/remote-session'

import { Panel, PanelHeader, PanelPill } from '../overlays/panel'

const DEFAULT_REMOTE_PORT = 8642
const VISIBLE_EVENT_LIMIT = 100

interface RemoteViewProps {
  onClose: () => void
}

function formattedDate(value: number | string | undefined): string {
  if (value === undefined || value === '') {
    return '—'
  }

  const date = typeof value === 'number' ? new Date(value * 1_000) : new Date(value)

  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString()
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null
}

function displayValue(value: unknown): string {
  if (typeof value === 'string') {
    return value
  }

  if (value === undefined || value === null) {
    return ''
  }

  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

function eventText(event: RemoteSessionEvent, copy: ReturnType<typeof useI18n>['t']['remoteAttach']): string {
  if (event.event === 'session.message') {
    const message = asRecord(event.message)
    const role = typeof message?.role === 'string' ? message.role : copy.message
    const content = displayValue(message?.content ?? event.content)

    return `${role}: ${content || copy.emptyMessage}`
  }

  if (event.event === 'session.status') {
    return copy.statusChanged(typeof event.status === 'string' ? event.status : copy.unknown)
  }

  const toolCall = asRecord(event.tool_call)
  const name = typeof toolCall?.name === 'string' ? toolCall.name : copy.unknownTool
  const phase = typeof toolCall?.phase === 'string' ? toolCall.phase : copy.unknown

  return `${name} · ${phase}`
}

function shortSessionId(id: string): string {
  return id.length > 16 ? `${id.slice(0, 16)}…` : id
}

function PairingForm({ busy, error }: { busy: boolean; error?: string }) {
  const { t } = useI18n()
  const c = t.remoteAttach
  const [host, setHost] = useState('')
  const [port, setPort] = useState(String(DEFAULT_REMOTE_PORT))
  const [code, setCode] = useState('')
  const numericPort = Number(port)
  const valid = host.trim().length > 0 && Number.isInteger(numericPort) && numericPort > 0 && numericPort <= 65_535
  const canSubmit = valid && code.length === 6 && !busy

  const submit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()

    if (canSubmit) {
      void pairWithCode(host, numericPort, code)
    }
  }

  return (
    <div className="grid min-h-0 flex-1 place-items-center overflow-y-auto px-4 py-8">
      <form className="grid w-full max-w-md gap-4" onSubmit={submit}>
        <div>
          <h3 className="text-sm font-semibold text-foreground">{c.connectTitle}</h3>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">{c.connectDescription}</p>
        </div>

        {error ? (
          <ErrorBanner>
            <span role="alert">{error}</span>
          </ErrorBanner>
        ) : null}

        <div className="grid items-start gap-4 sm:grid-cols-[minmax(0,1fr)_7rem]">
          <Field htmlFor="remote-host" label={c.hostLabel}>
            <Input
              disabled={busy}
              id="remote-host"
              onChange={event => setHost(event.target.value)}
              placeholder={c.hostPlaceholder}
              value={host}
            />
          </Field>
          <Field htmlFor="remote-port" label={c.portLabel}>
            <Input
              disabled={busy}
              id="remote-port"
              inputMode="numeric"
              max={65_535}
              min={1}
              onChange={event => setPort(event.target.value)}
              type="number"
              value={port}
            />
          </Field>
        </div>

        <Field htmlFor="remote-pairing-code" label={c.codeLabel}>
          <Input
            className="font-mono uppercase tracking-[0.2em]"
            disabled={busy}
            id="remote-pairing-code"
            maxLength={6}
            onChange={event =>
              setCode(
                event.target.value
                  .toUpperCase()
                  .replace(/[^A-Z0-9]/g, '')
                  .slice(0, 6)
              )
            }
            placeholder={c.codePlaceholder}
            value={code}
          />
          <FieldHint>{c.codeHint}</FieldHint>
        </Field>

        <div className="flex justify-end">
          <Button disabled={!canSubmit} type="submit">
            {busy ? <GlyphSpinner ariaLabel={t.common.connecting} /> : null}
            {busy ? t.common.connecting : t.common.connect}
          </Button>
        </div>
      </form>
    </div>
  )
}

function SessionRow({ attached, busy, session }: { attached: boolean; busy: boolean; session: RemoteSession }) {
  const { t } = useI18n()
  const c = t.remoteAttach
  const title = session.title?.trim() || c.untitled
  const active = session.status === 'active'

  return (
    <div className="row-hover flex min-w-0 items-center gap-3 rounded-md px-2 py-2 text-xs">
      <div className="min-w-0 flex-1">
        <div className="flex min-w-0 items-center gap-2">
          <span className="truncate font-medium text-foreground">{title}</span>
          <PanelPill tone={active ? 'good' : 'muted'}>{active ? c.active : c.idle}</PanelPill>
        </div>
        <div className="mt-1 flex min-w-0 flex-wrap items-center gap-x-3 gap-y-1 text-[0.65rem] text-(--ui-text-tertiary)">
          <span className="font-mono">{shortSessionId(session.id)}</span>
          <time dateTime={session.updated_at}>{formattedDate(session.updated_at)}</time>
        </div>
      </div>
      <Button
        aria-label={attached ? c.detachFrom(title) : c.attachTo(title)}
        disabled={busy && !attached}
        onClick={() => (attached ? detachSession() : void attachToSession(session.id))}
        size="xs"
        variant={attached ? 'secondary' : 'outline'}
      >
        {attached ? c.detach : c.attach}
      </Button>
    </div>
  )
}

function AttachedSession({ busy, session }: { busy: boolean; session: RemoteSession }) {
  const { t } = useI18n()
  const c = t.remoteAttach
  const [message, setMessage] = useState('')
  const [sending, setSending] = useState(false)
  const events = session.events.slice(-VISIBLE_EVENT_LIMIT)

  const submit = async () => {
    const text = message.trim()

    if (!text || busy || sending) {
      return
    }

    setSending(true)
    setMessage('')
    await sendRemoteChat(text)
    setSending(false)
  }

  return (
    <section className="flex min-h-0 flex-1 flex-col gap-2 border-t border-(--ui-stroke-tertiary) pt-3">
      <div className="flex items-baseline justify-between gap-3">
        <h3 className="truncate text-xs font-semibold text-foreground">{session.title?.trim() || c.untitled}</h3>
        <span className="shrink-0 font-mono text-[0.62rem] text-(--ui-text-tertiary)">
          {shortSessionId(session.id)}
        </span>
      </div>

      <LogView aria-label={c.eventLogLabel} className="min-h-24 flex-1">
        {events.length ? (
          events.map((event, index) => (
            <div className="grid grid-cols-[auto_minmax(0,1fr)] gap-x-2" key={`${String(event.timestamp)}:${index}`}>
              <time className="text-(--ui-text-tertiary)" dateTime={String(event.timestamp ?? '')}>
                {formattedDate(event.timestamp)}
              </time>
              <span className="text-foreground/80">{eventText(event, c)}</span>
            </div>
          ))
        ) : (
          <span>{c.noEvents}</span>
        )}
      </LogView>

      <form
        className="flex items-center gap-2"
        onSubmit={event => {
          event.preventDefault()
          void submit()
        }}
      >
        <Input
          aria-label={c.messageAria}
          className="min-w-0 flex-1"
          disabled={busy || sending}
          onChange={event => setMessage(event.target.value)}
          onKeyDown={event => {
            if (event.key === 'Enter' && !event.nativeEvent.isComposing) {
              event.preventDefault()
              void submit()
            }
          }}
          placeholder={c.messagePlaceholder}
          value={message}
        />
        <Button disabled={!message.trim() || busy || sending} type="submit">
          {sending ? <GlyphSpinner ariaLabel={c.sending} /> : null}
          {sending ? c.sending : t.common.send}
        </Button>
      </form>
    </section>
  )
}

function ConnectedView() {
  const state = useStore($remoteAttach)
  const { t } = useI18n()
  const c = t.remoteAttach
  const busy = state.status === 'connecting'
  const reconnecting = busy && state.reconnecting === true
  const attached = state.sessions.find(session => session.id === state.attachedSessionId)

  useEffect(() => {
    void refreshSessions()
  }, [])

  return (
    <div className="flex min-h-0 flex-1 flex-col gap-3">
      <div className="flex flex-wrap items-center justify-between gap-3 text-xs">
        <div className="min-w-0">
          <div className="truncate font-mono text-foreground">
            {state.host}:{state.port}
          </div>
          {reconnecting ? (
            <div className="flex items-center gap-1 text-(--ui-text-tertiary)" role="status">
              <GlyphSpinner ariaLabel={c.reconnecting} />
              <span>{c.reconnecting}</span>
            </div>
          ) : (
            <div className="text-(--ui-text-tertiary)">{c.pairedUntil(formattedDate(state.expiresAt))}</div>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button disabled={busy} onClick={() => void refreshSessions()} size="sm" variant="outline">
            {busy && !reconnecting ? <GlyphSpinner ariaLabel={c.refreshing} /> : null}
            {t.common.refresh}
          </Button>
          <Button disabled={busy && !reconnecting} onClick={disconnect} size="sm" variant="secondary">
            {c.disconnect}
          </Button>
        </div>
      </div>

      {state.error ? (
        <ErrorBanner>
          <span role="alert">{state.error}</span>
        </ErrorBanner>
      ) : null}

      <section className={attached ? 'max-h-[42%] min-h-32 overflow-y-auto' : 'min-h-0 flex-1 overflow-y-auto'}>
        <h3 className="mb-1 text-[0.6rem] font-medium uppercase tracking-wider text-muted-foreground/50">
          {c.sessions}
        </h3>
        {state.sessions.length ? (
          <div className="space-y-1">
            {state.sessions.map(session => (
              <SessionRow
                attached={session.id === state.attachedSessionId}
                busy={busy}
                key={session.id}
                session={session}
              />
            ))}
          </div>
        ) : (
          <p className="py-8 text-center text-xs text-muted-foreground">{c.noSessions}</p>
        )}
      </section>

      {attached ? <AttachedSession busy={busy} session={attached} /> : null}
    </div>
  )
}

export function RemoteView({ onClose }: RemoteViewProps) {
  const state = useStore($remoteAttach)
  const { t } = useI18n()
  const c = t.remoteAttach
  const needsPairing = !state.token
  const busy = state.status === 'connecting'

  return (
    <Panel closeLabel={c.close} onClose={onClose}>
      <PanelHeader subtitle={c.subtitle} title={c.title} />
      {needsPairing ? <PairingForm busy={busy} error={state.error} /> : <ConnectedView />}
    </Panel>
  )
}
