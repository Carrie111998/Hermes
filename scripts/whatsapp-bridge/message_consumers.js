/**
 * Bounded, named inbound-message queues for the WhatsApp bridge.
 *
 * The default queue preserves the existing gateway contract.  Optional named
 * consumers may be selected by declarative routes supplied by the gateway;
 * this keeps one Baileys socket authoritative while preventing two pollers
 * from racing to drain the same messages.
 */

const DEFAULT_CONSUMER = 'default';
const CONSUMER_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/i;

export function normalizeConsumerId(value) {
  if (value === undefined || value === null || String(value).trim() === '') {
    return DEFAULT_CONSUMER;
  }
  const consumer = String(value).trim().toLowerCase();
  return CONSUMER_RE.test(consumer) ? consumer : null;
}

function normalizeRoute(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const consumer = normalizeConsumerId(raw.consumer);
  const prefix = typeof raw.prefix === 'string' ? raw.prefix.trim() : '';
  const chatValues = raw.chat_ids ?? raw.chatIds;
  if (!consumer || consumer === DEFAULT_CONSUMER || !prefix || !Array.isArray(chatValues)) {
    return null;
  }
  const chatIds = new Set(
    chatValues.map(value => String(value || '').trim()).filter(Boolean),
  );
  if (chatIds.size === 0) return null;
  return { consumer, prefix: prefix.toLowerCase(), chatIds };
}

export function parseConsumerRoutes(raw) {
  if (!raw) return [];
  let values;
  try {
    values = typeof raw === 'string' ? JSON.parse(raw) : raw;
  } catch {
    return [];
  }
  if (!Array.isArray(values)) return [];
  return values.map(normalizeRoute).filter(Boolean).slice(0, 16);
}

export function selectConsumerForEvent(event, routes) {
  const chatId = String(event?.chatId || '').trim();
  const body = String(event?.body || '').trimStart().toLowerCase();
  for (const route of routes || []) {
    const exactPrefix = body === route.prefix;
    const prefixedCommand = body.startsWith(`${route.prefix} `)
      || body.startsWith(`${route.prefix}\n`);
    if (route.chatIds.has(chatId) && (exactPrefix || prefixedCommand)) {
      return route.consumer;
    }
  }
  return DEFAULT_CONSUMER;
}

export function createMessageConsumerQueues(limit = 100) {
  const maxQueueSize = Number.isInteger(limit) && limit > 0 ? limit : 100;
  const defaultQueue = [];
  const namedQueues = new Map();

  function pushBounded(queue, event) {
    queue.push(event);
    if (queue.length > maxQueueSize) queue.shift();
  }

  function queueFor(consumer) {
    if (consumer === DEFAULT_CONSUMER) return defaultQueue;
    let queue = namedQueues.get(consumer);
    if (!queue) {
      queue = [];
      namedQueues.set(consumer, queue);
    }
    return queue;
  }

  function enqueue(event, consumer = DEFAULT_CONSUMER) {
    pushBounded(queueFor(consumer), event);
  }

  function drain(consumer = DEFAULT_CONSUMER) {
    const queue = queueFor(consumer);
    return queue.splice(0, queue.length);
  }

  function lengths() {
    return Object.fromEntries([
      [DEFAULT_CONSUMER, defaultQueue.length],
      ...Array.from(namedQueues, ([consumer, queue]) => [consumer, queue.length]),
    ]);
  }

  return { enqueue, drain, lengths, defaultQueueLength: () => defaultQueue.length };
}
