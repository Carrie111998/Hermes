/**
 * Decide whether Resuming a session needs an explicit ownership handoff.
 *
 * Live view is always read-only. Resume starts a writable TUI. When the
 * session still looks gateway-owned (active messaging source), require the
 * user to confirm they are taking over as a second writer.
 */

const GATEWAY_SESSION_SOURCES = new Set([
  "discord",
  "telegram",
  "slack",
  "feishu",
  "whatsapp",
  "signal",
  "matrix",
  "mattermost",
  "email",
  "sms",
  "api_server",
  "webhook",
  "qqbot",
  "dingtalk",
  "wecom",
  "weixin",
  "yuanbao",
  "bluebubbles",
  "homeassistant",
]);

export function sessionSourceFamily(source: string | null | undefined): string {
  if (!source) return "";
  return source.split(":")[0].trim().toLowerCase();
}

export function isGatewayOwnedSession(session: {
  is_active?: boolean | null;
  source?: string | null;
}): boolean {
  if (!session.is_active) return false;
  const family = sessionSourceFamily(session.source);
  return GATEWAY_SESSION_SOURCES.has(family);
}

export function shouldConfirmResumeOwnership(session: {
  is_active?: boolean | null;
  source?: string | null;
}): boolean {
  return isGatewayOwnedSession(session);
}
