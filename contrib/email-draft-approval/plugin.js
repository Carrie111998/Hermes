/**
 * Email Draft Approval — desktop runtime plugin.
 *
 * Loads through the runtime pipeline (apps/desktop/src/contrib/runtime-loader.ts):
 * plain ESM js with jsx() calls, `@hermes/plugin-sdk` imports rewritten to live
 * shims at load time. Registers a "Email Drafts" pane that lists pending
 * outbound-email drafts and approves or denies them through gateway RPC.
 *
 * Requires the backend P1 draft-approval stack (gateway/outbound_drafts.py +
 * tui_gateway/methods_email_drafts.py) and platforms.email.extra.draft_only.
 * When the backend replies with auth errors (4403) or the RPC is missing, the
 * pane shows a friendly message instead of crashing — the loader guarantees a
 * broken plugin can never take the app down.
 */

import { host } from '@hermes/plugin-sdk'
import { useEffect, useState } from 'react'
import { jsx, jsxs } from 'react/jsx-runtime'

const LIST_METHOD = 'email.drafts.list'
const APPROVE_METHOD = 'email.drafts.approve'
const DENY_METHOD = 'email.drafts.deny'

function toEntries(payload) {
  const drafts = payload && Array.isArray(payload.drafts) ? payload.drafts : []
  return drafts.map((d) => ({
    draft_id: d.draft_id || '',
    subject: d.subject || '(no subject)',
    recipient: d.recipient || '',
    state: d.state || 'unknown',
    created_at: d.created_at || '',
  }))
}

function DraftPane() {
  const [entries, setEntries] = useState([])
  const [busyId, setBusyId] = useState(null)
  const [notice, setNotice] = useState('')

  const refresh = async () => {
    try {
      const resp = await host.request(LIST_METHOD, {})
      setEntries(toEntries(resp))
      setNotice('')
    } catch (err) {
      setEntries([])
      setNotice(err && err.message ? err.message : String(err))
    }
  }

  useEffect(() => {
    void refresh()
  }, [])

  const act = async (draftId, method) => {
    setBusyId(draftId)
    try {
      await host.request(method, { draft_id: draftId })
      setNotice('')
      await refresh()
    } catch (err) {
      setNotice(err && err.message ? err.message : String(err))
    } finally {
      setBusyId(null)
    }
  }

  if (notice && entries.length === 0) {
    return jsx('div', {
      className: 'px-3 py-3 text-[0.75rem] text-(--ui-text-secondary)',
      children: notice,
    })
  }

  if (entries.length === 0) {
    return jsx('div', {
      className: 'px-3 py-3 text-[0.75rem] text-(--ui-text-tertiary)',
      children: 'No pending email drafts.',
    })
  }

  return jsxs('div', {
    className: 'flex h-full flex-col gap-2 overflow-y-auto p-3',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between',
        children: [
          jsx('span', {
            className: 'text-[0.6875rem] uppercase tracking-wide text-(--ui-text-tertiary)',
            children: `Email Drafts · ${entries.length}`,
          }),
          jsx('button', {
            onClick: () => void refresh(),
            className: 'rounded px-2 py-1 text-[0.75rem] text-(--ui-accent) hover:bg-(--ui-hover)',
            children: 'Refresh',
          }),
        ],
      }),
      ...entries.map((e) =>
        jsxs('div', {
          key: e.draft_id,
          className: 'rounded border border-(--ui-border) p-2',
          children: [
            jsx('div', {
              className: 'truncate text-[0.8125rem] font-medium',
              children: e.subject,
            }),
            jsx('div', {
              className: 'truncate text-[0.6875rem] text-(--ui-text-secondary)',
              children: `To: ${e.recipient} · ${e.state}`,
            }),
            jsxs('div', {
              className: 'mt-2 flex items-center gap-2',
              children: [
                jsx('button', {
                  disabled: busyId === e.draft_id,
                  onClick: () => void act(e.draft_id, APPROVE_METHOD),
                  className: 'rounded bg-(--ui-accent) px-2 py-1 text-[0.75rem] text-white disabled:opacity-50',
                  children: busyId === e.draft_id ? 'Working…' : 'Approve',
                }),
                jsx('button', {
                  disabled: busyId === e.draft_id,
                  onClick: () => void act(e.draft_id, DENY_METHOD),
                  className: 'rounded border border-(--ui-border) px-2 py-1 text-[0.75rem] text-(--ui-text-secondary) disabled:opacity-50',
                  children: 'Deny',
                }),
              ],
            }),
          ],
        })
      ),
      notice
        ? jsx('div', {
            className: 'text-[0.75rem] text-(--ui-danger)',
            children: notice,
          })
        : null,
    ],
  })
}

export default {
  id: 'email-draft-approval',
  name: 'Email Draft Approval',
  description: 'Review and approve pending outbound-email drafts (draft_only mode).',
  register(ctx) {
    ctx.register({
      id: 'pane',
      area: 'panes',
      title: 'Email Drafts',
      data: {
        placement: 'right',
        width: '280px',
        collapsible: true,
      },
      render: () => jsx(DraftPane, {}),
    })
  },
}
