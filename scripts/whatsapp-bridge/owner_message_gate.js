/**
 * Pure classifier for the WhatsApp bridge's owner-message dispatch loop.
 *
 * Centralises the "should this fromMe message be forwarded as fromOwner?"
 * decision so the gate can be unit-tested without spinning up Baileys or
 * the Express server.
 *
 * Lives next to `outbound_ids.js` rather than inline in `bridge.js`
 * because the previous implementation accidentally bypassed the
 * customer-side allowlist when forwarding owner-typed messages — see
 * the regression test in `owner_message_gate.test.mjs`.
 *
 * Caller responsibilities:
 *   - Pre-filter group / status JIDs in bot mode (that legacy gate doesn't
 *     know about them). The self-chat command exception validates direct
 *     customer JIDs itself.
 *   - On `drop_allowlist`, log the rejection so operators can audit
 *     accidental allowlist mismatches.
 *
 * Returned actions:
 *   - 'pass'           : non-fromMe, fall through to existing handling
 *   - 'drop_echo'      : fromMe and matches a recently-sent /send id
 *   - 'drop_disabled'  : fromMe but operator hasn't opted into forwarding
 *   - 'drop_allowlist' : fromMe and the *customer chatId* isn't on the
 *                        allowlist (owner-typed reply to a stranger)
 *   - 'forward_owner'  : fromMe, owner-typed, allowlisted — forward with
 *                        fromOwner: true
 */

export function classifyOwnerMessageGate({
  mode,
  fromMe,
  fromOwnerEnabled,
  recentlySent,
  allowlistMatches,
  messageId,
  chatId,
  isSelfChat,
  ownerCommands,
  messageContent,
}) {
  if (mode === 'self-chat') {
    return classifySelfChatOwnerCommand({
      fromMe,
      chatId,
      isSelfChat,
      messageId,
      recentlySent,
      ownerCommands,
      messageContent,
    });
  }
  if (!fromMe) {
    return { action: 'pass' };
  }
  if (recentlySent && recentlySent.has(messageId)) {
    return { action: 'drop_echo' };
  }
  if (!fromOwnerEnabled) {
    return { action: 'drop_disabled' };
  }
  // Allowlist gate: check the *customer* chatId, not the sender. The
  // sender is the owner's own number/LID and won't be on the allowlist
  // by construction. Without this check, any contact the owner happens
  // to reply to leaks into Hermes and triggers implicit handover in the
  // gateway-policy plugin.
  if (typeof allowlistMatches === 'function' && !allowlistMatches(chatId)) {
    return { action: 'drop_allowlist' };
  }
  return { action: 'forward_owner' };
}

export function parseOwnerCommands(value) {
  if (typeof value !== 'string' || !value.trim()) return [];
  try {
    const parsed = JSON.parse(value);
    if (!Array.isArray(parsed)) return [];
    return Array.from(new Set(
      parsed
        .filter((name) => typeof name === 'string')
        .map((name) => name.trim().toLowerCase())
        .filter((name) => /^[a-z0-9_-]+$/u.test(name)),
    ));
  } catch {
    return [];
  }
}

/**
 * Classify the narrow self-chat exception for owner slash commands sent in a
 * direct customer chat. Non-matches deliberately collapse to one action so
 * the caller preserves its existing self-chat mismatch rejection.
 */
export function classifySelfChatOwnerCommand({
  fromMe,
  chatId,
  isSelfChat,
  messageId,
  recentlySent,
  ownerCommands,
  messageContent,
}) {
  if (!fromMe || isSelfChat) return { action: 'drop' };
  if (typeof chatId !== 'string' || !/^[^@]+@(lid|s\.whatsapp\.net)$/u.test(chatId)) {
    return { action: 'drop' };
  }
  if (recentlySent?.has(messageId)) return { action: 'drop' };

  const text = typeof messageContent?.conversation === 'string'
    ? messageContent.conversation
    : messageContent?.extendedTextMessage?.text;
  if (typeof text !== 'string' || /[\r\n\u2028\u2029]/u.test(text)) {
    return { action: 'drop' };
  }

  const match = text.trim().match(/^\/([^\s/]+)(?:\s+(\S(?:.*\S)?))?$/u);
  if (!match) return { action: 'drop' };

  const command = match[1].toLowerCase();
  const configured = new Set(
    Array.isArray(ownerCommands)
      ? ownerCommands
        .filter((name) => typeof name === 'string')
        .map((name) => name.trim().toLowerCase())
        .filter(Boolean)
      : [],
  );
  if (!configured.has(command)) return { action: 'drop' };

  return { action: 'forward_owner', command };
}
