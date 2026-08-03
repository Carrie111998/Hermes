import { LONG_MSG } from '../config/limits.js'
import { buildToolTrailLine } from '../lib/text.js'
import type { Msg, SessionInfo } from '../types.js'

export const introMsg = (info: SessionInfo): Msg => ({ info, kind: 'intro', role: 'system', text: '' })

export const userDisplay = (text: string) => {
  if (text.length <= LONG_MSG) {
    return text
  }

  const first = text.split('\n')[0]?.trim() ?? ''
  const words = first.split(/\s+/).filter(Boolean)
  const prefix = (words.length > 1 ? words.slice(0, 4).join(' ') : first).slice(0, 80)

  return `${prefix || '(message)'} [long message]`
}

export const toTranscriptMessages = (rows: unknown): Msg[] => {
  if (!Array.isArray(rows)) {
    return []
  }

  const out: Msg[] = []
  let pending: string[] = []

  for (const row of rows) {
    if (!row || typeof row !== 'object') {
      continue
    }

    const {
      context,
      db_id: dbId,
      db_session_id: dbSessionId,
      display_kind,
      name,
      role,
      text
    } = row as TranscriptRow

    const dbMeta = {
      ...(typeof dbId === 'number' && { dbId }),
      ...(typeof dbSessionId === 'string' && dbSessionId ? { dbSessionId } : {})
    }

    if (role === 'tool') {
      const trailLine = buildToolTrailLine(name ?? 'tool', context ?? '')
      pending.push(trailLine)
      out.push({
        role,
        text: typeof text === 'string' && text.trim() ? text : trailLine,
        ...dbMeta
      })

      continue
    }

    if (typeof text !== 'string' || !text.trim()) {
      continue
    }

    // Display-only timeline events: render as dim ◈ markers instead of
    // opaque user messages. Hidden compaction handoffs are skipped entirely.
    if (display_kind === 'hidden') {
      continue
    }

    if (display_kind === 'model_switch') {
      out.push({ kind: 'event', role: 'system', text: 'model changed' })
      pending = []

      continue
    }

    if (display_kind === 'auto_continue') {
      out.push({ kind: 'event', role: 'system', text: 'resumed interrupted turn' })
      pending = []

      continue
    }

    if (display_kind === 'async_delegation_complete') {
      const meta = (row as TranscriptRow).display_metadata
      const count = meta && typeof meta.task_count === 'number' ? meta.task_count : undefined

      const label =
        count === undefined
          ? 'background agent work finished'
          : `${count} background agent${count === 1 ? '' : 's'} finished`

      out.push({ kind: 'event', role: 'system', text: label })
      pending = []

      continue
    }

    if (role === 'assistant') {
      out.push({ role, text, ...dbMeta, ...(pending.length && { tools: pending }) })
      pending = []
    } else if (role === 'user' || role === 'system') {
      out.push({ role, text, ...dbMeta })
      pending = []
    }
  }

  return out
}

export const fmtDuration = (ms: number) => {
  const t = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(t / 3600)
  const m = Math.floor((t % 3600) / 60)
  const s = t % 60

  return h > 0 ? `${h}h ${m}m` : m > 0 ? `${m}m ${s}s` : `${s}s`
}

interface TranscriptRow {
  context?: string
  display_kind?: string
  display_metadata?: Record<string, unknown>
  db_id?: number
  db_session_id?: string
  name?: string
  role?: string
  text?: string
}
