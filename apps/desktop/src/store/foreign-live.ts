/**
 * FOREIGN-LIVE — DB-derived liveness for sessions this renderer has no
 * runtime for (cli one-shots, cron runs, other profiles, TUI sessions).
 *
 * The backend already stamps every list row with `is_active`
 * (ended_at IS NULL AND now - last_active < 300, web_routers/sessions.py).
 * Claim a stored id ONLY when the row says active AND no runtime exists in
 * $sessionStates (event-owned sessions are the authoritative path and must
 * win). Refreshes ride the existing sessions.changed list refresh; a real
 * gateway event for the session removes it from this set by construction.
 */
import { computed } from 'nanostores'

import { $sessions, lineageAliases } from './session'
import { $sessionStates } from './session-states'

// Janela de atividade REAL: ~1.5x a cadência do heartbeat de 60s
// (agent/session_activity.py:29). `is_active` (janela de 300s) sozinho pinta
// linhas reabertas/órfãs como vivas por até 5 min; um toque RECENTE de
// last_activity_at prova que um agente está de fato escrevendo agora.
// Tool calls longas mantêm o toque via descrições "terminal command running
// (Ns elapsed)" e streams de token.
export const FOREIGN_LIVE_ACTIVITY_MS = 90_000

export const $foreignLiveSessionIds = computed([$sessions, $sessionStates], (sessions, states) => {
  const owned = new Set<string>()

  for (const state of Object.values(states)) {
    if (state?.storedSessionId) {
      owned.add(state.storedSessionId)
    }
  }

  const foreign = new Set<string>()

  for (const session of sessions) {
    if (!session.is_active || owned.has(session.id)) {
      continue
    }

    const stamp = Number(session.last_activity_at ?? 0) * 1000
    if (!Number.isFinite(stamp) || stamp <= 0 || Date.now() - stamp >= FOREIGN_LIVE_ACTIVITY_MS) {
      continue
    }

    for (const alias of lineageAliases(session.id, sessions)) {
      foreign.add(alias)
    }
  }

  return [...foreign]
})
