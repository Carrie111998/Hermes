const ROLLOVER_PATTERN = /^\/(new|reset)\s*$/i

/** Return the Bot-rollover command name, or null for every ordinary command. */
export function botRolloverCommand(text) {
  const match = ROLLOVER_PATTERN.exec(String(text || '').trim())
  return match ? match[1].toLowerCase() : null
}

/** Resolve the exact selected Bot owner only while its canonical stored row
 * (root or compression tip) owns the focused transcript. */
export function focusedCanonicalBot({ roster, selectedKey, focusedStoredSessionId, botsMode = true, keyForBot }) {
  const key = String(selectedKey || '')
  const focused = String(focusedStoredSessionId || '')

  if (!botsMode || !key || !focused || !Array.isArray(roster) || typeof keyForBot !== 'function') {
    return null
  }

  const bot = roster.find(row => keyForBot(row) === key)
  const canonical = bot?.canonical_session || null
  const ids = [canonical?.id, canonical?.resolved_id].filter(Boolean).map(String)

  if (!bot || !canonical || !ids.includes(focused)) {
    return null
  }

  return {
    bot,
    canonical,
    expectedCurrentSessionId: focused
  }
}

/** Execute the routed RPC, then reconcile roster truth and open exactly the
 * returned stored row. A probe requiring confirmation has no UI side effects. */
export async function executeBotRollover({ target, force = false, request, profileForBot, refresh, open }) {
  if (!target?.bot || !target.expectedCurrentSessionId) {
    throw new Error('No focused canonical Bot Chat to roll over')
  }

  const profile = String(
    profileForBot?.(target.bot) || target.bot?.targetProfile || target.bot?.profile || target.bot?.name || ''
  ).trim()
  if (!profile) {
    throw new Error('Bot session rollover has no owning profile')
  }

  const result = await request(target.bot, 'session.bot_rollover', {
    expected_current_session_id: target.expectedCurrentSessionId,
    force: Boolean(force),
    profile
  })

  if (result?.confirmation_required) {
    return result
  }

  const currentId = String(result?.current_session_id || result?.current_session?.id || '')
  if (!currentId) {
    throw new Error('Bot session rollover returned no current session')
  }

  try {
    await refresh?.()
    await open(target.bot, currentId, result.current_session || { id: currentId, title: 'Bot Chat', message_count: 0 })
  } catch (cause) {
    const detail = cause instanceof Error && cause.message ? `: ${cause.message}` : ''
    const error = new Error(`The fresh Bot session was created but could not be opened${detail}`, { cause })
    error.rolloverCommitted = true
    error.currentSessionId = currentId
    throw error
  }
  return result
}
