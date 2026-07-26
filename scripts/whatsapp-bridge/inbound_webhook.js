/**
 * Inbound webhook routing for the WhatsApp bridge.
 *
 * Some deployments send outbound campaigns (through POST /send) to people who
 * must be able to reply in the same chat without ever reaching the agent. The
 * allowlist is the gate on who may instruct the agent, so those senders can
 * never be added to it. This module gives their messages somewhere safe to go
 * instead: a deterministic HTTP forward to an operator controlled webhook,
 * decided purely by sender number, with no model anywhere in the path.
 *
 * The server behind `numbersUrl` decides who is routable. It returns
 * `{ tails: ["<digits>", ...] }` and a sender matches when its phone number
 * ends with one of the tails, so the server controls match length and the
 * bridge never holds full foreign numbers longer than a refresh interval.
 * The list refreshes on an interval and once per cooldown on a miss, so a
 * number that just entered a campaign is picked up within a message or two.
 *
 * Off unless `url` is configured. Wire from env in bridge.js:
 *   WHATSAPP_INBOUND_WEBHOOK_URL          POST target for matched messages
 *   WHATSAPP_INBOUND_WEBHOOK_NUMBERS_URL  GET, returns { tails: [] }
 *   WHATSAPP_INBOUND_WEBHOOK_SECRET       sent as x-hermes-key on both calls
 *
 * POST body: { from, text, attachments: [{ name, contentType, contentB64 }] }.
 * A 404 from the webhook means "no open conversation for this number" and is
 * not an error; the message is simply not stored server side.
 */

import { readFileSync, unlinkSync } from 'fs';
import path from 'path';

const REFRESH_MS = 30000;
const MISS_COOLDOWN_MS = 10000;
const REQUEST_TIMEOUT_MS = 20000;
const MAX_ATTACHMENT_BYTES = 12 * 1024 * 1024;

/**
 * The sender's real phone number as bare digits.
 *
 * WhatsApp increasingly addresses chats by an opaque @lid instead of the phone
 * number. Baileys exposes the underlying number as senderPn / participantPn
 * when that happens, and the bridge keeps a LID to phone map from the session
 * files as a fallback. An unmapped LID resolves to '' rather than leaking LID
 * digits that would never match a phone tail.
 */
export function resolveSenderPhone(msg, senderId, lidToPhone = {}) {
  for (const candidate of [msg?.key?.senderPn, msg?.key?.participantPn]) {
    if (typeof candidate === 'string' && candidate.endsWith('@s.whatsapp.net')) {
      return candidate.replace(/\D/g, '');
    }
  }
  const bare = String(senderId || '').replace(/:.*@/, '@');
  const digits = bare.replace(/@.*/, '').replace(/\D/g, '');
  if (bare.endsWith('@lid')) {
    return lidToPhone[digits] || '';
  }
  return digits;
}

export function createInboundWebhook({
  url = '',
  numbersUrl = '',
  secret = '',
  refreshMs = REFRESH_MS,
  missCooldownMs = MISS_COOLDOWN_MS,
  requestTimeoutMs = REQUEST_TIMEOUT_MS,
  maxAttachmentBytes = MAX_ATTACHMENT_BYTES,
  fetchImpl = fetch,
  now = Date.now,
  readFile = readFileSync,
  unlink = unlinkSync,
  log = console,
} = {}) {
  const enabled = Boolean(url && numbersUrl && secret);

  let tails = [];
  let lastRefreshAttempt = -Infinity;
  let timer = null;

  const headers = { 'x-hermes-key': secret };

  async function refresh() {
    // Stamped before the request so a failing server is retried at the
    // cooldown pace, not once per incoming message.
    lastRefreshAttempt = now();
    try {
      const response = await fetchImpl(numbersUrl, {
        headers,
        signal: AbortSignal.timeout(requestTimeoutMs),
      });
      if (!response.ok) {
        return;
      }
      const body = await response.json();
      if (Array.isArray(body?.tails)) {
        tails = body.tails.map((tail) => String(tail).replace(/\D/g, '')).filter(Boolean);
      }
    } catch (err) {
      // Keep the previous set rather than opening up or going silent on a blip.
      try {
        log.warn(`[bridge] inbound webhook numbers refresh failed: ${err?.message || err}`);
      } catch {}
    }
  }

  function matches(phone) {
    return tails.some((tail) => phone.endsWith(tail));
  }

  /**
   * Whether a sender's messages should be forwarded instead of dropped.
   * On a miss, refreshes the list once per cooldown window and rechecks, so
   * a number contacted moments ago is not lost to a stale list.
   */
  async function shouldForward(phone) {
    if (!enabled || !phone) {
      return false;
    }
    if (matches(phone)) {
      return true;
    }
    if (now() - lastRefreshAttempt < missCooldownMs) {
      return false;
    }
    await refresh();
    return matches(phone);
  }

  function collectAttachments(mediaUrls, mime, fileName) {
    const attachments = [];
    for (const filePath of mediaUrls || []) {
      try {
        const buffer = readFile(filePath);
        if (buffer.length > maxAttachmentBytes) {
          log.warn(`[bridge] inbound webhook attachment skipped, ${buffer.length} bytes exceeds the limit`);
          continue;
        }
        attachments.push({
          name: fileName || path.basename(filePath),
          contentType: mime || 'application/octet-stream',
          contentB64: buffer.toString('base64'),
        });
      } catch (err) {
        log.warn(`[bridge] inbound webhook could not read ${filePath}: ${err?.message || err}`);
      }
    }
    return attachments;
  }

  /**
   * Forwards one message. Throws when the webhook is unreachable or errors, so
   * the caller can log the loss; a 404 (no open conversation) is a normal
   * outcome. Media files are deleted after the webhook has answered — they
   * belong to the campaign owner, not to this machine's caches.
   */
  async function forward({ from, text = '', mediaUrls = [], mime = '', fileName = '' }) {
    const attachments = collectAttachments(mediaUrls, mime, fileName);
    const response = await fetchImpl(url, {
      method: 'POST',
      headers: { ...headers, 'Content-Type': 'application/json' },
      body: JSON.stringify({ from, text, attachments }),
      signal: AbortSignal.timeout(requestTimeoutMs),
    });
    if (!response.ok && response.status !== 404) {
      throw new Error(`inbound webhook returned ${response.status}`);
    }
    for (const filePath of mediaUrls || []) {
      try {
        unlink(filePath);
      } catch {}
    }
    return { stored: response.ok };
  }

  function start() {
    if (!enabled || timer) {
      return;
    }
    refresh();
    timer = setInterval(refresh, refreshMs);
    if (typeof timer.unref === 'function') {
      timer.unref();
    }
  }

  function stop() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  return { enabled, shouldForward, forward, refresh, start, stop };
}
