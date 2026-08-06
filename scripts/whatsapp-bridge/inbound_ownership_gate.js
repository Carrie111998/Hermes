const DEFAULT_TIMEOUT_MS = 1500;
const DEFAULT_PREFIXES = ['jeffersom'];

export function normalizedPrefixes(prefixes) {
  const custom = (Array.isArray(prefixes) ? prefixes : [])
    .map((value) => String(value || '').trim().toLocaleLowerCase('pt-BR'))
    .filter(Boolean)
    .slice(0, 8);
  return [...new Set([...DEFAULT_PREFIXES, ...custom])];
}

function startsWithExplicitPrefix(body, prefixes) {
  const text = String(body || '').trimStart().toLocaleLowerCase('pt-BR');
  return normalizedPrefixes(prefixes).some((prefix) => {
    if (!text.startsWith(prefix)) return false;
    const next = text.slice(prefix.length, prefix.length + 1);
    return !next || /[\s,.:;!?\-]/u.test(next);
  });
}

function isSafeRouterUrl(value) {
  try {
    const url = new URL(value);
    if (url.protocol === 'https:') return true;
    return url.protocol === 'http:' && ['127.0.0.1', 'localhost', '::1', '[::1]'].includes(url.hostname);
  } catch {
    return false;
  }
}

export function buildOwnershipSignal({ event, senderAliases }) {
  const body = String(event?.body || '').trim();
  const replyKind = body === '1'
    ? 'approval_1'
    : body === '2'
      ? 'approval_2'
      : body
        ? 'text'
        : 'none';
  return {
    senderAliases: [...new Set((senderAliases || []).map((value) => String(value || '').trim()).filter(Boolean))].slice(0, 8),
    messageId: event?.messageId
      ? String(event.messageId)
      : event?.id
        ? String(event.id)
        : null,
    quotedMessageId: event?.quotedMessageId ? String(event.quotedMessageId) : null,
    replyKind,
    hasText: Boolean(body),
    fromOwner: Boolean(event?.fromOwner),
  };
}

export async function applyInboundOwnershipGate({ config, event, senderAliases, fetchFn = globalThis.fetch }) {
  if (!config?.url) return { action: 'pass', reason: 'router_disabled' };
  if (startsWithExplicitPrefix(event?.body, config.prefixes)) {
    return { action: 'pass', reason: 'explicit_agent_prefix' };
  }
  if (!isSafeRouterUrl(config.url)) {
    return { action: 'drop', reason: 'invalid_router_url' };
  }
  if (!config.token) {
    return { action: 'drop', reason: 'missing_router_token' };
  }

  const timeoutMs = Number.isFinite(Number(config.timeoutMs))
    ? Math.max(1, Math.min(Number(config.timeoutMs), 10000))
    : DEFAULT_TIMEOUT_MS;
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const headers = { 'content-type': 'application/json' };
    if (config.token) headers.authorization = `Bearer ${config.token}`;
    const response = await fetchFn(config.url, {
      method: 'POST',
      headers,
      body: JSON.stringify(buildOwnershipSignal({ event, senderAliases })),
      signal: controller.signal,
      redirect: 'error',
    });
    if (!response?.ok) return { action: 'drop', reason: 'router_http_error' };
    const result = await response.json();
    if (result?.owner === 'jeffersom') return { action: 'pass', reason: 'owned_by_jeffersom' };
    if (result?.owner === 'autocria') return { action: 'drop', reason: 'owned_by_autocria' };
    return { action: 'drop', reason: 'router_ambiguous' };
  } catch (error) {
    return {
      action: 'drop',
      reason: error?.name === 'AbortError' ? 'router_timeout' : 'router_unavailable',
    };
  } finally {
    clearTimeout(timer);
  }
}
