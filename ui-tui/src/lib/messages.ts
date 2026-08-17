import { MAX_HISTORY } from '../config/limits.js'
import type { Msg, Role } from '../types.js'

import { appendToolShelfMessage } from './liveProgress.js'

// Stamp live rows AT APPEND (wall clock, Unix seconds) rather than later:
// a message's authoring time is when it entered the transcript, not when it
// happened to be persisted or re-rendered (#82840-class rule). Rehydrated
// rows arrive with their persisted `createdAt` and keep it.
export const appendTranscriptMessage = (prev: Msg[], msg: Msg): Msg[] => {
  const stamped = msg.createdAt === undefined ? { ...msg, createdAt: Date.now() / 1000 } : msg
  const last = prev.at(-1)

  // A snapshot/live-tail race can append the same user or assistant row
  // back-to-back (pairwise duplication, #88362): the persisted snapshot and
  // the live tail both carry the same row. Skip an adjacent duplicate —
  // same role and same text — so the transcript renders exactly one copy.
  // Tool rows are deliberately untouched: their merge semantics live in
  // appendToolShelfMessage and repeated tool output is legitimate.
  if (
    last &&
    stamped.role === last.role &&
    stamped.text === last.text &&
    (stamped.role === 'user' || stamped.role === 'assistant')
  ) {
    return prev
  }

  return appendToolShelfMessage(prev, stamped)
}

export const capTranscriptHistory = (items: Msg[]): Msg[] => {
  if (items.length <= MAX_HISTORY) {
    return items
  }

  return items[0]?.kind === 'intro' ? [items[0], ...items.slice(-(MAX_HISTORY - 1))] : items.slice(-MAX_HISTORY)
}

export const upsert = (prev: Msg[], role: Role, text: string): Msg[] =>
  prev.at(-1)?.role === role ? [...prev.slice(0, -1), { role, text }] : [...prev, { role, text }]
