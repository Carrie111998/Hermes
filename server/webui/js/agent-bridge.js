/* Future Hermes Agent boundary.
   This module speaks only to the sales backend declared in api.js. The backend,
   not this browser client, will own Hermes auth, sessions, and streaming. */

import { call, config } from './api.js';

export const AGENT_RUN_STATES = new Set([
  'queued', 'running', 'completed', 'failed', 'cancelled',
]);

export const AGENT_EVENT_TYPES = new Set([
  'queued', 'started', 'progress', 'tool', 'completed', 'failed', 'cancelled',
]);

export function normalizeAgentEvent(event = {}) {
  const type = AGENT_EVENT_TYPES.has(event.type) ? event.type : 'progress';
  const status = AGENT_RUN_STATES.has(event.status)
    ? event.status
    : type === 'completed' ? 'completed'
      : type === 'failed' ? 'failed'
        : type === 'cancelled' ? 'cancelled'
          : type === 'queued' ? 'queued'
            : 'running';
  const progress = Number.isFinite(Number(event.progress))
    ? Math.max(0, Math.min(100, Number(event.progress)))
    : null;
  return {
    type,
    status,
    progress,
    message: String(event.message || ''),
    tool: event.tool ? {
      name: String(event.tool.name || ''),
      preview: String(event.tool.preview || ''),
    } : null,
    occurred_at: event.occurred_at || new Date().toISOString(),
  };
}

export function adapterIsEnabled(capabilities) {
  return Boolean(
    config.agentAdapter.enabled
    && capabilities?.adapter === 'hermes'
    && capabilities?.status === 'available',
  );
}

export async function getAgentCapabilities() {
  return call('agent.capabilities');
}

export async function getAgentStatus() {
  return call('agent.status');
}
