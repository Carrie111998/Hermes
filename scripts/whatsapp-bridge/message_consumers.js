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
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new Error('Consumer route must be an object');
  }
  const consumer = normalizeConsumerId(raw.consumer);
  const prefix = typeof raw.prefix === 'string' ? raw.prefix.trim() : '';
  const matchAll = raw.match_all === true || raw.matchAll === true;
  const chatValues = raw.chat_ids ?? raw.chatIds;
  if (!consumer || consumer === DEFAULT_CONSUMER) {
    throw new Error('Consumer route requires a named non-default consumer');
  }
  if (Boolean(prefix) === matchAll) {
    throw new Error('Consumer route requires exactly one of prefix or match_all');
  }
  if (!Array.isArray(chatValues)) {
    throw new Error('Consumer route requires a chat_ids array');
  }
  const chatIds = new Set(
    chatValues.map(value => String(value || '').trim()).filter(Boolean),
  );
  if (chatIds.size === 0) {
    throw new Error('Consumer route requires at least one chat ID');
  }
  return { consumer, prefix: prefix.toLowerCase(), matchAll, chatIds };
}

export function parseConsumerRoutes(raw) {
  if (!raw) return [];
  let values;
  try {
    values = typeof raw === 'string' ? JSON.parse(raw) : raw;
  } catch (error) {
    throw new Error(`Invalid consumer routes JSON: ${error.message}`);
  }
  if (!Array.isArray(values) || values.length > 16) {
    throw new Error('Consumer routes must be an array with at most 16 entries');
  }
  const routes = values.map(normalizeRoute);
  const catchAllChats = new Set();
  const prefixKeys = new Set();
  for (const route of routes) {
    for (const chatId of route.chatIds) {
      if (route.matchAll) {
        if (catchAllChats.has(chatId)) {
          throw new Error(`Duplicate catch-all consumer route for chat ${chatId}`);
        }
        catchAllChats.add(chatId);
      } else {
        const key = `${chatId}\0${route.prefix}`;
        if (prefixKeys.has(key)) {
          throw new Error(`Duplicate prefix consumer route for chat ${chatId}`);
        }
        prefixKeys.add(key);
      }
    }
  }
  return routes;
}

export function selectConsumerForEvent(event, routes) {
  // Named command routes are owner-DM only. A group conversation can be on
  // the allowlist while still containing participants who are not the owner.
  if (event?.isGroup === true) return DEFAULT_CONSUMER;
  const chatId = String(event?.chatId || '').trim();
  const body = String(event?.body || '').trimStart().toLowerCase();
  const prefixRoutes = (routes || [])
    .filter(route => !route.matchAll)
    .sort((left, right) => right.prefix.length - left.prefix.length);
  for (const route of prefixRoutes) {
    const exactPrefix = body === route.prefix;
    const prefixedCommand = body.startsWith(`${route.prefix} `)
      || body.startsWith(`${route.prefix}\n`);
    if (route.chatIds.has(chatId) && (exactPrefix || prefixedCommand)) {
      return route.consumer;
    }
  }
  for (const route of routes || []) {
    if (route.matchAll && route.chatIds.has(chatId)) {
      return route.consumer;
    }
  }
  return DEFAULT_CONSUMER;
}

export function createMessageConsumerQueues(limit = 100, configuredConsumers = []) {
  const maxQueueSize = Number.isInteger(limit) && limit > 0 ? limit : 100;
  const defaultQueue = [];
  const namedQueues = new Map(
    configuredConsumers
      .map(normalizeConsumerId)
      .filter(consumer => consumer && consumer !== DEFAULT_CONSUMER)
      .map(consumer => [consumer, []]),
  );
  const leases = new Map(
    Array.from(namedQueues, ([consumer]) => [consumer, new Map()]),
  );

  function pushBounded(queue, event) {
    queue.push(event);
    if (queue.length > maxQueueSize) queue.shift();
  }

  function queueFor(consumer) {
    if (consumer === DEFAULT_CONSUMER) return defaultQueue;
    return namedQueues.get(consumer) || null;
  }

  function enqueue(event, consumer = DEFAULT_CONSUMER) {
    const queue = queueFor(consumer);
    if (!queue) return false;
    pushBounded(queue, event);
    return true;
  }

  function drain(consumer = DEFAULT_CONSUMER) {
    const queue = queueFor(consumer);
    if (!queue) return null;
    return queue.splice(0, queue.length);
  }

  function lease(consumer, leaseMs = 60000, maxItems = 1, now = Date.now()) {
    const queue = queueFor(consumer);
    const consumerLeases = leases.get(consumer);
    if (!queue || !consumerLeases) return null;
    for (const [deliveryId, delivery] of consumerLeases) {
      if (delivery.expiresAt <= now) {
        queue.unshift(delivery.event);
        consumerLeases.delete(deliveryId);
      }
    }
    const deliveries = [];
    while (queue.length && deliveries.length < maxItems) {
      const event = queue.shift();
      const deliveryId = randomUUID();
      consumerLeases.set(deliveryId, { event, expiresAt: now + leaseMs });
      deliveries.push({ deliveryId, event });
    }
    return deliveries;
  }

  function ack(consumer, deliveryId) {
    const consumerLeases = leases.get(consumer);
    if (!consumerLeases) return null;
    return consumerLeases.delete(deliveryId);
  }

  function lengths() {
    return Object.fromEntries([
      [DEFAULT_CONSUMER, defaultQueue.length],
      ...Array.from(namedQueues, ([consumer, queue]) => [consumer, queue.length]),
    ]);
  }

  return { enqueue, drain, lease, ack, lengths, defaultQueueLength: () => defaultQueue.length };
}
import { randomUUID } from 'node:crypto';
