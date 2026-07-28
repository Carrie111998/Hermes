import { useCallback, useEffect, useRef, useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { cn } from '@/lib/utils'

const API = 'http://127.0.0.1:19876/api/email'

interface Email {
  id: string
  subject: string
  from: string
  date: string
  snippet: string
}

interface EmailDetail {
  id: string
  subject: string
  from: string
  to: string
  date: string
  body: string
}

export function EmailView() {
  const [emails, setEmails] = useState<Email[]>([])
  const [loading, setLoading] = useState(true)
  const [detail, setDetail] = useState<EmailDetail | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [view, setView] = useState<'inbox' | 'detail' | 'compose'>('inbox')
  const [compose, setCompose] = useState({ to: '', subject: '', body: '' })
  const [sending, setSending] = useState(false)
  const [error, setError] = useState('')
  const inboxRef = useRef<HTMLDivElement>(null)

  const loadInbox = useCallback(async () => {
    setLoading(true)
    try {
      const r = await fetch(`${API}/inbox`)
      const data = await r.json()
      setEmails(Array.isArray(data) ? data : [])
    } catch { setEmails([]) }
    setLoading(false)
  }, [])

  useEffect(() => { loadInbox() }, [loadInbox])

  const openEmail = useCallback(async (id: string) => {
    setDetailLoading(true)
    setView('detail')
    try {
      const r = await fetch(`${API}/read/${id}`)
      setDetail(await r.json())
    } catch { setDetail(null) }
    setDetailLoading(false)
  }, [])

  const reply = useCallback(() => {
    if (!detail) return
    setCompose({
      to: detail.from.replace(/^.*<(.+)>$/, '$1').trim(),
      subject: `Re: ${detail.subject}`,
      body: `\n\n-------- Original message --------\nFrom: ${detail.from}\nDate: ${detail.date}\nSubject: ${detail.subject}\n\n${detail.body}`,
    })
    setView('compose')
  }, [detail])

  const send = useCallback(async () => {
    if (!compose.to || !compose.subject) return
    setSending(true)
    setError('')
    try {
      const r = await fetch(`${API}/send`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(compose),
      })
      const res = await r.json()
      if (res.success) {
        setView('inbox')
        setCompose({ to: '', subject: '', body: '' })
        loadInbox()
      } else {
        setError(res.error || 'Send failed')
      }
    } catch { setError('Network error') }
    setSending(false)
  }, [compose, loadInbox])

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header */}
      <div className="flex items-center justify-between border-b px-4 py-2">
        {view === 'inbox' && (
          <>
            <h2 className="text-sm font-semibold">Inbox</h2>
            <div className="flex items-center gap-1">
              <Button variant="ghost" size="icon" onClick={() => setView('compose')} title="New email">
                <Codicon name="new-file" />
              </Button>
              <Button variant="ghost" size="icon" onClick={loadInbox} disabled={loading}>
                <Codicon name={loading ? 'sync~spin' : 'refresh'} />
              </Button>
            </div>
          </>
        )}
        {view === 'detail' && (
          <>
            <button className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground" onClick={() => setView('inbox')}>
              <Codicon name="arrow-left" /> Back
            </button>
            <Button variant="ghost" size="icon" onClick={reply} title="Reply">
              <Codicon name="reply" />
            </Button>
          </>
        )}
        {view === 'compose' && (
          <>
            <button className="flex items-center gap-1 text-xs text-muted-foreground hover:text-foreground" onClick={() => setView(detail ? 'detail' : 'inbox')}>
              <Codicon name="arrow-left" /> Back
            </button>
            <span className="text-xs text-muted-foreground">{sending ? 'Sending...' : 'New Email'}</span>
          </>
        )}
      </div>

      {/* Inbox list */}
      {view === 'inbox' && (
        <div ref={inboxRef} className="flex-1 overflow-y-auto">
          {loading && emails.length === 0 && (
            <div className="flex items-center justify-center py-8 text-xs text-muted-foreground">Loading...</div>
          )}
          {!loading && emails.length === 0 && (
            <div className="flex items-center justify-center py-8 text-xs text-muted-foreground">No emails</div>
          )}
          {emails.map(m => (
            <button
              key={m.id}
              onClick={() => openEmail(m.id)}
              className="w-full border-b px-4 py-2.5 text-left transition-colors hover:bg-accent/50"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-xs font-medium">{m.from.replace(/<[^>]+>/, '').trim() || m.from}</span>
                <span className="shrink-0 text-[10px] text-muted-foreground">
                  {m.date ? m.date.slice(0, 16) : ''}
                </span>
              </div>
              <div className="mt-0.5 truncate text-[11px] text-foreground/80">{m.subject || '(no subject)'}</div>
              <div className="mt-0.5 truncate text-[10px] text-muted-foreground">{m.snippet}</div>
            </button>
          ))}
        </div>
      )}

      {/* Detail view */}
      {view === 'detail' && (
        <div className="flex-1 overflow-y-auto p-4">
          {detailLoading ? (
            <div className="py-8 text-center text-xs text-muted-foreground">Loading...</div>
          ) : detail ? (
            <>
              <h3 className="pb-1 text-sm font-semibold">{detail.subject || '(no subject)'}</h3>
              <div className="pb-3 text-[11px] text-muted-foreground">
                <div>From: {detail.from}</div>
                <div>To: {detail.to}</div>
                <div>Date: {detail.date}</div>
              </div>
              <div className="whitespace-pre-wrap text-[12px] leading-relaxed">{detail.body || '(no content)'}</div>
            </>
          ) : (
            <div className="py-8 text-center text-xs text-muted-foreground">Email not found</div>
          )}
        </div>
      )}

      {/* Compose view */}
      {view === 'compose' && (
        <div className="flex flex-1 flex-col gap-3 p-4">
          <input
            className="w-full border-b bg-transparent pb-1 text-xs outline-none placeholder:text-muted-foreground/50"
            placeholder="To"
            value={compose.to}
            onChange={e => setCompose(p => ({ ...p, to: e.target.value }))}
          />
          <input
            className="w-full border-b bg-transparent pb-1 text-xs outline-none placeholder:text-muted-foreground/50"
            placeholder="Subject"
            value={compose.subject}
            onChange={e => setCompose(p => ({ ...p, subject: e.target.value }))}
          />
          <textarea
            className="flex-1 resize-none bg-transparent text-xs outline-none placeholder:text-muted-foreground/50"
            placeholder="Write your message..."
            value={compose.body}
            onChange={e => setCompose(p => ({ ...p, body: e.target.value }))}
          />
          {error && <div className="text-xs text-destructive">{error}</div>}
          <div className="flex justify-end">
            <Button size="sm" onClick={send} disabled={sending || !compose.to || !compose.subject}>
              {sending ? 'Sending...' : 'Send'}
            </Button>
          </div>
        </div>
      )}
    </div>
  )
}
